import json
import re
import sqlite3
import time
import asyncio

import httpx

from fastapi.testclient import TestClient

from server.app import Store
from server.dev import create_dev_app, set_public_origin
from server.tunnel import device_gateway
from tests.test_server import system, register


def test_admin_can_send_notification_without_a_lesson(system):
    client, store = system
    device, headers = register(client)
    endpoint = '/v1/admin/devices/' + device + '/notifications/test'
    assert client.post(endpoint, json={}).status_code in (401, 403)
    assert client.post(endpoint, headers=headers, json={}).status_code == 401
    response = client.post(endpoint, headers={'Authorization': 'Bearer ' + 'a' * 32}, json={})
    assert response.status_code == 200
    queued = response.json()
    task = client.get('/v1/device/tasks/next?wait=0', headers=headers).json()['task']
    assert task['id'] == queued['task_id']
    assert task['kind'] == 'notification'
    assert task['payload']['test'] is True
    assert 'lessonId' not in task['payload']
    assert task['payload']['title']
    result = {'lease_token': task['lease_token'], 'success': True, 'message': 'test_notification_posted'}
    assert client.post('/v1/device/tasks/' + task['id'] + '/result', headers=headers, json=result).status_code == 200
    assert client.get('/v1/device/tasks/next?wait=0', headers=headers).json()['task'] is None
    with store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE kind='checkin'").fetchone()[0] == 0


def test_notification_is_scoped_to_target_and_rejects_revoked_device(system):
    client, store = system
    target, _ = register(client)
    _, other = register(client, 'other')
    store.notify(target, 'test', 'body')
    assert client.get('/v1/device/tasks/next?wait=0', headers=other).json()['task'] is None
    admin = {'Authorization': 'Bearer ' + 'a' * 32}
    client.delete('/v1/admin/devices/' + target, headers=admin)
    assert client.post('/v1/admin/devices/' + target + '/notifications/test', headers=admin, json={}).status_code == 404


def test_local_notification_button_status_and_csrf(tmp_path):
    app = create_dev_app(tmp_path, '127.0.0.1', monitor=False, public_mode=True)
    set_public_origin(app, 'https://testing-example.trycloudflare.com')
    store = app.state.store
    with store.db() as db:
        db.execute("INSERT INTO devices(id,token,name) VALUES('phone','unused','My phone')")
    with TestClient(app, base_url='http://127.0.0.1:8000', client=('127.0.0.1', 2000)) as client:
        page = client.get('/dev/pair')
        assert '发送测试通知' in page.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
        endpoint = '/dev/notifications/test'
        assert 'referrerpolicy="same-origin"' in page.text
        assert client.post(endpoint, data={'device': 'phone'}).status_code == 403
        form = {'device': 'phone', 'csrf': csrf}
        browser = {'Origin': 'null', 'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Mode': 'navigate'}
        assert client.post(endpoint, data=form, headers={'Origin': 'https://attacker.example'}).status_code == 403
        assert client.post(endpoint, data=form, headers={'Origin': 'null', 'Sec-Fetch-Site': 'cross-site'}).status_code == 403
        assert client.post(endpoint, data=form, headers={'Origin': 'null'}).status_code == 403
        queued = client.post(endpoint, data=form, headers=browser, follow_redirects=False)
        assert queued.status_code == 303
        status = client.get(queued.headers['location'])
        assert '等待手机领取' in status.text
        task = store.claim('phone')
        assert task['kind'] == 'notification'
        assert '手机已领取' in client.get(queued.headers['location']).text
        with store.db() as db:
            db.execute('UPDATE jobs SET done=1,result=? WHERE id=?',
                       (json.dumps({'success': False, 'message': 'notification_permission_denied'}), task['id']))
        assert 'notification_permission_denied' in client.get(queued.headers['location']).text
        revoked = client.post('/dev/devices/revoke', data=form, headers=browser, follow_redirects=False)
        assert revoked.status_code == 303
        assert '暂无已绑定设备' in client.get('/dev/pair').text


def test_public_gateway_cannot_send_test_notifications(tmp_path):
    app = create_dev_app(tmp_path, '127.0.0.1', monitor=False, public_mode=True)
    set_public_origin(app, 'https://testing-example.trycloudflare.com')
    async def run():
        transport = httpx.ASGITransport(app=device_gateway(app), client=('127.0.0.1', 2000))
        async with httpx.AsyncClient(transport=transport, base_url='https://testing-example.trycloudflare.com') as client:
            assert (await client.post('/dev/notifications/test')).status_code == 404
            assert (await client.post('/v1/admin/devices/phone/notifications/test', json={})).status_code == 404
    asyncio.run(run())


def test_legacy_checkin_jobs_migrate_and_notification_expiry(tmp_path):
    path = str(tmp_path / 'old.db')
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY, device TEXT, lesson TEXT, payload TEXT, '
                   'created REAL, available REAL, expires REAL, lease_until REAL DEFAULT 0, '
                   'lease_token TEXT, done INTEGER DEFAULT 0, result TEXT, UNIQUE(device,lesson))')
        now = time.time()
        db.execute('INSERT INTO jobs(id,device,lesson,payload,created,available,expires) VALUES(?,?,?,?,?,?,?)',
                   ('old', 'phone', '1', '{"lessonId":1}', now, now, now + 120))
    store = Store(path)
    assert store.claim('phone')['kind'] == 'checkin'
    with store.db() as db:
        db.execute("INSERT INTO devices(id,token,name) VALUES('phone','hash','phone')")
    store.notify('phone', 'test', 'expired', ttl=-1)
    assert store.claim('phone') is None
