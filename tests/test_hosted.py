import asyncio
import json

import httpx
from fastapi.testclient import TestClient

from server.hosted import create_hosted_app
from server.tunnel import device_gateway


def test_direct_ip_pairing_keeps_admin_private_and_keys_stable(tmp_path, monkeypatch):
    import qrcode
    original = qrcode.make
    payloads = []

    def capture(value, **kwargs):
        payloads.append(json.loads(value))
        return original(value, **kwargs)

    monkeypatch.setattr(qrcode, 'make', capture)
    origin = 'https://121.4.26.119'
    app = create_hosted_app(tmp_path, origin, monitor=False)
    with TestClient(app, base_url='http://127.0.0.1:8000', client=('127.0.0.1', 5000)) as client:
        page = client.get('/dev/pair')
        assert '云服务器直连' in page.text
        assert 'Cloudflare' not in page.text
        assert '临时地址会变化' not in page.text
        assert payloads[0]['server'] == origin
        assert payloads[0]['local_dev'] is False
    keys = (tmp_path / 'keys.json').read_bytes()
    create_hosted_app(tmp_path, origin, monitor=False)
    assert (tmp_path / 'keys.json').read_bytes() == keys

    async def verify():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=device_gateway(app)),
                                     base_url=origin) as remote:
            assert (await remote.get('/health')).json() == {'status': 'ok'}
            for path in ['/dev/pair', '/v1/admin/devices', '/docs', '/openapi.json']:
                assert (await remote.get(path)).status_code == 404
            result = await remote.post('/v1/pair', json={'code': payloads[0]['code'], 'name': 'IP phone'})
            assert result.status_code == 200
            token = result.json()['device_token']
            assert (await remote.get('/v1/device/tasks/next?wait=0',
                                     headers={'Authorization': 'Bearer ' + token})).json() == {'task': None}

    asyncio.run(verify())
