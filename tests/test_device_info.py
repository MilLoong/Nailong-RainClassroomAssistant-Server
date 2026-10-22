import sqlite3

import httpx
from fastapi.testclient import TestClient

from server.app import DeviceInfo, Store, push_plan
from server.hosted import create_hosted_app
from server.tunnel import device_gateway
from tests.test_server import system, register


def test_metadata_upload_requires_device_auth_and_preserves_isolation(system):
    client, store = system
    device, auth = register(client)
    other, _ = register(client, 'other')
    info = {'manufacturer': 'Xiaomi', 'brand': 'Redmi', 'model': 'Test model',
            'android_release': '15', 'sdk_int': 35, 'app_version': 'dev.7'}
    assert client.put('/v1/device/info', json=info).status_code in (401, 403)
    response = client.put('/v1/device/info', json=info, headers=auth)
    assert response.status_code == 200
    assert response.json()['push'] == {'transport': 'polling', 'candidate_provider': 'xiaomi',
                                       'configured': False, 'reason': 'sdk_token_and_provider_credentials_required'}
    rows = client.get('/v1/admin/devices', headers={'Authorization': 'Bearer ' + 'a' * 32}).json()
    current = next(row for row in rows if row['id'] == device)
    assert current['device_info']['model'] == 'Test model'
    assert next(row for row in rows if row['id'] == other)['device_info'] == {}
    assert not {'credential', 'token'} & current.keys()
    assert client.put('/v1/device/info', headers=auth, json={'model': 'x' * 121}).status_code == 422
    assert client.put('/v1/device/info', headers=auth, json={'imei': 'not_collected'}).status_code == 422
    client.delete('/v1/device', headers=auth)
    assert client.put('/v1/device/info', json=info, headers=auth).status_code == 401


def test_binding_can_include_metadata_without_enabling_fake_push(system):
    client, _ = system
    code = client.post('/v1/admin/pairings', headers={'Authorization': 'Bearer ' + 'a' * 32}).json()['code']
    result = client.post('/v1/pair', json={'code': code, 'name': 'phone',
                                        'device_info': {'brand': 'HONOR', 'sdk_int': 35}})
    assert result.status_code == 200
    assert result.json()['push']['candidate_provider'] == 'honor'
    assert result.json()['push']['configured'] is False


def test_metadata_migration_keeps_existing_binding(tmp_path):
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE devices(id TEXT PRIMARY KEY, token TEXT UNIQUE, name TEXT, '
                   'credential TEXT, revoked INTEGER DEFAULT 0, status TEXT)')
        db.execute("INSERT INTO devices VALUES('id','hashed','old',NULL,0,'listening')")
    store = Store(str(path))
    with store.db() as db:
        row = db.execute('SELECT * FROM devices').fetchone()
        assert row['device_info'] == '{}'
        assert row['token'] == 'hashed'
    assert push_plan(DeviceInfo().model_dump())['candidate_provider'] is None


def test_direct_gateway_allows_only_authenticated_metadata_update(tmp_path):
    import asyncio
    app = create_hosted_app(tmp_path, 'https://121.4.26.119', monitor=False)
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=device_gateway(app)),
                                     base_url='https://121.4.26.119') as client:
            assert (await client.put('/v1/device/info', json={})).status_code in (401, 403)
            assert (await client.get('/v1/admin/devices')).status_code == 404
    asyncio.run(check())
