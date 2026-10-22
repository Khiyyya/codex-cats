"""Real loopback HTTP/WS tests with a synthetic upstream, never an OpenAI account."""
import json
import tempfile
from pathlib import Path
import unittest

from aiohttp import ClientSession, web
from websockets.asyncio.server import serve

from uplink12.gateway import Gateway


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.seen = []
        self.sockets = []
        self.reject_delta = False
        self.output = {'type': 'message', 'role': 'assistant', 'status': 'completed',
                       'content': [{'type': 'output_text', 'text': 'synthetic answer'}]}

        async def upstream(socket):
            self.sockets.append(socket)
            async for raw in socket:
                request = json.loads(raw)
                self.seen.append(request)
                if self.reject_delta and request.get('previous_response_id'):
                    await socket.send(json.dumps({'type': 'error', 'error': {'code': 'previous_response_not_found'}}))
                    continue
                await socket.send(json.dumps({'type': 'response.completed', 'response': {
                    'id': f'resp_{len(self.seen)}', 'status': 'completed', 'output': [self.output]}}))

        self.server = await serve(upstream, '127.0.0.1', 0)
        port = self.server.sockets[0].getsockname()[1]
        self.metrics = Path(self.temporary.name) / 'metrics.jsonl'
        self.gateway = await Gateway(target_ws=f'ws://127.0.0.1:{port}/responses',
                                     target_http=f'http://127.0.0.1:{port}/responses',
                                     token='synthetic-local-token-only', port=0,
                                     native_mount=True, metrics=str(self.metrics)).start()
        self.url = f'http://127.0.0.1:{self.gateway.listen_port()}/uplink/synthetic-local-token-only/responses'
        self.client = ClientSession(headers={'Authorization': 'Bearer synthetic-test-only'})
        self.first = {'model': 'test', 'stream': True, 'store': False,
                      'client_metadata': {'thread_id': 'test-thread', 'x-codex-window-id': 'test-window'},
                      'input': [{'role': 'user', 'content': [
                          {'type': 'input_text', 'text': 'remember synthetic image'},
                          {'type': 'input_image', 'image_url': 'data:image/png;base64,c3ludGhldGlj'}]}]}

    async def asyncTearDown(self):
        await self.client.close()
        await self.gateway.close()
        self.server.close()
        await self.server.wait_closed()
        self.temporary.cleanup()

    def continuation(self):
        return {**self.first, 'input': self.first['input'] + [self.output,
                {'role': 'user', 'content': 'next synthetic turn'}]}

    async def post(self, body):
        async with self.client.post(self.url, json=body) as response:
            text = await response.text()
            self.assertEqual(response.status, 200, text)
            self.assertIn('response.completed', text)

    async def test_http_full_becomes_delta_on_live_socket(self):
        await self.post(self.first)
        await self.post(self.continuation())
        self.assertEqual(self.seen[0]['input'], self.first['input'])
        self.assertEqual(self.seen[1]['previous_response_id'], 'resp_1')
        self.assertEqual(self.seen[1]['input'], [self.continuation()['input'][-1]])
        self.assertNotIn('synthetic answer', self.metrics.read_text())
        self.assertNotIn('synthetic-test-only', self.metrics.read_text())

    async def test_closed_upstream_requires_full_history(self):
        await self.post(self.first)
        await self.sockets[0].close()
        await self.post(self.continuation())
        self.assertNotIn('previous_response_id', self.seen[1])
        self.assertEqual(self.seen[1]['input'], self.continuation()['input'])

    async def test_explicit_delta_rejection_retries_full_once(self):
        await self.post(self.first)
        self.reject_delta = True
        await self.post(self.continuation())
        self.assertEqual(len(self.seen), 3)
        self.assertIn('previous_response_id', self.seen[1])
        self.assertNotIn('previous_response_id', self.seen[2])
        self.assertEqual(self.seen[2]['input'], self.continuation()['input'])

    async def test_different_window_does_not_reuse_history(self):
        await self.post(self.first)
        body = self.continuation()
        body['client_metadata'] = {**body['client_metadata'], 'x-codex-window-id': 'other-window'}
        await self.post(body)
        self.assertNotIn('previous_response_id', self.seen[1])

    async def test_control_requires_local_token(self):
        url = self.url.split('/uplink/')[0] + '/_uplink/health'
        async with self.client.get(url) as response:
            self.assertEqual(response.status, 403)
        async with self.client.get(url, headers={'x-uplink-token': 'synthetic-local-token-only'}) as response:
            self.assertEqual(response.status, 200)


if __name__ == '__main__':
    unittest.main()
