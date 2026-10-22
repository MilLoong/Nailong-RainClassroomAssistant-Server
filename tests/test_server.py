import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import asyncio
import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from server.app import create_app
from server.app import digest, fetch_lessons


@pytest.fixture
def system(tmp_path):
    app = create_app(str(tmp_path / 'test.db'), 'a' * 32, Fernet.generate_key(), monitor=False)
    with TestClient(app) as client:
        yield client, app.state.store


def register(client, name='phone'):
    pairing = client.post('/v1/admin/pairings', headers={'Authorization': 'Bearer ' + 'a' * 32}).json()
    result = client.post('/v1/pair', json={'code': pairing['code'], 'name': name})
    assert result.status_code == 200
    assert client.post('/v1/pair', json={'code': pairing['code'], 'name': name}).status_code == 400
    data = result.json()
    return data['device_id'], {'Authorization': 'Bearer ' + data['device_token']}


def test_flow(system):
    client, store = system
    device, headers = register(client)
    assert client.put('/v1/device/credentials', headers=headers, json={'sessionid': 'secret'}).status_code == 200
    with store.db() as db:
        row = db.execute('SELECT * FROM devices').fetchone()
        assert 'secret' not in row['credential']
        assert row['token'] != headers['Authorization'][7:]
    lesson = {'lessonId': 123, 'classroomId': 7, 'courseName': 'Demo'}
    store.enqueue(device, lesson, delay=0)
    store.enqueue(device, lesson, delay=0)
    task = client.get('/v1/device/tasks/next?wait=0', headers=headers).json()['task']
    assert task['payload'] == lesson
    assert client.get('/v1/device/tasks/next?wait=0', headers=headers).json()['task'] is None
    body = {'lease_token': task['lease_token'], 'success': True}
    endpoint = '/v1/device/tasks/' + task['id'] + '/result'
    assert client.post(endpoint, headers=headers, json=body).status_code == 200
    assert client.post(endpoint, headers=headers, json=body).status_code == 200
    assert client.get('/v1/device/tasks/next?wait=0', headers=headers).json()['task'] is None


def test_isolation_revocation_and_stale_lease(system):
    client, store = system
    device, headers = register(client)
    _, other = register(client, 'other')
    store.enqueue(device, {'lessonId': 1}, delay=0)
    old = store.claim(device)
    with store.db() as db:
        db.execute('UPDATE jobs SET lease_until=0')
    new = store.claim(device)
    endpoint = '/v1/device/tasks/' + new['id'] + '/result'
    body = {'lease_token': old['lease_token'], 'success': True}
    assert client.post(endpoint, headers=other, json=body).status_code == 404
    assert client.post(endpoint, headers=headers, json=body).status_code == 409
    client.delete('/v1/admin/devices/' + device, headers={'Authorization': 'Bearer ' + 'a' * 32})
    assert client.get('/v1/device/tasks/next?wait=0', headers=headers).status_code == 401


def test_expiry_and_concurrent_claim(system):
    client, store = system
    device, _ = register(client)
    store.enqueue(device, {'lessonId': 1}, delay=0, ttl=-1)
    assert store.claim(device) is None
    store.enqueue(device, {'lessonId': 2}, delay=0)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.claim(device), range(4)))
    assert sum(result is not None for result in results) == 1
    with store.db() as db:
        db.execute('INSERT INTO pairs VALUES(?,?)', (digest('x' * 32), time.time() - 1))
    assert client.post('/v1/pair', json={'code': 'x' * 32, 'name': 'phone'}).status_code == 400
    assert client.post('/v1/admin/pairings').status_code in (401, 403)


def test_restart_preserves_tasks(system):
    client, store = system
    device, _ = register(client)
    store.enqueue(device, {'lessonId': 1}, delay=0)
    from server.app import Store
    assert Store(store.path).claim(device)['payload']['lessonId'] == 1


def test_device_can_revoke_itself(system):
    client, store = system
    device, headers = register(client)
    assert client.put('/v1/device/credentials', headers=headers, json={'sessionid': 'test'}).status_code == 200
    assert client.delete('/v1/device', headers=headers).status_code == 200
    assert client.get('/v1/device/tasks/next?wait=0', headers=headers).status_code == 401
    with store.db() as db:
        assert db.execute('SELECT credential FROM devices WHERE id=?', (device,)).fetchone()[0] is None


def test_upstream_adapter():
    lesson = {'lessonId': 123, 'classroomId': 1, 'courseName': 'Demo'}

    def handler(request):
        assert request.url.path == '/api/v3/classroom/on-lesson'
        assert request.headers['cookie'] == 'sessionid=test-session'
        return httpx.Response(200, json={'code': 0, 'data': {'onLessonClassrooms': [lesson]}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await fetch_lessons(client, 'test-session') == [lesson]
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={'code': 1, 'msg': 'UNAUTHENTICATED'}))) as client:
            with pytest.raises(ValueError):
                await fetch_lessons(client, 'expired')

    asyncio.run(run())


def test_mobile_upstream_adapter():
    def handler(request):
        assert request.headers['cookie'] == 'sid=mobile-session'
        assert request.headers['x-client'] == 'app'
        assert request.headers['xtbz'] == 'ykt'
        assert 'sessionid=' not in request.headers['cookie']
        return httpx.Response(200, json={'code': 0, 'data': {'onLessonClassrooms': []}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await fetch_lessons(client, 'mobile-session', 'mobile') == []

    asyncio.run(run())


def test_one_scan_pairing_configures_encrypted_monitor(system, monkeypatch):
    client, store = system
    calls = []

    async def verify(client, credential, kind):
        calls.append((credential, kind))
        return []

    monkeypatch.setattr('server.app.fetch_lessons', verify)
    code = client.post('/v1/admin/pairings', headers={'Authorization': 'Bearer ' + 'a' * 32}).json()['code']
    body = {'code': code, 'name': 'phone', 'monitor_sid': 'mobile-secret'}
    response = client.post('/v1/pair', json=body)
    assert response.status_code == 200
    data = response.json()
    assert data['protocol_version'] == 2
    assert data['monitor_configured'] is True
    assert calls == [('mobile-secret', 'mobile')]
    with store.db() as db:
        row = db.execute('SELECT * FROM devices WHERE id=?', (data['device_id'],)).fetchone()
        assert row['credential_kind'] == 'mobile'
        assert row['status'] == 'configured'
        assert 'mobile-secret' not in row['credential']
    headers = {'Authorization': 'Bearer ' + data['device_token']}
    assert client.get('/v1/device/tasks/next?wait=0', headers=headers).status_code == 200
    assert client.post('/v1/pair', json=body).status_code == 400
    assert calls == [('mobile-secret', 'mobile')]
    assert client.delete('/v1/device', headers=headers).status_code == 200


@pytest.mark.parametrize('failure,status', [(ValueError('rejected'), 401), (httpx.ConnectError('offline'), 502)])
def test_failed_mobile_pairing_preserves_qr(system, monkeypatch, failure, status):
    client, store = system

    async def reject(*args):
        raise failure

    monkeypatch.setattr('server.app.fetch_lessons', reject)
    code = client.post('/v1/admin/pairings', headers={'Authorization': 'Bearer ' + 'a' * 32}).json()['code']
    body = {'code': code, 'name': 'phone', 'monitor_sid': 'mobile-secret'}
    assert client.post('/v1/pair', json=body).status_code == status
    with store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM devices').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM pairs WHERE code=?', (digest(code),)).fetchone()[0] == 1

    async def accept(*args):
        return []

    monkeypatch.setattr('server.app.fetch_lessons', accept)
    assert client.post('/v1/pair', json=body).status_code == 200


def test_old_database_migrates_without_losing_credentials(tmp_path):
    import sqlite3
    from server.app import Store
    path = str(tmp_path / 'legacy.db')
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE devices(id TEXT PRIMARY KEY, token TEXT UNIQUE, name TEXT, credential TEXT, "
                   "revoked INTEGER DEFAULT 0, status TEXT DEFAULT 'unconfigured')")
        db.execute("INSERT INTO devices(id,token,name,credential) VALUES('old','hash','desktop','encrypted')")
    store = Store(path)
    with store.db() as db:
        row = db.execute("SELECT * FROM devices WHERE id='old'").fetchone()
        assert row['credential_kind'] == 'desktop'
        assert row['credential'] == 'encrypted'
    Store(path)


def test_repeat_mobile_scan_replaces_the_previous_device(system, monkeypatch):
    async def accept(*args):
        return []

    monkeypatch.setattr('server.app.fetch_lessons', accept)
    client, store = system
    admin = {'Authorization': 'Bearer ' + 'a' * 32}

    def pair(sid, name='Android client'):
        code = client.post('/v1/admin/pairings', headers=admin).json()['code']
        response = client.post('/v1/pair', json={'code': code, 'name': name, 'monitor_sid': sid})
        assert response.status_code == 200
        return response.json()

    first = pair('same-sid')
    second = pair('same-sid')
    assert first['device_id'] != second['device_id']
    assert client.get('/v1/device/tasks/next?wait=0',
                      headers={'Authorization': 'Bearer ' + first['device_token']}).status_code == 401
    assert client.get('/v1/device/tasks/next?wait=0',
                      headers={'Authorization': 'Bearer ' + second['device_token']}).status_code == 200
    other = pair('other-sid', 'Other phone')
    listed = client.get('/v1/admin/devices', headers=admin).json()
    assert {item['id'] for item in listed if not item['revoked']} == {second['device_id'], other['device_id']}
    with store.db() as db:
        old = db.execute('SELECT revoked,credential FROM devices WHERE id=?', (first['device_id'],)).fetchone()
        assert old['revoked'] == 1 and old['credential'] is None

    async def reject(*args):
        raise ValueError('rejected')

    monkeypatch.setattr('server.app.fetch_lessons', reject)
    code = client.post('/v1/admin/pairings', headers=admin).json()['code']
    failed = client.post('/v1/pair', json={'code': code, 'name': 'Android client', 'monitor_sid': 'same-sid'})
    assert failed.status_code == 401
    assert client.get('/v1/device/tasks/next?wait=0',
                      headers={'Authorization': 'Bearer ' + second['device_token']}).status_code == 200


def test_invalid_mobile_credential_is_not_accepted(system):
    client, store = system
    code = client.post('/v1/admin/pairings', headers={'Authorization': 'Bearer ' + 'a' * 32}).json()['code']
    assert client.post('/v1/pair', json={'code': code, 'name': 'phone', 'monitor_sid': 'sid; injected'}).status_code == 422
    with store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM devices').fetchone()[0] == 0


def test_auto_pair_to_monitor_to_result(tmp_path, monkeypatch):
    from server.app import Store

    async def lessons(client, secret, kind):
        assert secret == 'phone-session'
        assert kind == 'mobile'
        return [{'lessonId': 987, 'classroomId': 6, 'courseName': 'Integration'}]

    original_enqueue = Store.enqueue

    def immediate_enqueue(self, device, lesson):
        original_enqueue(self, device, lesson, delay=0)

    monkeypatch.setattr('server.app.fetch_lessons', lessons)
    monkeypatch.setattr(Store, 'enqueue', immediate_enqueue)
    monkeypatch.setenv('POLL_INTERVAL', '3')
    key = Fernet.generate_key()
    app = create_app(str(tmp_path / 'integration.db'), 'a' * 32, key, monitor=True)
    with TestClient(app) as client:
        code = client.post('/v1/admin/pairings', headers={'Authorization': 'Bearer ' + 'a' * 32}).json()['code']
        paired = client.post('/v1/pair', json={'code': code, 'name': 'phone', 'monitor_sid': 'phone-session'}).json()
        assert paired['monitor_configured'] is True
        headers = {'Authorization': 'Bearer ' + paired['device_token']}
        task = client.get('/v1/device/tasks/next?wait=6', headers=headers).json()['task']
        assert task is not None
        assert task['payload']['lessonId'] == 987
        result = client.post('/v1/device/tasks/' + task['id'] + '/result', headers=headers,
                             json={'lease_token': task['lease_token'], 'success': True, 'message': 'simulated'})
        assert result.status_code == 200
        with app.state.store.db() as db:
            row = db.execute('SELECT credential FROM devices WHERE id=?', (paired['device_id'],)).fetchone()
            assert Fernet(key).decrypt(row['credential'].encode()) == b'phone-session'
            assert db.execute('SELECT done FROM jobs WHERE id=?', (task['id'],)).fetchone()['done'] == 1
