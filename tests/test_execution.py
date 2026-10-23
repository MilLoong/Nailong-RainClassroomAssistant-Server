import asyncio
import json

import httpx

from tests.test_server import system, register
from server.app import submit_checkin


def mobile(store, device):
    # Unit tests replace upstream calls; store encryption is populated in the polling test.
    with store.db() as db:
        db.execute("UPDATE devices SET credential_kind='mobile',credential='configured' WHERE id=?", (device,))


def test_mode_auth_and_no_client_execution(system):
    client, store = system
    device, headers = register(client)
    assert client.put('/v1/device/mode', headers=headers, json={'mode': 'server'}).status_code == 409
    mobile(store, device)
    assert client.put('/v1/device/mode', headers=headers, json={'mode': 'server'}).json()['mode'] == 'server'
    store.enqueue(device, {'lessonId': 123}, delay=0)
    assert store.claim(device) is None
    assert client.put('/v1/device/mode', headers=headers, json={'mode': 'local'}).status_code == 200
    with store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs WHERE done=0').fetchone()[0] == 0
    assert client.put('/v1/device/mode', json={'mode': 'server'}).status_code in (401, 403)


def test_events_replay_and_isolation(system):
    client, store = system
    device, headers = register(client)
    _, other = register(client, 'other')
    with store.db() as db:
        for i in range(105):
            db.execute('INSERT INTO events(device,job,payload,created) VALUES(?,?,?,?)',
                       (device, str(i), json.dumps({'success': True, 'lesson': {'lessonId': i}, 'message': 'checked_in'}), 1))
    first = client.get('/v1/device/events', headers=headers).json()
    assert len(first['events']) == 100
    assert client.get('/v1/device/events', headers=headers).json() == first
    assert len(client.get('/v1/device/events?after=' + str(first['cursor']), headers=headers).json()['events']) == 5
    assert client.get('/v1/device/events', headers=other).json()['events'] == []
    assert client.get('/v1/device/events?after=-1', headers=headers).status_code == 422


def test_active_legacy_lease_blocks_switch(system):
    client, store = system
    device, headers = register(client)
    mobile(store, device)
    store.enqueue(device, {'lessonId': 12}, delay=0)
    store.claim(device)
    assert client.put('/v1/device/mode', headers=headers, json={'mode': 'server'}).status_code == 409


def test_submit_classifies_manual_and_no_redirects():
    async def run():
        def handle(request):
            assert request.headers['cookie'] == 'sid=test'
            assert json.loads(request.content)['lessonId'] == '123'
            assert json.loads(request.content)['source'] == 21
            assert json.loads(request.content)['joinIfNotIn'] is True
            return httpx.Response(200, json={'code': 1, 'msg': 'DYNAMIC_QR_CHECK_IN_REFUSED'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as upstream:
            result = await submit_checkin(upstream, 'test', '123')
            assert result == {'success': False, 'message': 'checkin_rejected_dynamic_qr'}
    asyncio.run(run())


def test_execution_survives_offline_client_and_does_not_repeat(tmp_path):
    from cryptography.fernet import Fernet
    from server.app import create_app, digest
    key = Fernet.generate_key()
    app = create_app(str(tmp_path / 'execution.db'), 'a' * 32, key, monitor=False)
    store = app.state.store
    with store.db() as db:
        db.execute("INSERT INTO devices(id,token,name,credential,credential_kind,execution_mode) "
                   "VALUES('phone',?,'phone',?,'mobile','server')",
                   (digest('test-token'), Fernet(key).encrypt(b'sid').decode()))
    store.enqueue('phone', {'lessonId': 123, 'courseName': 'Demo'}, delay=0)
    calls = []
    async def run():
        def handle(request):
            if request.method == 'GET':
                return httpx.Response(200, json={'code': 0, 'data': {'onLessonClassrooms': []}})
            calls.append(request)
            return httpx.Response(200, json={'code': 0})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as upstream:
            with store.db() as db:
                device = db.execute('SELECT * FROM devices').fetchone()
            await app.state.poll_device(upstream, device)
            await app.state.poll_device(upstream, device)
    asyncio.run(run())
    assert len(calls) == 1
    with store.db() as db:
        assert json.loads(db.execute('SELECT payload FROM events').fetchone()[0])['success'] is True
        assert db.execute('SELECT done FROM jobs').fetchone()[0] == 1


def test_notifications_are_durable_in_new_mode(system):
    client, store = system
    device, headers = register(client)
    client.put('/v1/device/mode', headers=headers, json={'mode': 'local'})
    store.notify(device, 'Test', 'Hello', ttl=0)
    events = client.get('/v1/device/events', headers=headers).json()['events']
    assert events[0]['kind'] == 'notification'
    assert events[0]['message'] == 'Hello'


def test_mode_handshake_shares_completed_lessons(system):
    client, store = system
    device, headers = register(client)
    mobile(store, device)
    store.enqueue(device, {'lessonId': 123}, delay=0)
    response = client.put('/v1/device/mode', headers=headers,
                          json={'mode': 'server', 'checked_lessons': ['123']})
    assert response.status_code == 200
    assert response.json()['checked_lessons'] == ['123']
    with store.db() as db:
        assert db.execute('SELECT done FROM jobs WHERE lesson=?', ('123',)).fetchone()[0] == 1
    assert client.put('/v1/device/mode', headers=headers,
                      json={'mode': 'server', 'checked_lessons': ['invalid']}).status_code == 422


def test_pause_waits_for_inflight_and_network_failure_is_not_replayed(tmp_path):
    from cryptography.fernet import Fernet
    from server.app import create_app, digest
    key = Fernet.generate_key()
    app = create_app(str(tmp_path / 'race.db'), 'a' * 32, key, monitor=False)
    store = app.state.store
    with store.db() as db:
        db.execute("INSERT INTO devices(id,token,name,credential,credential_kind,execution_mode) "
                   "VALUES('phone',?,'phone',?,'mobile','server')",
                   (digest('test-token'), Fernet(key).encrypt(b'sid').decode()))
    store.enqueue('phone', {'lessonId': 123, 'courseName': 'Demo'}, delay=0)
    submissions = []
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        async def upstream(request):
            if request.method == 'GET':
                return httpx.Response(200, json={'code': 0, 'data': {'onLessonClassrooms': []}})
            submissions.append(request)
            started.set()
            await release.wait()
            raise httpx.ReadTimeout('unknown outcome')
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
            with store.db() as db:
                device = db.execute('SELECT * FROM devices').fetchone()
            polling = asyncio.create_task(app.state.poll_device(remote, device))
            await started.wait()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
                pause = asyncio.create_task(client.put('/v1/device/mode', headers={'Authorization': 'Bearer test-token'},
                                                       json={'mode': 'local'}))
                await asyncio.sleep(0.02)
                assert not pause.done()
                release.set()
                await polling
                assert (await pause).json()['mode'] == 'local'
                await client.put('/v1/device/mode', headers={'Authorization': 'Bearer test-token'}, json={'mode': 'server'})
                await app.state.poll_device(remote, device)
    asyncio.run(run())
    assert len(submissions) == 1
    with store.db() as db:
        assert json.loads(db.execute('SELECT payload FROM events').fetchone()[0])['message'] == 'execution_uncertain_check_manually'


def test_location_config_encrypted_and_can_be_cleared(system):
    client, store = system
    device, headers = register(client)
    config = {'delay_seconds': 30, 'location': {'lat': 30.123456, 'lng': 120.654321, 'address': 'test-address'}}
    assert client.put('/v1/device/checkin-config', headers=headers, json=config).status_code == 200
    with store.db() as db:
        value = db.execute('SELECT checkin_config FROM devices WHERE id=?', (device,)).fetchone()[0]
        assert 'test-address' not in value and '30.123456' not in value
    assert client.put('/v1/device/checkin-config', headers=headers, json={'location': {'lat': 91, 'lng': 0}}).status_code == 422
    assert client.put('/v1/device/checkin-config', headers=headers, json={'location': None}).status_code == 200
    assert client.put('/v1/device/checkin-config', json=config).status_code in (401, 403)


def test_location_submission_contract():
    async def run():
        paths = []
        def handle(request):
            paths.append(request.url.path)
            assert request.headers['cookie'] == 'sid=test'
            if request.url.path.endswith('eligibility'):
                return httpx.Response(200, json={'code': 0, 'data': {'needLocationSignIn': True}})
            assert request.url.path.endswith('location-checkin/submit')
            assert json.loads(request.content) == {'lat': '30.0', 'lng': '120.0', 'source': 54,
                                                  'lessonId': '123', 'address': 'test'}
            return httpx.Response(200, json={'code': 0})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as remote:
            result = await submit_checkin(remote, 'test', '123',
                                          {'location': {'lat': 30.0, 'lng': 120.0, 'address': 'test'}})
            assert result == {'success': True, 'message': 'location_checked_in'}
        assert len(paths) == 2
    asyncio.run(run())


def test_location_not_configured_never_invents_coordinates():
    async def run():
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(200, json={'code': 1, 'msg': 'LOCATION_REQUIRED'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as remote:
            result = await submit_checkin(remote, 'test', '123')
            assert result['message'] == 'location_config_required'
        assert len(calls) == 1
    asyncio.run(run())
