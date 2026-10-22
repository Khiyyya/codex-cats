"""Isolated macOS lifecycle tests; launchctl and app discovery never touch the user setup."""
import json
import os
from pathlib import Path
import plistlib
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from uplink12 import integration, macos
from uplink12.desktop import control


class ProxyTests(unittest.TestCase):
    def probe(self, output):
        result = subprocess.CompletedProcess([], 0, output, '')
        with patch.dict(os.environ, {}, clear=True), patch.object(macos.subprocess, 'run', return_value=result):
            return macos.detect_proxy()

    def test_system_https_proxy_uses_http_connect(self):
        self.assertEqual(self.probe(' HTTPSEnable : 1\n HTTPSProxy : 127.0.0.1\n HTTPSPort : 7897\n'),
                         ('http://127.0.0.1:7897', 'macos_system'))

    def test_system_http_proxy(self):
        self.assertEqual(self.probe(' HTTPEnable : 1\n HTTPProxy : localhost\n HTTPPort : 8080\n'),
                         ('http://localhost:8080', 'macos_system'))

    def test_ipv6_proxy(self):
        self.assertEqual(self.probe(' HTTPEnable : 1\n HTTPProxy : ::1\n HTTPPort : 8080\n')[0],
                         'http://[::1]:8080')

    def test_direct(self):
        self.assertEqual(self.probe('<dictionary> {\n}\n'), (None, 'direct'))

    def test_reject_automatic_proxy(self):
        for field in ('ProxyAutoConfigEnable', 'ProxyAutoDiscoveryEnable'):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'PAC/WPAD'):
                self.probe(f' {field} : 1\n')

    def test_reject_socks(self):
        with self.assertRaisesRegex(ValueError, 'SOCKS'):
            self.probe(' SOCKSEnable : 1\n')

    def test_reject_invalid_port(self):
        for port in ('0', '65536', 'oops'):
            with self.subTest(port=port), self.assertRaises(ValueError):
                self.probe(f' HTTPEnable : 1\n HTTPProxy : localhost\n HTTPPort : {port}\n')

    def test_environment_precedes_system_and_is_validated(self):
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://localhost:8888'}, clear=True), \
                patch.object(macos.subprocess, 'run') as run:
            self.assertEqual(macos.detect_proxy(), ('http://localhost:8888', 'environment'))
            run.assert_not_called()
        for proxy in ('socks5://localhost:8080', 'http://user:secret@localhost:8080'):
            with patch.dict(os.environ, {'ALL_PROXY': proxy}, clear=True), self.assertRaises(ValueError):
                macos.detect_proxy()

    def test_system_probe_failure_does_not_silently_use_direct(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(macos.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'scutil')), \
                self.assertRaises(subprocess.CalledProcessError):
            macos.detect_proxy()


class DiscoveryTests(unittest.TestCase):
    def test_renamed_app_and_bundled_core(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / 'ChatGPT with spaces.app'
            contents = app / 'Contents'
            core = contents / 'Resources/codex-cli/bin/codex'
            core.parent.mkdir(parents=True)
            core.write_text('#!/bin/sh\n')
            core.chmod(0o755)
            info = {'CFBundleIdentifier': 'com.openai.codex', 'CFBundleShortVersionString': 'test'}
            (contents / 'Info.plist').write_bytes(plistlib.dumps(info))
            with patch.dict(os.environ, {'CATS_CODEX_APP': str(app)}):
                self.assertEqual(macos.discover()['core'], str(core))
                info['CFBundleIdentifier'] = 'com.example.other'
                (contents / 'Info.plist').write_bytes(plistlib.dumps(info))
                self.assertIsNone(macos.discover()['desktop'])

    def test_missing_core_is_not_supported(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / 'Codex.app'
            (app / 'Contents').mkdir(parents=True)
            (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'com.openai.codex'}))
            with patch.dict(os.environ, {'CATS_CODEX_APP': str(app)}):
                self.assertIsNone(macos.discover()['desktop'])


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve() / 'CATS with spaces'
        self.path = Path(self.temporary.name) / 'agent.plist'
        self.patch = patch.object(macos, 'agent_path', return_value=self.path)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_venv_python_is_not_resolved_to_base_interpreter(self):
        with patch.object(sys, 'executable', '/tmp/venv/bin/python'):
            self.assertEqual(macos.daemon_arguments(self.home)[0], '/tmp/venv/bin/python')

    def test_arguments_and_proxy_survive_plist_roundtrip(self):
        cfg = {'proxy_source': 'environment', 'upstream_proxy': 'http://127.0.0.1:7897'}
        doc = plistlib.loads(plistlib.dumps(macos.agent_document(self.home, cfg)))
        self.assertEqual(doc['ProgramArguments'][-2], str(self.home))
        self.assertEqual(doc['EnvironmentVariables'], {'HTTPS_PROXY': cfg['upstream_proxy']})
        self.assertEqual(doc['KeepAlive'], {'SuccessfulExit': False})
        self.assertEqual(doc['Umask'], 0o077)

    def test_system_proxy_is_redetected_at_login(self):
        self.assertNotIn('EnvironmentVariables', macos.agent_document(self.home, {'proxy_source': 'macos_system'}))

    def test_reject_foreign_or_symlinked_agent(self):
        self.path.write_bytes(plistlib.dumps({'Label': 'unrelated'}))
        with self.assertRaisesRegex(RuntimeError, 'another installation'):
            macos.owned_agent(self.home)
        self.path.unlink()
        target = self.path.with_suffix('.other')
        target.write_bytes(b'private')
        self.path.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            macos.owned_agent(self.home)
        self.assertEqual(target.read_bytes(), b'private')

    def test_scope_changes_with_home(self):
        self.assertNotEqual(macos.label(self.home), macos.label(self.home / 'other'))


@unittest.skipUnless(sys.platform == 'darwin', 'macOS daemon lifecycle')
class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / 'Application Support/CATS test'
        self.config = self.root / 'codex/config.toml'
        self.config.parent.mkdir()
        self.original = b'\xef\xbb\xbf# retain comments and UTF-8 BOM\nmodel = "test-model"\n'
        self.config.write_bytes(self.original)
        self.agent = self.root / 'LaunchAgents/cats.plist'
        self.process = None
        self.addCleanup(self.cleanup_process)
        for replacement in (
            patch.dict(os.environ, {'CODEX_HOME': str(self.config.parent), 'OPENAI_BASE_URL': ''}),
            patch.object(macos, 'discover', return_value={'desktop': {'Version': 'test'}, 'core': '/test/codex'}),
            patch.object(macos, 'detect_proxy', return_value=(None, 'direct')),
            patch.object(macos, 'agent_path', return_value=self.agent),
            patch.object(macos, 'launchctl', side_effect=self.fake_launchctl),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def cleanup_process(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def fake_launchctl(self, *arguments, check=True):
        if arguments[0] == 'print':
            return self.process is not None and self.process.poll() is None
        if arguments[0] == 'bootstrap':
            environment = {**os.environ, 'HTTPS_PROXY': 'http://127.0.0.1:9'}
            self.process = subprocess.Popen(macos.daemon_arguments(self.home), env=environment,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        if arguments[0] == 'bootout':
            self.cleanup_process()
            return True
        raise AssertionError(arguments)

    def test_install_status_idempotent_start_stop_restart(self):
        self.assertTrue(macos.install(self.home)['installed'])
        self.assertTrue(macos.report(self.home)['configuration_managed'])
        self.assertEqual(control(self.home)['product'], integration.PRODUCT)
        pid = self.process.pid
        macos.install(self.home)
        self.assertEqual(self.process.pid, pid)
        self.assertTrue(macos.disable(self.home)['stopped'])
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse(self.agent.exists())
        self.assertFalse((self.home / 'runtime.json').exists())
        macos.install(self.home)
        self.assertNotEqual(self.process.pid, pid)
        macos.disable(self.home)
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_sigterm_restores_configuration(self):
        macos.install(self.home)
        self.process.send_signal(signal.SIGTERM)
        self.assertEqual(self.process.wait(timeout=10), 0)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse((self.home / 'runtime.json').exists())
        macos.disable(self.home)

    def test_activation_failure_rolls_back_settings_agent_and_config(self):
        with patch.object(integration, 'activate', side_effect=RuntimeError('injected activation failure')):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                macos.install(self.home)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse(self.agent.exists())
        self.assertFalse((self.home / 'settings.json').exists())
        self.assertIsNotNone(self.process.poll())

    def test_bootstrap_failure_rolls_back(self):
        def fail(*args, check=True):
            if args[0] == 'print':
                return False
            raise RuntimeError('bootstrap failed')
        with patch.object(macos, 'launchctl', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'bootstrap failed'):
                macos.install(self.home)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse(self.agent.exists())
        self.assertFalse((self.home / 'settings.json').exists())

    def test_stop_preserves_unrelated_user_edits(self):
        macos.install(self.home)
        with self.config.open('ab') as file:
            file.write(b'\n# added while active\n')
        macos.disable(self.home)
        self.assertIn(b'# added while active', self.config.read_bytes())
        self.assertNotIn(b'openai_base_url', self.config.read_bytes())

    def test_custom_provider_refused_before_launch(self):
        self.config.write_text('model_provider = "custom"\n')
        with self.assertRaisesRegex(ValueError, 'custom provider'):
            macos.install(self.home)
        self.assertIsNone(self.process)
        self.assertFalse(self.agent.exists())

    def test_foreign_agent_preserved(self):
        self.agent.parent.mkdir()
        raw = plistlib.dumps({'Label': 'foreign'})
        self.agent.write_bytes(raw)
        with self.assertRaisesRegex(RuntimeError, 'another installation'):
            macos.install(self.home)
        self.assertEqual(self.agent.read_bytes(), raw)
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_new_configuration_removed_on_stop(self):
        self.config.unlink()
        macos.install(self.home)
        self.assertTrue(self.config.exists())
        macos.disable(self.home)
        self.assertFalse(self.config.exists())

    def test_environment_override_refused(self):
        with patch.dict(os.environ, {'OPENAI_BASE_URL': 'https://example.com'}):
            with self.assertRaisesRegex(ValueError, 'override'):
                macos.install(self.home)
        self.assertIsNone(self.process)

    def test_api_key_login_refused(self):
        (self.config.parent / 'auth.json').write_text(json.dumps({'auth_mode': 'apikey'}))
        with self.assertRaisesRegex(ValueError, 'ChatGPT login'):
            macos.install(self.home)

    def test_occupied_port_is_not_replaced(self):
        macos.install(self.home)
        macos.disable(self.home)
        cfg = macos.read_settings(self.home)
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', cfg['port']))
            listener.listen()
            with self.assertRaises(OSError):
                macos.install(self.home)
            self.assertEqual(self.config.read_bytes(), self.original)


if __name__ == '__main__':
    unittest.main()
