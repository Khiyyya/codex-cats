"""Experimental per-user macOS integration. No system proxy or certificate edits."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import plistlib
import re
import secrets
import socket
import subprocess
import sys
import time
import tomllib

from . import integration as shared
from .metrics import status as metric_status


def default_home():
    return Path.home() / 'Library/Application Support/CodexCATS'


def discover():
    override = os.environ.get('CATS_CODEX_APP')
    candidates = [Path(override).expanduser()] if override else [
        root / name for root in (Path('/Applications'), Path.home() / 'Applications')
        for name in ('Codex.app', 'ChatGPT.app')]
    for app in candidates:
        try:
            info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
        if info.get('CFBundleIdentifier') != 'com.openai.codex':
            continue
        contents = app / 'Contents'
        cores = [contents / name for name in (
            'Resources/codex', 'Resources/codex-cli/bin/codex',
            'Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex', 'MacOS/codex')]
        core = next((p for p in cores if p.is_file() and os.access(p, os.X_OK)), None)
        if core:
            return {'desktop': {'Version': info.get('CFBundleShortVersionString', 'unknown'),
                                'InstallLocation': str(app.resolve()),
                                'BundleIdentifier': info['CFBundleIdentifier']},
                    'core': str(core)}
    return {'desktop': None, 'core': None}


def detect_proxy():
    # Explicit settings take precedence, including for GUI login environments.
    for key in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy', 'ALL_PROXY', 'all_proxy'):
        if os.environ.get(key):
            return shared.parse_proxy(os.environ[key]), 'environment'
    result = subprocess.run(['/usr/sbin/scutil', '--proxy'], capture_output=True,
                            text=True, timeout=10, check=True)
    values = dict(re.findall(r'^\s*(\w+)\s*:\s*(.*?)\s*$', result.stdout, re.MULTILINE))
    if any(values.get(key) == '1' for key in ('ProxyAutoConfigEnable', 'ProxyAutoDiscoveryEnable')):
        raise ValueError('PAC/WPAD detected; set an explicit HTTP(S) proxy')
    for prefix in ('HTTPS', 'HTTP'):
        if values.get(prefix + 'Enable') == '1':
            host, port = values.get(prefix + 'Proxy', ''), values.get(prefix + 'Port', '')
            if not port.isdecimal() or not 1 <= int(port) <= 65535:
                raise ValueError('invalid macOS system proxy port')
            if ':' in host and not host.startswith('['):
                host = '[' + host + ']'
            # HTTPSProxy denotes CONNECT for HTTPS destinations, not TLS to the proxy.
            return shared.parse_proxy(f'http://{host}:{port}'), 'macos_system'
    if values.get('SOCKSEnable') == '1':
        raise ValueError('SOCKS detected; set an explicit HTTP(S) proxy')
    return None, 'direct'


def label(home):
    scope = hashlib.sha256(str(home.resolve()).encode()).hexdigest()[:12]
    return 'io.github.khiyyya.codex-cats.' + scope


def agent_path(home):
    return Path.home() / 'Library/LaunchAgents' / (label(home) + '.plist')


def daemon_arguments(home):
    # Resolving the venv's python symlink would lose its site-packages.
    return [os.path.abspath(sys.executable), '-m', 'uplink12.desktop',
            '--home', str(home.resolve()), 'daemon']


def agent_document(home, cfg):
    doc = {'Label': label(home), 'ProgramArguments': daemon_arguments(home),
           'WorkingDirectory': str(home.resolve()), 'RunAtLoad': True,
           'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval': 10,
           'ExitTimeOut': 15, 'Umask': 0o077,
           'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null'}
    # launchd does not inherit the installing shell's proxy/CODEX_HOME variables.
    # Only the already validated, credential-free proxy is carried into login.
    if cfg.get('proxy_source') == 'environment' and cfg.get('upstream_proxy'):
        doc['EnvironmentVariables'] = {'HTTPS_PROXY': cfg['upstream_proxy']}
    return doc


def owned_agent(home):
    path = agent_path(home)
    if path.is_symlink():
        raise RuntimeError('LaunchAgent path is a symlink; preserving it')
    if not path.exists():
        return None
    try:
        doc = plistlib.loads(path.read_bytes())
    except (ValueError, plistlib.InvalidFileException):
        raise RuntimeError('unrecognized LaunchAgent; preserving it') from None
    args = doc.get('ProgramArguments', [])
    if (doc.get('Label') != label(home) or doc.get('WorkingDirectory') != str(home.resolve())
            or not isinstance(args, list) or len(args) != 6
            or args[1:] != ['-m', 'uplink12.desktop', '--home', str(home.resolve()), 'daemon']):
        raise RuntimeError('LaunchAgent owned by another installation; preserving it')
    return doc


def launchctl(*arguments, check=True):
    result = subprocess.run(['/bin/launchctl', *arguments], capture_output=True, timeout=20)
    if check and result.returncode:
        raise RuntimeError('LaunchAgent operation failed; run doctor from a logged-in macOS desktop')
    return result.returncode == 0


def service(home):
    return f'gui/{os.getuid()}/{label(home)}'


def loaded(home):
    return launchctl('print', service(home), check=False)


def wait_stopped(home):
    from .desktop import healthy
    for _ in range(100):
        if not healthy(home) and not loaded(home):
            return
        time.sleep(.1)
    raise RuntimeError('gateway cleanup incomplete; installation files retained, retry stop')


def read_settings(home):
    cfg = shared.load_json(home / 'settings.json')
    if cfg.get('product') != shared.PRODUCT:
        raise RuntimeError('unrecognized installation settings')
    return cfg


def install(home):
    from .desktop import InstanceLock
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    with InstanceLock(home / 'manager.lock'):
        return install_locked(home)


def install_locked(home):
    from .desktop import base_url, control, healthy
    if os.environ.get('OPENAI_BASE_URL'):
        raise ValueError('OPENAI_BASE_URL environment override detected; automatic integration refused')
    config = shared.codex_config().resolve()
    auth = config.parent / 'auth.json'
    if auth.exists() and shared.load_json(auth).get('auth_mode') not in (None, 'chatgpt'):
        raise ValueError('automatic integration currently requires ChatGPT login')
    found = discover()
    if not found['desktop'] or not found['core']:
        raise RuntimeError('Codex macOS app not found; set CATS_CODEX_APP to its .app path')
    file = home / 'settings.json'
    cfg = read_settings(home) if file.exists() else None
    if cfg and cfg['config'] != str(config):
        raise ValueError('different CODEX_HOME; stop using the original CODEX_HOME first')
    proxy, source = detect_proxy()
    if cfg is None:
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(('127.0.0.1', 8766))
            except OSError:
                sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        cfg = {'product': shared.PRODUCT, 'port': port, 'local_token': secrets.token_urlsafe(32),
               'target_http': 'https://chatgpt.com/backend-api/codex/responses',
               'target_ws': 'wss://chatgpt.com/backend-api/codex/responses',
               'config': str(config), 'enabled': False}
    cfg = {**cfg, 'upstream_proxy': proxy, 'proxy_source': source}
    shared.activation_state(config, home, base_url(cfg))
    previous_agent = owned_agent(home)
    was_loaded, was_running = loaded(home), healthy(home) if file.exists() else False
    document = agent_document(home, cfg)
    if was_loaded and (previous_agent != document or not was_running):
        raise RuntimeError('existing LaunchAgent must be stopped before changing its runtime or proxy')
    if was_running and not was_loaded:
        raise RuntimeError('gateway running outside the owned LaunchAgent; stop it first')
    if not was_running:
        with socket.socket() as sock:
            # Recent loopback control connections may leave TIME_WAIT sockets.
            # An active listener is still refused; SO_REUSEPORT is never set.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', cfg['port']))
    watched = (config, home / 'config-ownership.json', home / 'config-original.toml', file, agent_path(home))
    snapshot = {path: path.read_bytes() if path.exists() else None for path in watched}
    activated_hash = None
    try:
        shared.save_json(file, cfg)
        if not was_loaded:
            shared.atomic(agent_path(home), plistlib.dumps(document))
            launchctl('bootstrap', f'gui/{os.getuid()}', str(agent_path(home)))
        for _ in range(100):
            if healthy(home):
                break
            time.sleep(.1)
        else:
            raise RuntimeError('gateway failed to start; run doctor')
        shared.activate(config, home, base_url(cfg))
        activated_hash = shared.digest(config.read_bytes())
        cfg['enabled'] = True
        shared.save_json(file, cfg)
        (home / 'last-error.txt').unlink(missing_ok=True)
    except Exception:
        if not was_loaded:
            # Stop crash recovery before requesting shutdown or restoring files.
            if loaded(home):
                launchctl('bootout', service(home))
            try:
                control(home, 'shutdown')
            except Exception:
                pass
            wait_stopped(home)
        current = config.read_bytes() if config.exists() else None
        if current == snapshot[config] or (activated_hash and current is not None
                                          and shared.digest(current) == activated_hash):
            for path, raw in snapshot.items():
                if raw is None:
                    path.unlink(missing_ok=True)
                else:
                    shared.atomic(path, raw)
        else:
            # Concurrent user edits win; remove only our own base URL.
            shared.restore(home)
            cfg['enabled'] = False
            shared.save_json(file, cfg)
        raise
    return {'installed': True, 'gateway_ready': True, 'autostart': True,
            'desktop_version': found['desktop']['Version'], 'home': str(home),
            'activation': 'restart existing Codex once', 'proxy_source': source}


def disable(home):
    from .desktop import InstanceLock, control
    with InstanceLock(home / 'manager.lock'):
        cfg = read_settings(home)
        document = owned_agent(home)
        if loaded(home) and document is None:
            raise RuntimeError('loaded LaunchAgent ownership cannot be confirmed; preserving it')
        outcome = shared.restore(home)
        cfg['enabled'] = False
        shared.save_json(home / 'settings.json', cfg)
        if loaded(home):
            launchctl('bootout', service(home))
        try:
            control(home, 'shutdown')
        except Exception:
            pass
        wait_stopped(home)
        if document is not None:
            agent_path(home).unlink()
        return {'configuration': outcome, 'stopped': True, 'autostart_removed': True}


def report(home):
    from .desktop import base_url, control
    if not (home / 'settings.json').exists():
        return {'installed': False, 'home': str(home)}
    cfg = read_settings(home)
    try:
        health = control(home)
    except Exception:
        health = None
    config = Path(cfg['config'])
    doc = tomllib.loads(config.read_text(encoding='utf-8-sig')) if config.exists() else {}
    return {'installed': bool(cfg.get('enabled')), 'home': str(home), 'gateway': health,
            'configuration_managed': doc.get('openai_base_url') == base_url(cfg),
            'autostart': owned_agent(home) is not None, 'agent_loaded': loaded(home),
            'proxy_source': cfg['proxy_source'],
            'metrics_kind': 'application payload; NOT TLS/network bytes',
            'metrics': metric_status(home / 'metrics.jsonl')}


def doctor(home):
    result = report(home)
    found = discover()
    result.update(platform='macOS', desktop_detected=bool(found['desktop']),
                  official_core_detected=bool(found['core']),
                  desktop_version=found['desktop']['Version'] if found['desktop'] else None)
    result['detected_proxy_source'] = detect_proxy()[1]
    result['verification_limits'] = ['macOS GUI not verified; isolated real-core measurements are scenario-specific',
        'LaunchAgent registration does not prove logout/login recovery',
        'native WebSocket continuation can already avoid full uploads']
    startup = home / 'last-error.txt'
    if startup.exists():
        name = startup.read_text(encoding='utf-8', errors='replace').split(';', 1)[0].strip()
        result['startup_error'] = name if name.isidentifier() and len(name) <= 64 else 'unknown'
    return result


def main():
    parser = argparse.ArgumentParser(description='Experimental macOS CATS integration')
    parser.add_argument('--home', type=Path, default=default_home())
    parser.add_argument('command', choices=['install', 'start', 'stop', 'uninstall',
                                          'status', 'doctor', 'daemon', 'open'])
    args = parser.parse_args()
    home = args.home.expanduser().resolve()
    logging.basicConfig(level=logging.CRITICAL)
    try:
        if sys.platform != 'darwin':
            raise RuntimeError('macOS required')
        if args.command == 'daemon':
            from .desktop import run_daemon
            run_daemon(home)
            return
        if args.command in ('install', 'start', 'open'):
            result = install(home)
            if args.command == 'open':
                subprocess.run(['/usr/bin/open', discover()['desktop']['InstallLocation']], check=True)
                result['launch_requested'] = True
        elif args.command in ('stop', 'uninstall'):
            result = disable(home) if (home / 'settings.json').exists() else {'installed': False}
            if args.command == 'uninstall':
                result['runtime_retained'] = str(home)
                result['note'] = 'integration removed; private runtime and backups retained for manual removal'
        elif args.command == 'doctor':
            result = doctor(home)
        else:
            result = report(home)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as error:
        safe = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
        print(json.dumps({'error': safe}, ensure_ascii=False))
        sys.exit(1)


if __name__ == '__main__':
    main()
