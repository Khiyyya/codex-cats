#!/usr/bin/env python3
"""Opt-in real-upstream benchmark. Counts only bounded metadata; never saves auth/headers.

Run with the project Python. Credentials are copied into a temporary CODEX_HOME,
used only by the official installed core, then removed. Source config is untouched.
"""
from __future__ import annotations
import argparse
import asyncio
import base64
from contextlib import suppress
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import random
import secrets
import shutil
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, WSMsgType, web
from multidict import CIMultiDict
from websockets.asyncio.client import connect

from uplink12.gateway import Gateway, decoded_json, headers_for_ws, HOP, HTTP_HOP
from uplink12.integration import codex_config, parse_proxy
from uplink12.macos import discover, detect_proxy
from uplink12.protocol import identity, response_event
from uplink12.gateway import PRINCIPAL

TARGET = 'https://chatgpt.com/backend-api/codex/responses'
WS_TARGET = TARGET.replace('https:', 'wss:', 1)


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def progress(**values):
    print(dump(values), flush=True)


def summary_request(wire, transport, turn, stage):
    try:
        body = json.loads(wire) if isinstance(wire, (str, bytes)) else wire
        items = body.get('input', [])
        return {'transport': transport, 'turn': turn, 'stage': stage,
                'canonical_bytes': len(dump(body).encode()),
                'input_items': len(items) if isinstance(items, list) else None,
                'has_previous_response_id': bool(body.get('previous_response_id')),
                'is_warmup': body.get('generate') is False,
                'identity_present': identity(body) is not None,
                'metadata_keys': sorted(body.get('client_metadata', {}).keys()),
                'type': body.get('type'), 'completed': False}
    except (ValueError, AttributeError, TypeError):
        return {'transport': transport, 'turn': turn, 'stage': stage, 'parse_failed': True}


class CountingTunnel:
    """Count bytes successfully queued to the existing HTTP proxy, including CONNECT/TLS.

    This is a local relay-to-proxy measurement, NOT physical WAN/NIC traffic.
    No TLS decryption or certificate changes occur here.
    """
    def __init__(self, proxy):
        self.proxy = urlsplit(proxy) if proxy else None
        if self.proxy and self.proxy.scheme != 'http':
            raise ValueError('benchmark byte boundary currently requires an HTTP CONNECT proxy')
        self.up = self.down = self.connects = self.blocked = 0
        self.blocked_until = 0
        self.writers = set()
        self.tasks = set()

    async def start(self):
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0)
        self.url = f'http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}'
        return self

    async def outage(self, seconds):
        self.blocked_until = time.monotonic() + seconds
        for writer in list(self.writers):
            writer.close()
        progress(event='outage_started', seconds=seconds)

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        peer = None
        self.writers.add(writer)
        try:
            header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 15)
            first = header.split(b'\r\n', 1)[0].split()
            if len(first) != 3 or first[0] != b'CONNECT' or first[1] != b'chatgpt.com:443':
                writer.write(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
                await writer.drain()
                return
            if time.monotonic() < self.blocked_until:
                self.blocked += 1
                writer.write(b'HTTP/1.1 503 Unavailable\r\nContent-Length: 0\r\n\r\n')
                await writer.drain()
                return
            self.connects += 1
            host, port = (self.proxy.hostname, self.proxy.port or 80) if self.proxy else ('chatgpt.com', 443)
            remote_reader, peer = await asyncio.wait_for(asyncio.open_connection(host, port), 15)
            self.writers.add(peer)
            if self.proxy:
                peer.write(header)
                await peer.drain()
                self.up += len(header)
                reply = await asyncio.wait_for(remote_reader.readuntil(b'\r\n\r\n'), 15)
                self.down += len(reply)
                writer.write(reply)
            else:
                writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            await writer.drain()

            async def copy(source, target, up):
                while data := await source.read(65536):
                    target.write(data)
                    await target.drain()
                    if up:
                        self.up += len(data)
                    else:
                        self.down += len(data)
            transfers = [asyncio.create_task(copy(reader, peer, True)),
                         asyncio.create_task(copy(remote_reader, writer, False))]
            try:
                await asyncio.wait(transfers, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for transfer in transfers:
                    transfer.cancel()
                await asyncio.gather(*transfers, return_exceptions=True)
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            for stream in (writer, peer):
                if stream is not None:
                    stream.close()
                    self.writers.discard(stream)
            self.tasks.discard(task)

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        for writer in list(self.writers):
            writer.close()
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)


class ObservedGateway(Gateway):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.turn = 0
        self.observed = []
        self.scope_checks = []
        self.previous_key = None
        self.previous_headers = {}

    async def session(self, key, headers):
        current = {k.lower(): v for k, v in headers_for_ws(headers) if k.lower() not in PRINCIPAL}
        previous = self.previous_key
        self.scope_checks.append({'turn': self.turn,
            'identity_same': previous is not None and key[0] == previous[0],
            'principal_same': previous is not None and key[1] == previous[1],
            'handshake_same': previous is not None and key[2] == previous[2],
            'changed_noncredential_header_names': sorted(k for k in current.keys() | self.previous_headers.keys()
                if current.get(k) != self.previous_headers.get(k)),
            'pool_entries': len(self.pool)})
        self.previous_key, self.previous_headers = key, current
        return await super().session(key, headers)

    async def http_native(self, req, raw, reason, target=None):
        row = None
        if req.method == 'POST' and req.path.endswith('/responses') and raw is not None:
            try:
                row = summary_request(decoded_json(raw, req.headers.get('Content-Encoding', 'identity')),
                    'http', self.turn, 'upstream')
                row['fallback_reason'] = reason
                self.observed.append(row)
            except ValueError:
                pass
        response = await super().http_native(req, raw, reason, target)
        if row:
            row['http_status'] = response.status
        return response

    async def send_wire(self, socket, wire):
        row = summary_request(wire, 'websocket', self.turn, 'upstream')
        self.observed.append(row)
        await Gateway.send_wire(socket, wire)
        row['send_returned'] = True

    async def exchange(self, *args, **kwargs):
        async for chunk, event in super().exchange(*args, **kwargs):
            if event.get('type') == 'response.completed' and self.observed:
                self.observed[-1]['completed'] = True
            yield chunk, event


class ObservationRelay:
    """Preserve native HTTP bytes and WS messages; metadata only on disk."""
    def __init__(self, tunnel, cats=None):
        self.tunnel, self.cats = tunnel, cats
        self.turn = 0
        self.ingress = []
        self.upstream = []
        self.errors = []

    async def start(self):
        self.client = ClientSession(auto_decompress=False, trust_env=False,
            cookie_jar=DummyCookieJar(), timeout=ClientTimeout(total=None, connect=20),
            skip_auto_headers={'Accept', 'Accept-Encoding', 'User-Agent', 'Content-Type'})
        self.client._retry_connection = False
        app = web.Application(client_max_size=512*1024*1024, handler_args={'auto_decompress': False})
        app.router.add_route('*', '/{tail:.*}', self.handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
        return self

    def target(self, request, ws=False):
        path = request.rel_url.raw_path
        if self.cats:
            root = f'http://127.0.0.1:{self.cats.listen_port()}/uplink/{self.cats.token}'
            target = root + path
        else:
            target = TARGET.rsplit('/', 1)[0] + path
        if ws:
            target = target.replace('https:', 'wss:', 1).replace('http:', 'ws:', 1)
        if request.rel_url.raw_query_string:
            target += '?' + request.rel_url.raw_query_string
        return target

    async def handle(self, request):
        if request.headers.get('Upgrade', '').lower() == 'websocket':
            return await self.websocket(request)
        raw = await request.read()
        row = None
        if request.method == 'POST' and request.path.endswith('/responses'):
            try:
                body = decoded_json(raw, request.headers.get('Content-Encoding', 'identity'))
                row = summary_request(body, 'http', self.turn, 'ingress')
                row['received_body_bytes'] = len(raw)
                row['content_encoding'] = request.headers.get('Content-Encoding', 'identity')
                self.ingress.append(row)
                if not self.cats:
                    upstream_row = row.copy()
                    self.upstream.append(upstream_row)
            except ValueError:
                self.errors.append({'phase': 'decode_http', 'turn': self.turn})
        headers = CIMultiDict((k, v) for k, v in request.headers.items()
                              if k.lower() not in HTTP_HOP | {'host', 'content-length'})
        target = self.target(request)
        try:
            async with self.client.request(request.method, target, data=raw, headers=headers,
                    proxy=None if self.cats else self.tunnel.url, allow_redirects=False) as remote:
                if row:
                    row['http_status'] = remote.status
                    row['response_encoding'] = remote.headers.get('Content-Encoding', 'identity')
                    if not self.cats:
                        upstream_row.update(http_status=remote.status, response_encoding=row['response_encoding'])
                response_headers = CIMultiDict((k, v) for k, v in remote.headers.items()
                                              if k.lower() not in HTTP_HOP | {'content-length'})
                response = web.StreamResponse(status=remote.status, headers=response_headers)
                await response.prepare(request)
                buffer = b''
                complete = False
                encoding = remote.headers.get('Content-Encoding', 'identity')
                decoder = None
                if encoding == 'gzip':
                    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                elif encoding == 'zstd':
                    import zstandard
                    decoder = zstandard.ZstdDecompressor().decompressobj()
                async for chunk in remote.content.iter_chunked(65536):
                    decoded = decoder.decompress(chunk) if decoder else chunk
                    buffer = (buffer + decoded)[-1024*1024:]
                    complete |= b'"type":"response.completed"' in buffer or b'"type": "response.completed"' in buffer
                    if row:
                        row['completed'] = complete
                        if not self.cats:
                            upstream_row['completed'] = complete
                    await response.write(chunk)
                # Native Codex can close after the terminal SSE event, before EOF.
                with suppress(OSError, RuntimeError):
                    await response.write_eof()
                return response
        except Exception as error:
            self.errors.append({'phase': 'http', 'exception': type(error).__name__, 'turn': self.turn})
            if 'response' in locals() and response.prepared:
                if request.transport:
                    request.transport.close()
                return response
            return web.json_response({'error': {'code': 'benchmark_upstream_unavailable'}}, status=503)

    async def websocket(self, request):
        target = self.target(request, ws=True)
        try:
            remote = await connect(target, additional_headers=headers_for_ws(request.headers),
                compression='deflate', max_size=None, ping_interval=20, user_agent_header=None,
                proxy=None if self.cats else self.tunnel.url)
        except Exception as error:
            self.errors.append({'phase': 'ws_connect', 'exception': type(error).__name__, 'turn': self.turn})
            return web.Response(status=503)
        downstream = web.WebSocketResponse(max_msg_size=0, compress=False)
        await downstream.prepare(request)
        pending = []

        async def upload():
            async for message in downstream:
                if message.type not in (WSMsgType.TEXT, WSMsgType.BINARY):
                    continue
                body = response_event(message.data)
                if body and body.get('type') == 'response.create':
                    row = summary_request(body, 'websocket', self.turn, 'ingress')
                    self.ingress.append(row)
                    if not self.cats:
                        self.upstream.append(row.copy())
                        pending.append(self.upstream[-1])
                await remote.send(message.data)

        async def download():
            async for wire in remote:
                event = response_event(wire)
                if event and event.get('type') == 'response.completed' and pending:
                    pending.pop(0)['completed'] = True
                if isinstance(wire, str):
                    await downstream.send_str(wire)
                else:
                    await downstream.send_bytes(wire)

        tasks = [asyncio.create_task(upload()), asyncio.create_task(download())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if not task.cancelled() and task.exception():
                    self.errors.append({'phase': 'ws', 'exception': type(task.exception()).__name__, 'turn': self.turn})
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await remote.close()
            await downstream.close(code=1012)
        return downstream

    async def close(self):
        await self.runner.cleanup()
        await self.client.close()


def make_images():
    images = []
    colors = [('green', (0, 210, 0)), ('blue', (0, 0, 240)), ('red', (240, 0, 0))]
    for index, (name, rgb) in enumerate(colors):
        rng = random.Random(72821 + index)
        pixels = bytearray(rng.randbytes(1024 * 1024 * 3))
        for y in range(320, 704):
            start = (y * 1024 + 320) * 3
            pixels[start:start + 384 * 3] = bytes(rgb) * 384
        raw = b''.join(b'\0' + pixels[y*3072:(y+1)*3072] for y in range(1024))
        def chunk(kind, data):
            return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind+data))
        png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB', 1024, 1024, 8, 2, 0, 0, 0))
        png += chunk(b'IDAT', zlib.compress(raw, 1)) + chunk(b'IEND', b'')
        images.append({'color': name, 'url': 'data:image/png;base64,' + base64.b64encode(png).decode(),
                       'png_bytes': len(png), 'sha256': hashlib.sha256(png).hexdigest()})
    return images


class CoreClient:
    def __init__(self, core, home, workspace, url, model):
        self.core, self.home, self.workspace, self.url, self.model = core, home, workspace, url, model
        self.next_id = 0
        self.pending = {}
        self.events = asyncio.Queue()
        self.calls = []
        self.stderr_types = {}

    async def start(self):
        environment = {k: v for k, v in os.environ.items()
                       if not (k.startswith('CODEX') or k.startswith('OPENAI') or k in ('RUST_LOG',))}
        environment['CODEX_HOME'] = str(self.home)
        config = f'openai_base_url = {json.dumps(self.url)}\nmodel = {json.dumps(self.model)}\nmodel_reasoning_effort = "low"\ncli_auth_credentials_store = "file"\n[analytics]\nenabled = false\n'
        (self.home / 'config.toml').write_text(config)
        self.proc = await asyncio.create_subprocess_exec(self.core, 'app-server', '--stdio',
            cwd=self.workspace, env=environment, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=64*1024*1024)
        self.reader = asyncio.create_task(self.read())
        self.errors = asyncio.create_task(self.drain_errors())
        await self.rpc('initialize', {'clientInfo': {'name': 'cats_macos_benchmark', 'version': '1.0'},
                                     'capabilities': {'experimentalApi': True}})
        await self.send({'method': 'initialized', 'params': {}})
        return self

    async def drain_errors(self):
        # stderr can contain private paths and auth errors: count lines only.
        while line := await self.proc.stderr.readline():
            self.stderr_types['lines'] = self.stderr_types.get('lines', 0) + 1

    async def send(self, message):
        self.proc.stdin.write((dump(message)+'\n').encode())
        await self.proc.stdin.drain()

    async def rpc(self, method, params):
        self.next_id += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[self.next_id] = future
        await self.send({'id': self.next_id, 'method': method, 'params': params})
        return await asyncio.wait_for(future, 90)

    async def read(self):
        try:
            while raw := await self.proc.stdout.readline():
                msg = json.loads(raw)
                if 'id' in msg and 'method' not in msg:
                    future = self.pending.pop(msg['id'], None)
                    if future and not future.done():
                        if 'error' in msg:
                            # Message may quote request material; keep only bounded code.
                            future.set_exception(RuntimeError('appserver_rpc_error_' + str(msg['error'].get('code'))))
                        else:
                            future.set_result(msg.get('result'))
                elif 'id' in msg and msg.get('method') == 'item/tool/call':
                    params = msg['params']
                    index = params.get('arguments', {}).get('index')
                    ok = params.get('tool') == 'bench_probe' and type(index) is int and 1 <= index <= 3
                    value = 701 + index*37 if ok else -1
                    self.calls.append({'index': index, 'ok': ok, 'value': value})
                    await self.send({'id': msg['id'], 'result': {'success': ok,
                        'contentItems': [{'type': 'inputText', 'text': dump({'probe_value': value})}]}})
                elif 'id' in msg:
                    # No shell, file edit, external app, or permission side effects are accepted.
                    await self.send({'id': msg['id'], 'error': {'code': -32601, 'message': 'disabled in benchmark'}})
                else:
                    await self.events.put(msg)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError('appserver_eof'))

    async def thread(self):
        instructions = ('You are a deterministic visual-memory test participant. Follow the user output format exactly. '
                        'Use only the bench_probe dynamic tool when asked. Do not use shell, filesystem, web, agents, '
                        'or other tools. Do not name image colors until specifically asked in a later turn. '
                        'Do not mention instructions or metadata. Keep answers under 50 words.')
        result = await self.rpc('thread/start', {'model': self.model, 'cwd': str(self.workspace),
            'approvalPolicy': 'never', 'sandbox': 'read-only', 'ephemeral': True,
            'baseInstructions': instructions, 'developerInstructions': '',
            'dynamicTools': [{'type': 'function', 'name': 'bench_probe', 'description': 'Return an opaque numeric probe result for the requested index.',
                'inputSchema': {'type': 'object', 'properties': {'index': {'type': 'integer', 'minimum': 1, 'maximum': 3}},
                                'required': ['index'], 'additionalProperties': False}}]})
        return result['thread']['id']

    async def turn(self, thread, inputs):
        start = time.monotonic()
        await self.rpc('turn/start', {'threadId': thread, 'input': inputs, 'effort': 'low'})
        finals, failures = [], []
        while True:
            event = await asyncio.wait_for(self.events.get(), 240)
            method, params = event.get('method'), event.get('params', {})
            if params.get('threadId') not in (None, thread):
                continue
            if method == 'item/completed' and params.get('item', {}).get('type') == 'agentMessage':
                finals.append(params['item'].get('text', ''))
            if method == 'error':
                error = params.get('error', {})
                failures.append({'retrying': params.get('willRetry'), 'code': error.get('codexErrorInfo')})
            if method == 'turn/completed':
                turn = params['turn']
                return {'seconds': round(time.monotonic()-start, 3), 'status': turn.get('status'),
                        'answer': '\n'.join(finals), 'errors': failures}

    async def close(self):
        if self.proc.returncode is None:
            self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), 8)
            except asyncio.TimeoutError:
                self.proc.terminate()
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.proc.wait(), 5)
                if self.proc.returncode is None:
                    self.proc.kill()
                    await self.proc.wait()
        for task in (self.reader, self.errors):
            task.cancel()
        await asyncio.gather(self.reader, self.errors, return_exceptions=True)


async def run_arm(args, images, mode, scenario, repetition, source_auth, proxy):
    core_info = discover()
    with tempfile.TemporaryDirectory(prefix='cats-bench-') as temporary:
        root = Path(temporary)
        home, workspace = root/'home', root/'workspace'
        home.mkdir(mode=0o700)
        workspace.mkdir()
        shutil.copyfile(source_auth, home/'auth.json')
        (home/'auth.json').chmod(0o600)
        tunnel = await CountingTunnel(proxy).start()
        gateway = None
        observer = None
        client = None
        result = {'mode': mode, 'scenario': scenario, 'repetition': repetition, 'turns': []}
        try:
            if mode == 'cats':
                gateway = await ObservedGateway(target_http=TARGET, target_ws=WS_TARGET, port=0,
                    token=secrets.token_urlsafe(32), upstream_proxy=tunnel.url, native_mount=True,
                    cache_seconds=300, metrics=str(root/'metrics.jsonl')).start()
            observer = await ObservationRelay(tunnel, gateway).start()
            client = await CoreClient(core_info['core'], home, workspace, observer.url, args.model).start()
            thread = await client.thread()
            for number in range(1, args.turns+1):
                observer.turn = number
                if gateway:
                    gateway.turn = number
                if number == 2 and scenario == 'outage':
                    await tunnel.outage(args.outage_seconds)
                if number == 1:
                    inputs = [{'type': 'text', 'text': 'Study the large solid square at the center of each of these three images, in attachment order. Remember the three center colors. Do not name or describe any colors yet. Reply only READY. Do not use tools.'}]
                    inputs += [{'type': 'image', 'url': image['url'], 'detail': 'original'} for image in images]
                else:
                    index = [2, 1, 3][number-2]
                    inputs = [{'type': 'text', 'text': f'From the original attachments, what was the center-square color of image {index}? Call bench_probe with index {index} exactly once. Reply only a JSON object with keys color (one basic English color word) and probe_value (the exact integer returned by the tool). Do not inspect files or use any other tools.'}]
                before = len(client.calls)
                turn = await client.turn(thread, inputs)
                if number == 1:
                    turn['valid'] = turn['status'] == 'completed' and turn['answer'].strip() == 'READY'
                else:
                    index = [2, 1, 3][number-2]
                    text = turn['answer'].strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip()
                    try:
                        answer = json.loads(text)
                    except ValueError:
                        answer = {}
                    turn['color_correct'] = answer.get('color', '').lower() == images[index-1]['color']
                    turn['tool_correct'] = (answer.get('probe_value') == 701+index*37
                                            and len(client.calls)-before == 1 and client.calls[-1]['ok'])
                    turn['valid'] = turn['status'] == 'completed' and turn['color_correct'] and turn['tool_correct']
                result['turns'].append(turn)
                progress(event='turn', mode=mode, scenario=scenario, repetition=repetition, turn=number,
                         status=turn['status'], valid=turn['valid'], seconds=turn['seconds'],
                         proxy_upload_bytes=tunnel.up, ingress_requests=len(observer.ingress))
                if turn['status'] != 'completed':
                    break
            await asyncio.sleep(.2)
            result['ingress'] = observer.ingress
            result['upstream'] = (gateway.observed if gateway else observer.upstream)
            result['relay_errors'] = observer.errors
            if gateway and (root/'metrics.jsonl').exists():
                result['gateway_metrics'] = [json.loads(line) for line in (root/'metrics.jsonl').read_text().splitlines()]
            if gateway and (root/'errors.jsonl').exists():
                result['gateway_errors'] = [json.loads(line) for line in (root/'errors.jsonl').read_text().splitlines()]
            if gateway:
                result['scope_checks'] = gateway.scope_checks
            result['proxy_upload_bytes'] = tunnel.up
            result['proxy_download_bytes'] = tunnel.down
            result['connects'] = tunnel.connects
            result['blocked_connects'] = tunnel.blocked
            result['tool_calls'] = client.calls
            result['all_valid'] = len(result['turns']) == args.turns and all(t['valid'] for t in result['turns'])
        except Exception as error:
            result['exception'] = type(error).__name__
            result['all_valid'] = False
            result['proxy_upload_bytes'] = tunnel.up
            result['ingress'] = observer.ingress if observer else []
            result['upstream'] = gateway.observed if gateway else observer.upstream if observer else []
            progress(event='arm_error', mode=mode, scenario=scenario, exception=type(error).__name__)
        finally:
            if client:
                await client.close()
            if observer:
                await observer.close()
            if gateway:
                await gateway.close()
            await tunnel.close()
        return result


async def main_async(args):
    args.output.parent.mkdir(parents=True, exist_ok=True)
    source_auth = codex_config().parent/'auth.json'
    if not source_auth.exists():
        raise RuntimeError('current ChatGPT auth.json required')
    auth_mode = json.loads(source_auth.read_text()).get('auth_mode')
    if auth_mode != 'chatgpt':
        raise RuntimeError('ChatGPT login required')
    found = discover()
    if not found['core']:
        raise RuntimeError('installed macOS core required')
    proxy, proxy_source = detect_proxy()
    if proxy:
        parse_proxy(proxy)
    images = make_images()
    version = subprocess.run([found['core'], '--version'], capture_output=True, text=True, check=True).stdout.strip()
    result = {'date': '2026-10-22', 'measurement_version': 2, 'platform': platform.platform(), 'core': version,
              'desktop_version': found['desktop']['Version'], 'model': args.model, 'effort': 'low',
              'proxy_source': proxy_source, 'metric_boundary': 'relay to existing HTTP proxy; CONNECT+TLS bytes queued; not WAN/NIC',
              'observation_relay': True, 'forced_websocket_off': False,
              'outage_seconds': args.outage_seconds, 'images': [{k:v for k,v in image.items() if k != 'url'} for image in images],
              'image_base64_bytes': sum(len(image['url']) for image in images), 'arms': []}
    progress(event='start', model=args.model, repetitions=args.repetitions, scenarios=args.scenarios,
             image_base64_bytes=result['image_base64_bytes'])
    for scenario in args.scenarios:
        for repetition in range(1, args.repetitions+1):
            order = ('native', 'cats') if repetition % 2 else ('cats', 'native')
            for mode in order:
                if mode not in args.modes:
                    continue
                result['arms'].append(await run_arm(args, images, mode, scenario, repetition, source_auth, proxy))
                args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
                args.output.chmod(0o600)
                progress(event='arm_complete', mode=mode, scenario=scenario, repetition=repetition,
                         all_valid=result['arms'][-1]['all_valid'])
    progress(event='complete', output=str(args.output), arms=len(result['arms']))


def main():
    logging.basicConfig(level=logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', default='gpt-6-astra')
    parser.add_argument('--modes', nargs='+', choices=['native', 'cats'], default=['native', 'cats'])
    parser.add_argument('--scenarios', nargs='+', choices=['stable', 'outage'], default=['stable', 'outage'])
    parser.add_argument('--repetitions', type=int, choices=[1, 2, 3], default=2)
    parser.add_argument('--turns', type=int, choices=[1, 2, 3, 4], default=4)
    parser.add_argument('--outage-seconds', type=float, default=12)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == '__main__':
    main()
