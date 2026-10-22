"""Temporary HTTPS gateway; never publish the local QR/admin interface."""
import asyncio
import os
import re
import socket
import subprocess
from contextlib import nullcontext, suppress

import uvicorn
from fastapi.responses import JSONResponse


def device_gateway(app):
    async def gateway(scope, receive, send):
        if scope['type'] == 'websocket':
            await send({'type': 'websocket.close', 'code': 1008})
            return
        if scope['type'] != 'http':
            return
        path = scope.get('path', '')
        allowed = path in ('/health', '/v1/pair', '/v1/device', '/v1/device/info', '/v1/device/credentials', '/v1/device/tasks/next')
        allowed = allowed or (path.startswith('/v1/device/tasks/') and path.endswith('/result'))
        if not allowed:
            response = JSONResponse({'detail': 'not_found'}, status_code=404)
            await response(scope, receive, send)
            return
        # Only device routes reach here; do not depend on a proxy's Host rewrite policy.
        if app.state.public_host:
            scope = dict(scope)
            scope['headers'] = [(key, value) for key, value in scope.get('headers', []) if key.lower() != b'host']
            scope['headers'].append((b'host', app.state.public_host.encode('ascii')))
        await app(scope, receive, send)
    return gateway


def quick_tunnel_url(line):
    match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com(?=[\s"<>|]|$)', line)
    return match.group(0) if match else None


class QuickTunnel:
    def __init__(self, app, executable):
        self.app = app
        self.executable = executable
        self.process = self.gateway_server = self.gateway_task = self.reader_task = self.sock = None

    async def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(128)
        self.sock.setblocking(False)
        port = self.sock.getsockname()[1]
        self.gateway_server = uvicorn.Server(uvicorn.Config(device_gateway(self.app), host='127.0.0.1', port=port,
                                                           lifespan='off', access_log=False, proxy_headers=False,
                                                           log_level='warning'))
        # The main server owns signal handling; the internal gateway must not replace it.
        self.gateway_server.capture_signals = _no_signals
        self.gateway_task = asyncio.create_task(self.gateway_server.serve(sockets=[self.sock]))
        while not self.gateway_server.started:
            if self.gateway_task.done():
                await self.gateway_task
                raise RuntimeError('Internal gateway failed to start')
            await asyncio.sleep(0.05)
        self.process = await asyncio.create_subprocess_exec(
            self.executable, 'tunnel', '--no-autoupdate', '--url', f'http://127.0.0.1:{port}',
            '--protocol', 'http2', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)

        async def find_url():
            while line := await self.process.stdout.readline():
                url = quick_tunnel_url(line.decode(errors='replace'))
                if url:
                    return url
            raise RuntimeError('cloudflared exited before providing a public URL')

        try:
            url = await asyncio.wait_for(find_url(), timeout=90)
        except BaseException:
            await self.close()
            raise
        self.reader_task = asyncio.create_task(self.drain())
        return url

    async def drain(self):
        while await self.process.stdout.readline():
            pass
        self.app.state.tunnel_error = '隧道进程已退出，请检查网络并重启 --public 测试入口'
        self.app.state.public_origin = None

    async def close(self):
        if self.process and self.process.returncode is None:
            with suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.reader_task:
            self.reader_task.cancel()
            try:
                await self.reader_task
            except asyncio.CancelledError:
                pass
        if self.gateway_server:
            self.gateway_server.should_exit = True
        if self.gateway_task:
            try:
                await asyncio.wait_for(self.gateway_task, 10)
            except asyncio.TimeoutError:
                pass
        if self.sock:
            self.sock.close()


def _no_signals():
    return nullcontext()
