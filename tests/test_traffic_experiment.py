"""Check the measurement boundary with known bytes, not a live account."""
import asyncio
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'experiments'))
from traffic_macos import CountingTunnel, ObservationRelay, summary_request
from aiohttp import ClientSession, web
import zstandard


class MeasurementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tasks = set()
        async def proxy(reader, writer):
            self.tasks.add(asyncio.current_task())
            try:
                await reader.readuntil(b'\r\n\r\n')
                writer.write(b'HTTP/1.1 200 OK\r\n\r\n')
                await writer.drain()
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
                self.tasks.discard(asyncio.current_task())
        self.proxy = await asyncio.start_server(proxy, '127.0.0.1', 0)
        self.tunnel = await CountingTunnel(f'http://127.0.0.1:{self.proxy.sockets[0].getsockname()[1]}').start()
        self.header = b'CONNECT chatgpt.com:443 HTTP/1.1\r\nHost: chatgpt.com:443\r\n\r\n'

    async def asyncTearDown(self):
        await self.tunnel.close()
        self.proxy.close()
        await self.proxy.wait_closed()
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    async def test_connect_and_payload_are_counted_exactly_once(self):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.tunnel.server.sockets[0].getsockname()[1])
        writer.write(self.header)
        await writer.drain()
        reply = await reader.readuntil(b'\r\n\r\n')
        payload = bytes(range(256))*4096
        writer.write(payload)
        await writer.drain()
        echoed = await reader.readexactly(len(payload))
        self.assertEqual(echoed, payload)
        self.assertEqual(self.tunnel.up, len(self.header)+len(payload))
        self.assertEqual(self.tunnel.down, len(reply)+len(payload))
        writer.close()
        await writer.wait_closed()

    async def test_blocked_connect_does_not_count_as_upload(self):
        await self.tunnel.outage(.5)
        reader, writer = await asyncio.open_connection('127.0.0.1', self.tunnel.server.sockets[0].getsockname()[1])
        writer.write(self.header)
        await writer.drain()
        reply = await reader.readuntil(b'\r\n\r\n')
        self.assertIn(b'503', reply)
        self.assertEqual(self.tunnel.up, 0)
        self.assertEqual(self.tunnel.connects, 0)
        self.assertEqual(self.tunnel.blocked, 1)
        writer.close()
        await writer.wait_closed()

    async def test_non_official_target_is_rejected(self):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.tunnel.server.sockets[0].getsockname()[1])
        writer.write(self.header.replace(b'chatgpt.com', b'example.com'))
        await writer.drain()
        self.assertIn(b'403', await reader.readuntil(b'\r\n\r\n'))
        self.assertEqual(self.tunnel.up, 0)
        writer.close()
        await writer.wait_closed()


class SummaryTests(unittest.TestCase):
    def test_content_and_response_ids_are_not_saved(self):
        row = summary_request({'input': [{'text': 'PRIVATE_PAYLOAD'}],
            'previous_response_id': 'PRIVATE_RESPONSE_ID',
            'client_metadata': {'thread_id': 'PRIVATE_THREAD', 'x-codex-window-id': 'PRIVATE_WINDOW'}},
            'websocket', 1, 'ingress')
        rendered = json.dumps(row)
        self.assertNotIn('PRIVATE_', rendered)
        self.assertTrue(row['has_previous_response_id'])
        self.assertTrue(row['identity_present'])


class HttpObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_compressed_sse_is_observed_without_changing_wire_body(self):
        event = b'data: {"type":"response.completed","response":{"id":"synthetic"}}\n\n'
        wire = zstandard.ZstdCompressor().compress(event)
        async def upstream(request):
            return web.Response(body=wire, headers={'Content-Encoding': 'zstd', 'Content-Type': 'text/event-stream'})
        app = web.Application()
        app.router.add_post('/{tail:.*}', upstream)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        class LocalGateway:
            token = 'synthetic'
            def listen_port(self):
                return port
        observer = await ObservationRelay(None, LocalGateway()).start()
        try:
            async with ClientSession(auto_decompress=False) as client:
                async with client.post(observer.url + '/responses', json={'input': []}) as response:
                    self.assertEqual(await response.read(), wire)
                    self.assertEqual(response.headers['Content-Encoding'], 'zstd')
            self.assertTrue(observer.ingress[0]['completed'])
            self.assertEqual(observer.ingress[0]['http_status'], 200)
        finally:
            await observer.close()
            await runner.cleanup()


if __name__ == '__main__':
    unittest.main()
