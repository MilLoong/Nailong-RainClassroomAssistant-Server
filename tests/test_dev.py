import json

import httpx
import pytest
from fastapi.testclient import TestClient

from server.dev import create_dev_app, is_lan_ip, load_keys


def test_local_keys_are_generated_once(tmp_path):
    first = load_keys(tmp_path / 'local')
    assert len(first['admin']) >= 32
    assert load_keys(tmp_path / 'local') == first
    assert 'ADMIN_TOKEN' not in first


@pytest.mark.parametrize('ip', ['8.8.8.8', '0.0.0.0', '172.32.0.1', '192.169.1.1'])
def test_public_address_cannot_be_advertised(tmp_path, ip):
    assert not is_lan_ip(ip)
    with pytest.raises(ValueError):
        create_dev_app(tmp_path, ip, monitor=False)


def test_local_page_provides_working_qr_without_manual_token(tmp_path, monkeypatch):
    import qrcode
    make = qrcode.make
    payloads = []

    def capture(payload, **kwargs):
        payloads.append(json.loads(payload))
        return make(payload, **kwargs)

    monkeypatch.setattr(qrcode, 'make', capture)
    app = create_dev_app(tmp_path, '192.168.1.12', monitor=False)
    with TestClient(app, base_url='http://127.0.0.1:8000', client=('127.0.0.1', 2000)) as client:
        page = client.get('/dev/pair')
        assert page.status_code == 200
        assert '<svg' in page.text
        assert page.headers['cache-control'] == 'no-store'
        qr = payloads[0]
        assert qr['server'] == 'http://192.168.1.12:8000'
        assert qr['local_dev'] is True
        assert 'admin' not in qr
        paired = client.post('/v1/pair', json={'code': qr['code'], 'name': 'local phone'})
        assert paired.status_code == 200
        assert client.post('/v1/pair', json={'code': qr['code'], 'name': 'phone'}).status_code == 400


def test_lan_phone_cannot_read_qr_or_admin_routes(tmp_path):
    app = create_dev_app(tmp_path, '192.168.1.12', monitor=False)
    keys = load_keys(tmp_path)
    with TestClient(app, base_url='http://192.168.1.12:8000', client=('192.168.1.50', 2000)) as client:
        assert client.get('/health').status_code == 200
        assert client.get('/dev/pair').status_code == 403
        assert client.post('/v1/admin/pairings', headers={'Authorization': 'Bearer ' + keys['admin']}).status_code == 403
        assert client.get('/dev/pair', headers={'X-Forwarded-For': '127.0.0.1'}).status_code == 403


def test_dev_mode_rejects_public_peer_and_unrecognized_host(tmp_path):
    app = create_dev_app(tmp_path, '192.168.1.12', monitor=False)
    with TestClient(app, base_url='http://192.168.1.12:8000', client=('8.8.8.8', 2000)) as client:
        assert client.get('/health').status_code == 403
    with TestClient(app, base_url='http://127.0.0.1:8000', client=('127.0.0.1', 2000)) as client:
        assert client.get('/dev/pair', headers={'Host': 'attacker.example:8000'}).status_code == 403
