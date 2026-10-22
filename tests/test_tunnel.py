import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from server.dev import create_dev_app, set_public_origin
from server.tunnel import device_gateway, quick_tunnel_url, QuickTunnel


def test_tunnel_url_extraction_is_limited_to_official_domain():
    assert quick_tunnel_url('| https://small-quiet-test.trycloudflare.com |') == 'https://small-quiet-test.trycloudflare.com'
    assert quick_tunnel_url('https://test.trycloudflare.com.attacker.example') is None
    assert quick_tunnel_url('http://test.trycloudflare.com') is None
    assert quick_tunnel_url('https://example.com') is None


@pytest.mark.parametrize('origin', ['http://public.example', 'https://user:pass@public.example',
                                  'https://public.example/path', 'https://public.example/?token=x'])
def test_public_origin_requires_https_root(tmp_path, origin):
    app = create_dev_app(tmp_path, '127.0.0.1', monitor=False, public_mode=True)
    with pytest.raises(ValueError):
        set_public_origin(app, origin)


def test_public_qr_advertises_https_and_gateway_never_exposes_pairing_page(tmp_path, monkeypatch):
    import qrcode
    original = qrcode.make
    payloads = []

    def capture(payload, **kwargs):
        payloads.append(json.loads(payload))
        return original(payload, **kwargs)

    monkeypatch.setattr(qrcode, 'make', capture)
    app = create_dev_app(tmp_path, '127.0.0.1', monitor=False, public_mode=True)
    public = 'https://testing-example.trycloudflare.com'
    set_public_origin(app, public)
    with TestClient(app, base_url='http://127.0.0.1:8000', client=('127.0.0.1', 1000)) as local:
        assert local.get('/dev/pair').status_code == 200
    qr = payloads[0]
    assert qr['server'] == public
    assert qr['local_dev'] is False

    async def run():
        transport = httpx.ASGITransport(app=device_gateway(app), client=('127.0.0.1', 1001))
        async with httpx.AsyncClient(transport=transport, base_url=public) as remote:
            assert (await remote.get('/health')).status_code == 200
            assert (await remote.get('/health', headers={'Host': '127.0.0.1:41234'})).status_code == 200
            for path in ['/dev/pair', '/v1/admin/devices', '/docs', '/openapi.json']:
                assert (await remote.get(path, headers={'Host': '127.0.0.1:8000'})).status_code == 404
            assert (await remote.post('/v1/admin/pairings')).status_code == 404
            paired = await remote.post('/v1/pair', json={'code': qr['code'], 'name': 'remote phone'})
            assert paired.status_code == 200
            token = paired.json()['device_token']
            assert (await remote.get('/v1/device/tasks/next?wait=0', headers={'Authorization': 'Bearer ' + token})).json() == {'task': None}

    asyncio.run(run())


def test_pending_tunnel_does_not_generate_local_qr(tmp_path):
    app = create_dev_app(tmp_path, '127.0.0.1', monitor=False, public_mode=True)
    with TestClient(app, base_url='http://127.0.0.1:8000', client=('127.0.0.1', 1000)) as client:
        page = client.get('/dev/pair')
        assert page.status_code == 200
        assert '<svg' not in page.text
    with app.state.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM pairs').fetchone()[0] == 0


def test_tunnel_uses_separate_loopback_gateway_and_closes_process(tmp_path, monkeypatch):
    app = create_dev_app(tmp_path, '127.0.0.1', monitor=False, public_mode=True)
    captured = []

    class FakeProcess:
        returncode = None
        stdout = None
        stopped = False

        def terminate(self):
            self.stopped = True
            self.returncode = 0
            self.stdout.feed_eof()

        async def wait(self):
            return self.returncode

    async def run():
        process = FakeProcess()
        process.stdout = asyncio.StreamReader()
        process.stdout.feed_data(b'| https://integration-test.trycloudflare.com |\n')

        async def spawn(*args, **kwargs):
            captured.extend(args)
            return process

        monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
        tunnel = QuickTunnel(app, 'cloudflared')
        try:
            assert await tunnel.start() == 'https://integration-test.trycloudflare.com'
            target = captured[captured.index('--url') + 1]
            assert target.startswith('http://127.0.0.1:')
            assert not target.endswith(':8000')
        finally:
            await tunnel.close()
        assert process.stopped

    asyncio.run(run())
