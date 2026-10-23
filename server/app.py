import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)
bearer = HTTPBearer()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class DeviceInfo(BaseModel):
    model_config = {'extra': 'forbid'}
    manufacturer: str = Field(default='', max_length=80)
    brand: str = Field(default='', max_length=80)
    model: str = Field(default='', max_length=120)
    android_release: str = Field(default='', max_length=40)
    sdk_int: int = Field(default=0, ge=0, le=200)
    build_display: str = Field(default='', max_length=256)
    app_version: str = Field(default='', max_length=80)
    app_version_code: int = Field(default=0, ge=0, le=2100000000)
    package_name: str = Field(default='', max_length=200)


def push_plan(info):
    names = {info.get('manufacturer', '').strip().lower(), info.get('brand', '').strip().lower()}
    candidates = [('huawei', {'huawei'}), ('honor', {'honor'}),
                  ('xiaomi', {'xiaomi', 'redmi', 'poco'}),
                  ('oppo', {'oppo', 'realme', 'oneplus'}), ('vivo', {'vivo', 'iqoo'}),
                  ('fcm', {'google'})]
    candidate = next((provider for provider, brands in candidates if names & brands), None)
    return {'transport': 'polling', 'candidate_provider': candidate,
            'configured': False, 'reason': 'sdk_token_and_provider_credentials_required'}


class Pair(BaseModel):
    code: str = Field(min_length=20, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    monitor_sid: str | None = Field(default=None, min_length=1, max_length=4096,
                                    pattern=r"^[^\s;\r\n]+$")
    device_info: DeviceInfo | None = None


class Credentials(BaseModel):
    sessionid: str = Field(min_length=1, max_length=4096, pattern=r"^[^\s;\r\n]+$")


class Result(BaseModel):
    lease_token: str
    success: bool
    message: str = Field(default="", max_length=1000)


class ExecutionMode(BaseModel):
    mode: str = Field(pattern='^(server|local)$')
    checked_lessons: list[str] = Field(default_factory=list, max_length=5000)


class LocationConfig(BaseModel):
    model_config = {'extra': 'forbid'}
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lng: float = Field(ge=-180, le=180, allow_inf_nan=False)
    address: str = Field(default='', max_length=2000)


class CheckinConfig(BaseModel):
    model_config = {'extra': 'forbid'}
    delay_seconds: int = Field(default=0, ge=0, le=86400)
    location: LocationConfig | None = None


class TestNotification(BaseModel):
    title: str = Field(default='雨课堂助手：测试通知', min_length=1, max_length=100)
    body: str = Field(default='这是一条服务端发送的测试通知，不会执行签到。', min_length=1, max_length=500)


class Store:
    def __init__(self, path):
        self.path = path
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS pairs(code TEXT PRIMARY KEY, expires REAL);
                CREATE TABLE IF NOT EXISTS devices(
                    id TEXT PRIMARY KEY, token TEXT UNIQUE, name TEXT,
                    credential TEXT, revoked INTEGER DEFAULT 0, status TEXT DEFAULT 'unconfigured',
                    credential_kind TEXT NOT NULL DEFAULT 'desktop');
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY, device TEXT, lesson TEXT, payload TEXT,
                    created REAL, available REAL, expires REAL, lease_until REAL DEFAULT 0,
                    lease_token TEXT, done INTEGER DEFAULT 0, result TEXT,
                    kind TEXT NOT NULL DEFAULT 'checkin',
                    UNIQUE(device, lesson));
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(devices)')}
            if 'credential_kind' not in columns:
                db.execute("ALTER TABLE devices ADD COLUMN credential_kind TEXT NOT NULL DEFAULT 'desktop'")
            if 'device_info' not in columns:
                db.execute("ALTER TABLE devices ADD COLUMN device_info TEXT NOT NULL DEFAULT '{}'")
            if 'execution_mode' not in columns:
                db.execute("ALTER TABLE devices ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'legacy'")
            if 'checkin_config' not in columns:
                db.execute('ALTER TABLE devices ADD COLUMN checkin_config TEXT')
            db.execute('CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, '
                       'device TEXT NOT NULL, job TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL)')
            job_columns = {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
            if 'kind' not in job_columns:
                db.execute("ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'checkin'")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, device, lesson, delay=15, ttl=180):
        now = time.time()
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO jobs(id,device,lesson,payload,created,available,expires) '
                       'VALUES(?,?,?,?,?,?,?)',
                       (secrets.token_urlsafe(18), device, str(lesson['lessonId']),
                        json.dumps(lesson), now, now + delay, now + ttl))

    def claim(self, device):
        now = time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM jobs WHERE device=? AND done=0 AND available<=? '
                             "AND expires>? AND lease_until<=? AND (kind!='checkin' OR NOT EXISTS "
                             "(SELECT 1 FROM devices WHERE id=? AND execution_mode!='legacy')) ORDER BY created LIMIT 1",
                             (device, now, now, now, device)).fetchone()
            if not row:
                return None
            token = secrets.token_urlsafe(24)
            db.execute('UPDATE jobs SET lease_until=?,lease_token=? WHERE id=?',
                       (min(now + 45, row['expires']), token, row['id']))
            return dict(id=row['id'], kind=row['kind'], payload=json.loads(row['payload']),
                        expires_at=row['expires'], lease_token=token)

    def notify(self, device, title, body, ttl=120):
        now = time.time()
        task_id = secrets.token_urlsafe(18)
        payload = json.dumps({'title': title, 'body': body, 'test': True}, ensure_ascii=False)
        with self.db() as db:
            active = db.execute('SELECT execution_mode FROM devices WHERE id=? AND revoked=0', (device,)).fetchone()
            if not active:
                raise HTTPException(404, 'active_device_not_found')
            db.execute('INSERT INTO jobs(id,device,lesson,payload,created,available,expires,kind) '
                       'VALUES(?,?,?,?,?,?,?,?)',
                       (task_id, device, 'notification:' + task_id, payload, now, now, now + ttl, 'notification'))
            if active['execution_mode'] != 'legacy':
                event = {'success': True, 'message': body, 'title': title, 'kind': 'notification',
                         'lesson': {'courseName': '测试通知', 'lessonId': ''}}
                db.execute('INSERT INTO events(device,job,payload,created) VALUES(?,?,?,?)',
                           (device, task_id, json.dumps(event), now))
                db.execute('UPDATE jobs SET done=1,result=? WHERE id=?',
                           (json.dumps({'success': True, 'message': 'notification_stored_for_sync'}), task_id))
        return {'task_id': task_id, 'status': 'queued', 'expires_at': now + ttl}


async def fetch_lessons(client, sessionid, kind='desktop'):
    headers = {'Cookie': f'sessionid={sessionid}'}
    if kind == 'mobile':
        headers = {'Cookie': f'sid={sessionid}', 'x-client': 'app', 'xtbz': 'ykt'}
    elif kind != 'desktop':
        raise ValueError('invalid_credential_kind')
    response = await client.get('https://www.yuketang.cn/api/v3/classroom/on-lesson',
                                headers=headers)
    response.raise_for_status()
    body = response.json()
    if body.get('code') != 0:
        raise ValueError('upstream_rejected')
    lessons = body['data']['onLessonClassrooms']
    if not isinstance(lessons, list):
        raise ValueError('invalid_upstream_response')
    for lesson in lessons:
        for key in ('lessonId', 'courseName', 'classroomId'):
            if key not in lesson:
                raise ValueError('invalid_lesson')
    return lessons


async def submit_location_checkin(client, sid, lesson, location):
    if not location:
        return {'success': False, 'message': 'location_config_required'}
    headers = {'Cookie': f'sid={sid}', 'x-client': 'app', 'xtbz': 'ykt'}
    address = location.get('address', '')
    if not address:
        try:
            response = await client.get('https://www.yuketang.cn/api/v3/tencent-lbs/proxy', headers=headers,
                                        params={'lbsPath': 'geocoder/v1',
                                                'location': f"{location['lat']},{location['lng']}", 'get_poi': '2'})
            response.raise_for_status()
            body = response.json()
            data = body.get('data') or {}
            if body.get('code') == 0:
                address = data.get('address') or (data.get('result') or {}).get('address', '')
        except httpx.HTTPError:
            pass
    response = await client.post('https://www.yuketang.cn/api/v3/lesson/location-checkin/submit',
                                 headers=headers, json={'lat': str(location['lat']), 'lng': str(location['lng']),
                                                        'lessonId': str(lesson), 'source': 54, 'address': address})
    response.raise_for_status()
    body = response.json()
    return {'success': body.get('code') == 0, 'message': 'location_checked_in' if body.get('code') == 0
            else 'login_expired' if 'UNAUTHENTICATED' in str(body.get('msg', '')).upper()
            else 'location_checkin_rejected'}


async def submit_checkin(client, sid, lesson, config=None):
    if not str(lesson).isdigit():
        return {'success': False, 'message': 'invalid_lesson_id'}
    location = (config or {}).get('location')
    if location:
        response = await client.get('https://www.yuketang.cn/api/v3/lesson/location-checkin/eligibility',
                                    headers={'Cookie': f'sid={sid}', 'x-client': 'app', 'xtbz': 'ykt'},
                                    params={'lessonId': str(lesson), 'lesson_id': str(lesson)})
        response.raise_for_status()
        eligibility = response.json()
        data = eligibility.get('data') or {}
        if eligibility.get('code') == 0:
            if data.get('signed'):
                return {'success': True, 'message': 'location_already_checked_in'}
            if data.get('needLocationSignIn') or data.get('canSign') or data.get('session') is not None:
                return await submit_location_checkin(client, sid, lesson, location)
    response = await client.post('https://www.yuketang.cn/api/v3/lesson/checkin',
                                 headers={'Cookie': f'sid={sid}', 'x-client': 'app'},
                                 json={'source': 21, 'lessonId': str(lesson), 'joinIfNotIn': True})
    response.raise_for_status()
    body = response.json()
    if body.get('code') == 0:
        return {'success': True, 'message': 'checked_in'}
    message = str(body.get('msg', '')).upper()
    if 'LOCATION' in message or '定位' in message:
        return await submit_location_checkin(client, sid, lesson, location)
    reason = ('login_expired' if 'UNAUTHENTICATED' in message else
              'checkin_rejected_dynamic_qr' if 'DYNAMIC_QR' in message else
              'location_requires_manual_checkin' if 'LOCATION' in message or '定位' in message else
              'checkin_rejected_check_app')
    return {'success': False, 'message': reason}


def create_app(db_path=None, admin_token=None, encryption_key=None, monitor=True):
    admin = admin_token or os.environ.get('ADMIN_TOKEN', '')
    if len(admin) < 32:
        raise RuntimeError('ADMIN_TOKEN must contain at least 32 characters')
    cipher = Fernet(encryption_key or os.environ['ENCRYPTION_KEY'])
    store = Store(db_path or os.environ.get('DATABASE_PATH', 'nailong.db'))
    interval = max(3, int(os.environ.get('POLL_INTERVAL', '10')))
    # One worker per database: mode acknowledgement waits for any in-flight submission.
    execution_lock = asyncio.Lock()

    async def execute_pending(client, device, sid, config):
        with store.db() as db:
            rows = db.execute("SELECT * FROM jobs WHERE device=? AND kind='checkin' AND done=0 "
                              'AND available<=? AND expires>?', (device, time.time(), time.time())).fetchall()
        for job in rows:
            uncertain = {'success': False, 'message': 'execution_uncertain_check_manually'}
            # Persist before the non-idempotent request. A crash must never trigger replay.
            with store.db() as db:
                claimed = db.execute('UPDATE jobs SET done=1,result=? WHERE id=? AND done=0 AND EXISTS '
                                     "(SELECT 1 FROM devices WHERE id=? AND revoked=0 AND execution_mode='server')",
                                     (json.dumps(uncertain), job['id'], device)).rowcount
                if claimed:
                    db.execute('INSERT INTO events(device,job,payload,created) VALUES(?,?,?,?)',
                               (device, job['id'], json.dumps(dict(uncertain, lesson=json.loads(job['payload']))), time.time()))
            if not claimed:
                continue
            try:
                result = await submit_checkin(client, sid, job['lesson'], config)
            except asyncio.CancelledError:
                raise
            except Exception:
                result = uncertain
            with store.db() as db:
                db.execute('UPDATE jobs SET result=? WHERE id=?', (json.dumps(result), job['id']))
                # Replace the provisional event with a new sequence so a concurrent reader
                # that saw uncertainty can still observe the definitive result.
                db.execute('DELETE FROM events WHERE job=?', (job['id'],))
                db.execute('INSERT INTO events(device,job,payload,created) VALUES(?,?,?,?)',
                           (device, job['id'], json.dumps(dict(result, lesson=json.loads(job['payload']))), time.time()))

    async def poll_device(client, device):
        async with execution_lock:
            with store.db() as db:
                device = db.execute('SELECT * FROM devices WHERE id=? AND revoked=0', (device['id'],)).fetchone()
            if not device or device['execution_mode'] == 'local':
                return
            try:
                sid = cipher.decrypt(device['credential'].encode()).decode()
                config = json.loads(cipher.decrypt(device['checkin_config'].encode()).decode()) if device['checkin_config'] else {}
                lessons = await fetch_lessons(client, sid, device['credential_kind'])
                for lesson in lessons:
                    if device['execution_mode'] == 'server':
                        delay = config.get('delay_seconds', 0)
                        store.enqueue(device['id'], lesson, delay=delay, ttl=max(180, delay + 180))
                    else:
                        store.enqueue(device['id'], lesson)
                if device['execution_mode'] == 'server':
                    await execute_pending(client, device['id'], sid, config)
                status = 'listening'
            except asyncio.CancelledError:
                raise
            except ValueError:
                status = 'upstream_rejected_or_invalid'
            except Exception:
                status = 'network_or_credential_error'
            with store.db() as db:
                db.execute('UPDATE devices SET status=? WHERE id=?', (status, device['id']))

    async def poll():
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            while True:
                with store.db() as db:
                    devices = db.execute('SELECT * FROM devices WHERE revoked=0 AND credential IS NOT NULL').fetchall()
                for device in devices:
                    await poll_device(client, device)
                await asyncio.sleep(interval)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(poll()) if monitor else None
        app.state.monitor_task = task
        yield
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title='Nailong RainClassroom Assistant Server', lifespan=lifespan)
    app.state.store = store
    app.state.poll_device = poll_device

    @app.put('/v1/device/mode')
    async def set_mode(body: ExecutionMode, auth: HTTPAuthorizationCredentials = Depends(bearer)):
        async with execution_lock:
            with store.db() as db:
                row = db.execute('SELECT * FROM devices WHERE token=? AND revoked=0', (digest(auth.credentials),)).fetchone()
                if not row:
                    raise HTTPException(401, 'invalid_device_token')
                if body.mode == 'server' and (row['credential_kind'] != 'mobile' or not row['credential']):
                    raise HTTPException(409, 'mobile_credentials_required')
                active = db.execute("SELECT 1 FROM jobs WHERE device=? AND kind='checkin' AND done=0 "
                                    'AND lease_until>?', (row['id'], time.time())).fetchone()
                if active:
                    raise HTTPException(409, 'client_execution_in_flight')
                if any(not item.isascii() or not item.isdigit() or len(item) > 30 for item in body.checked_lessons):
                    raise HTTPException(422, 'invalid_checked_lesson')
                for lesson in body.checked_lessons:
                    now = time.time()
                    db.execute('INSERT INTO jobs(id,device,lesson,payload,created,available,expires,done,result) '
                               'VALUES(?,?,?,?,?,?,?,1,?) ON CONFLICT(device,lesson) DO UPDATE '
                               'SET done=1,result=excluded.result WHERE jobs.done=0',
                               (secrets.token_urlsafe(18), row['id'], lesson, '{}', now, now, now,
                                json.dumps({'success': True, 'message': 'already_checked_locally'})))
                db.execute('UPDATE devices SET execution_mode=?,status=? WHERE id=?',
                           (body.mode, 'paused' if body.mode == 'local' else 'configured', row['id']))
                if body.mode == 'local':
                    db.execute("DELETE FROM jobs WHERE device=? AND kind='checkin' AND done=0", (row['id'],))
                checked = [job['lesson'] for job in db.execute(
                    "SELECT lesson,result FROM jobs WHERE device=? AND kind='checkin' AND done=1 "
                    'ORDER BY created DESC LIMIT 5000', (row['id'],))
                    if json.loads(job['result'] or '{}').get('success')]
            return {'mode': body.mode, 'protocol_version': 3, 'checked_lessons': checked}

    def require_admin(auth: HTTPAuthorizationCredentials = Depends(bearer)):
        if not hmac.compare_digest(auth.credentials, admin):
            raise HTTPException(401, 'invalid_admin_token')

    def require_device(auth: HTTPAuthorizationCredentials = Depends(bearer)):
        with store.db() as db:
            device = db.execute('SELECT * FROM devices WHERE token=? AND revoked=0',
                                (digest(auth.credentials),)).fetchone()
        if not device:
            raise HTTPException(401, 'invalid_device_token')
        return device['id']

    @app.get('/v1/device/events')
    def events(after: int = 0, device=Depends(require_device)):
        if after < 0:
            raise HTTPException(422, 'invalid_cursor')
        with store.db() as db:
            rows = db.execute('SELECT * FROM events WHERE device=? AND seq>? ORDER BY seq LIMIT 100',
                              (device, after)).fetchall()
            state = db.execute('SELECT execution_mode,status FROM devices WHERE id=?', (device,)).fetchone()
        return {'events': [dict(seq=r['seq'], job_id=r['job'], created=r['created'],
                                **json.loads(r['payload'])) for r in rows],
                'cursor': rows[-1]['seq'] if rows else after,
                'mode': state['execution_mode'], 'status': state['status']}

    @app.put('/v1/device/checkin-config')
    async def checkin_config(body: CheckinConfig, device=Depends(require_device)):
        async with execution_lock:
            encrypted = cipher.encrypt(body.model_dump_json().encode()).decode()
            with store.db() as db:
                updated = db.execute('UPDATE devices SET checkin_config=? WHERE id=? AND revoked=0',
                                     (encrypted, device)).rowcount
            if not updated:
                raise HTTPException(401, 'device_revoked')
        return {'status': 'saved'}

    @app.get('/health')
    def health():
        return {'status': 'ok'}

    @app.post('/v1/admin/pairings', dependencies=[Depends(require_admin)])
    def pairing():
        code = secrets.token_urlsafe(32)
        expires = time.time() + 120
        with store.db() as db:
            db.execute('DELETE FROM pairs WHERE expires<=?', (time.time(),))
            db.execute('INSERT INTO pairs VALUES(?,?)', (digest(code), expires))
        return {'code': code, 'expires_at': expires}

    @app.post('/v1/pair')
    async def pair(body: Pair):
        token, device = secrets.token_urlsafe(32), secrets.token_urlsafe(18)
        if body.monitor_sid is not None:
            # Check the pairing capability before making any upstream request.
            with store.db() as db:
                valid = db.execute('SELECT 1 FROM pairs WHERE code=? AND expires>?',
                                   (digest(body.code), time.time())).fetchone()
            if not valid:
                raise HTTPException(400, 'invalid_or_expired_pairing')
            try:
                async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                    await fetch_lessons(client, body.monitor_sid, 'mobile')
            except (ValueError, KeyError, TypeError):
                raise HTTPException(401, 'monitor_login_rejected') from None
            except httpx.HTTPError:
                raise HTTPException(502, 'monitor_validation_unavailable') from None
        encrypted = cipher.encrypt(body.monitor_sid.encode()).decode() if body.monitor_sid is not None else None
        with store.db() as db:
            cursor = db.execute('DELETE FROM pairs WHERE code=? AND expires>?',
                                (digest(body.code), time.time()))
            if cursor.rowcount != 1:
                raise HTTPException(400, 'invalid_or_expired_pairing')
            # One login is one phone. A second scan must not leave the previous device
            # active, or the test page cannot tell which token the phone kept.
            if body.monitor_sid is not None:
                rows = db.execute("SELECT id,credential FROM devices WHERE revoked=0 AND credential_kind='mobile' "
                                  "AND credential IS NOT NULL")
                for row in rows:
                    try:
                        current = cipher.decrypt(row['credential'].encode()).decode()
                    except InvalidToken:
                        continue
                    if len(current) == len(body.monitor_sid) and hmac.compare_digest(
                            current.encode(), body.monitor_sid.encode()):
                        db.execute('UPDATE devices SET revoked=1,credential=NULL WHERE id=?', (row['id'],))
            info = body.device_info.model_dump() if body.device_info else {}
            db.execute('INSERT INTO devices(id,token,name,credential,credential_kind,status,device_info) VALUES(?,?,?,?,?,?,?)',
                       (device, digest(token), body.name, encrypted,
                        'mobile' if encrypted else 'desktop', 'configured' if encrypted else 'unconfigured',
                        json.dumps(info, ensure_ascii=False)))
        return {'device_id': device, 'device_token': token, 'protocol_version': 2,
                'monitor_configured': encrypted is not None, 'push': push_plan(info)}

    @app.put('/v1/device/info')
    def update_device_info(body: DeviceInfo, device=Depends(require_device)):
        info = body.model_dump()
        with store.db() as db:
            db.execute('UPDATE devices SET device_info=? WHERE id=? AND revoked=0',
                       (json.dumps(info, ensure_ascii=False), device))
        return {'status': 'updated', 'push': push_plan(info)}

    @app.put('/v1/device/credentials')
    def credentials(body: Credentials, device=Depends(require_device)):
        with store.db() as db:
            db.execute("UPDATE devices SET credential=?,status=?,credential_kind='desktop' WHERE id=?",
                       (cipher.encrypt(body.sessionid.encode()).decode(), 'configured', device))
        return {'status': 'configured'}

    @app.delete('/v1/device')
    def unpair(device=Depends(require_device)):
        with store.db() as db:
            db.execute('UPDATE devices SET revoked=1,credential=NULL WHERE id=?', (device,))
        return {'status': 'revoked'}

    @app.get('/v1/device/tasks/next')
    async def next_task(wait: int = 25, device=Depends(require_device)):
        if not 0 <= wait <= 30:
            raise HTTPException(422, 'wait_must_be_between_0_and_30')
        deadline = time.monotonic() + wait
        while True:
            # Recheck revocation while a long poll is outstanding.
            with store.db() as db:
                active = db.execute('SELECT 1 FROM devices WHERE id=? AND revoked=0', (device,)).fetchone()
            if not active:
                raise HTTPException(401, 'device_revoked')
            task = store.claim(device)
            if task or time.monotonic() >= deadline:
                return {'task': task}
            await asyncio.sleep(0.5)

    @app.post('/v1/device/tasks/{task_id}/result')
    def result(task_id: str, body: Result, device=Depends(require_device)):
        with store.db() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=? AND device=?', (task_id, device)).fetchone()
            if not row:
                raise HTTPException(404, 'task_not_found')
            if not hmac.compare_digest(row['lease_token'] or '', body.lease_token):
                raise HTTPException(409, 'stale_lease')
            if not row['done'] and row['lease_until'] <= time.time():
                raise HTTPException(409, 'lease_expired')
            if not row['done']:
                db.execute('UPDATE jobs SET done=1,result=? WHERE id=?',
                           (body.model_dump_json(exclude={'lease_token'}), task_id))
        return {'status': 'recorded'}

    @app.get('/v1/admin/devices', dependencies=[Depends(require_admin)])
    def devices():
        with store.db() as db:
            result = []
            for row in db.execute('SELECT id,name,status,revoked,device_info FROM devices'):
                device = dict(row)
                device['device_info'] = json.loads(device['device_info'])
                device['push'] = push_plan(device['device_info'])
                result.append(device)
            return result

    @app.post('/v1/admin/devices/{device}/notifications/test', dependencies=[Depends(require_admin)])
    def test_notification(device: str, body: TestNotification):
        return store.notify(device, body.title, body.body)

    @app.delete('/v1/admin/devices/{device}', dependencies=[Depends(require_admin)])
    def revoke(device: str):
        with store.db() as db:
            db.execute('UPDATE devices SET revoked=1,credential=NULL WHERE id=?', (device,))
        return {'status': 'revoked'}

    return app
