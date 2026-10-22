"""Persistent direct-HTTPS deployment behind a loopback reverse proxy."""
import argparse
import asyncio
from contextlib import nullcontext
from pathlib import Path

import uvicorn

from server.dev import create_dev_app
from server.tunnel import device_gateway


def create_hosted_app(directory, origin, monitor=True):
    return create_dev_app(Path(directory), '127.0.0.1', monitor=monitor,
                          public_mode=True, direct_origin=origin)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', required=True, help='Public HTTPS root, including an IP address')
    parser.add_argument('--state-dir', required=True, type=Path)
    args = parser.parse_args()
    app = create_hosted_app(args.state_dir, args.origin)

    class HostedServer(uvicorn.Server):
        gateway = task = None

        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if not self.started:
                return
            self.gateway = uvicorn.Server(uvicorn.Config(
                device_gateway(app), host='127.0.0.1', port=8001, lifespan='off',
                proxy_headers=False, access_log=False))
            self.gateway.capture_signals = nullcontext
            self.task = asyncio.create_task(self.gateway.serve())
            while not self.gateway.started:
                if self.task.done():
                    await self.task
                    raise RuntimeError('Public gateway failed to start')
                await asyncio.sleep(0.05)

        async def main_loop(self):
            while not self.should_exit:
                if self.task.done():
                    await self.task
                    raise RuntimeError('Public gateway stopped unexpectedly')
                await asyncio.sleep(0.2)

        async def shutdown(self, sockets=None):
            if self.gateway:
                self.gateway.should_exit = True
            if self.task:
                await self.task
            await super().shutdown(sockets=sockets)

    HostedServer(uvicorn.Config(app, host='127.0.0.1', port=8000,
                                proxy_headers=False, access_log=False)).run()


if __name__ == '__main__':
    main()
