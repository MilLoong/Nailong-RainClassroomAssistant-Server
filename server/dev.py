"""Smoke test: python -m server.dev --public for HTTPS across networks; omit --public for LAN."""
import argparse
import html
import hmac
import ipaddress
import json
import os
import secrets
import shutil
import socket
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import qrcode
import qrcode.image.svg
import uvicorn
from cryptography.fernet import Fernet
from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from server.app import create_app, digest


def device_status_label(status):
    return {'unconfigured': '未配置', 'configured': '已配置', 'listening': '监听中',
            'upstream_rejected_or_invalid': '登录失效', 'network_or_credential_error': '连接失败'}.get(
                status, status or '未知')


def device_info_label(raw):
    info = json.loads(raw)
    return ' / '.join(str(info.get(key) or '') for key in
                      ('brand', 'model', 'android_release', 'app_version')).strip(' /') or '尚未上报设备信息'


def is_lan_ip(value):
    address = ipaddress.IPv4Address(value)
    return any(address in ipaddress.IPv4Network(net) for net in
               ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8'))


def detect_ip():
    candidates = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            # Select the default interface without sending application traffic.
            sock.connect(('192.0.2.1', 9))
            candidates.append(sock.getsockname()[0])
    except OSError:
        pass
    candidates.extend(item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    for candidate in candidates:
        if is_lan_ip(candidate) and not candidate.startswith('127.'):
            return candidate
    raise RuntimeError('No private IPv4 address found. Connect Wi-Fi or pass --ip LAN_IP.')


def load_keys(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / 'keys.json'
    try:
        with path.open('x', encoding='ascii') as stream:
            os.chmod(path, 0o600)
            json.dump({'admin': secrets.token_urlsafe(32), 'encryption': Fernet.generate_key().decode()}, stream)
    except FileExistsError:
        pass
    keys = json.loads(path.read_text(encoding='ascii'))
    Fernet(keys['encryption'])
    if len(keys['admin']) < 32:
        raise RuntimeError('Invalid local keys; preserve keys.json to decrypt existing credentials.')
    return keys


def browser_form_allowed(origin, fetch_site, fetch_mode, port):
    # A same-origin HTML form under Referrer-Policy: no-referrer is specified to send
    # the Origin header as the literal "null". Sec-Fetch-* is set by the browser and
    # cannot be forged from a page, so it still distinguishes that submit from CSRF.
    if origin is None:
        return True
    if origin == 'null':
        return fetch_site == 'same-origin' and fetch_mode in (None, 'navigate')
    parsed = urlsplit(origin)
    if (parsed.scheme != 'http' or parsed.username or parsed.password or parsed.path
            or parsed.query or parsed.fragment):
        return False
    if (parsed.hostname or '').lower() not in ('127.0.0.1', 'localhost', '::1'):
        return False
    return parsed.port == (None if port == 80 else port)


def set_public_origin(app, origin):
    parsed = urlsplit(origin)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('Public origin must be a root HTTPS URL')
    app.state.public_origin = origin.rstrip('/')
    app.state.public_host = parsed.netloc.lower()


def create_dev_app(directory, ip, port=8000, monitor=True, public_mode=False, direct_origin=None):
    if not is_lan_ip(ip):
        raise ValueError('Local mode only supports private or loopback IPv4 addresses')
    keys = load_keys(directory)
    app = create_app(str(directory / 'nailong.db'), keys['admin'], keys['encryption'], monitor=monitor)
    origin = f'http://{ip}:{port}'
    app.state.public_origin = None
    app.state.public_host = None
    app.state.tunnel_error = None
    app.state.local_csrf = secrets.token_urlsafe(32)
    if direct_origin:
        if not public_mode:
            raise ValueError('Direct HTTPS requires public mode')
        set_public_origin(app, direct_origin)
    hosts = {f'{ip}:{port}', f'127.0.0.1:{port}', f'localhost:{port}'}
    if port == 80:
        hosts.update((ip, '127.0.0.1', 'localhost'))

    @app.middleware('http')
    async def local_boundary(request: Request, call_next):
        peer = request.client.host if request.client else ''
        if request.headers.get('host', '').lower() not in hosts | {app.state.public_host}:
            return JSONResponse({'detail': 'invalid_local_host'}, status_code=403)
        try:
            allowed = peer == '::1' or is_lan_ip(peer)
        except ValueError:
            allowed = False
        if not allowed:
            return JSONResponse({'detail': 'local_network_only'}, status_code=403)
        if (request.url.path.startswith('/dev/') or request.url.path.startswith('/v1/admin/')) and peer not in ('127.0.0.1', '::1'):
            return JSONResponse({'detail': 'administrator_page_is_loopback_only'}, status_code=403)
        return await call_next(request)

    @app.get('/dev/pair', response_class=HTMLResponse, include_in_schema=False)
    def pair_page(request: Request):
        if public_mode and not app.state.public_origin:
            text = app.state.tunnel_error or '正在获取公网 HTTPS 地址，请稍候…'
            return HTMLResponse('<meta charset="utf-8"><meta http-equiv="refresh" content="3"><p>' + html.escape(text)
                                + '</p>', headers={'Cache-Control': 'no-store'})
        code = secrets.token_urlsafe(32)
        with app.state.store.db() as db:
            db.execute('DELETE FROM pairs WHERE expires<=?', (time.time(),))
            db.execute('INSERT INTO pairs VALUES(?,?)', (digest(code), time.time() + 120))
            devices = db.execute('SELECT COUNT(*) FROM devices WHERE revoked=0').fetchone()[0]
            active_devices = db.execute(
                'SELECT id,name,status,device_info FROM devices WHERE revoked=0 ORDER BY rowid').fetchall()
            test_id = request.query_params.get('test', '')
            test = db.execute("SELECT * FROM jobs WHERE id=? AND kind='notification'", (test_id,)).fetchone()
        newest = active_devices[-1]['id'] if len(active_devices) > 1 else ''
        forms = ''.join(
            '<form method="post" referrerpolicy="same-origin">'
            f'<input type="hidden" name="csrf" value="{html.escape(app.state.local_csrf, quote=True)}">'
            f'<input type="hidden" name="device" value="{html.escape(device["id"], quote=True)}">'
            f'<span>{html.escape(device["name"])} · {html.escape(device["id"][-6:])} · '
            f'{html.escape(device_status_label(device["status"]))}'
            f' · {html.escape(device_info_label(device["device_info"]))} · 接收方式：轮询（未接入厂商推送）'
            f'{" · 最新" if device["id"] == newest else ""}</span> '
            '<button type="submit" formaction="/dev/notifications/test">发送测试通知</button> '
            '<button type="submit" formaction="/dev/devices/revoke">撤销</button></form>'
            for device in active_devices)
        test_status, pending = '', False
        if test:
            if test['done']:
                result = json.loads(test['result'])
                test_status = ('手机已回报：通知已提交 Android 系统，请查看手机通知栏'
                               if result.get('success') else '手机已回报失败：' + result.get('message', 'unknown'))
            elif test['expires'] <= time.time():
                test_status = '测试通知已过期，未收到完成回报，请检查手机服务、网络和通知权限'
            else:
                pending = True
                test_status = '手机已领取，等待回报' if test['lease_token'] else '已加入队列，等待手机领取'
        phone_origin = app.state.public_origin if public_mode else origin
        payload = {'version': 1, 'server': phone_origin, 'code': code, 'local_dev': not public_mode}
        qr = qrcode.make(json.dumps(payload), image_factory=qrcode.image.svg.SvgPathImage)
        svg = qr.to_string().decode()
        instructions = ('手机可用流量或任意可联网的 Wi-Fi，无需和电脑同网' if public_mode else '手机和电脑连接同一 Wi-Fi')
        warning = ('跨网测试经 Cloudflare 中转并由其终止 HTTPS，Cloudflare 能接触 sid 和设备令牌。临时地址会变化，没有长期可用性保证，不作为正式部署。'
                   if public_mode else 'HTTP 会明文传输手机 sid 和设备令牌，不要用于公网或公共 Wi-Fi。')
        if direct_origin:
            warning = '手机直接通过 HTTPS 连接云服务器，不经过临时隧道。管理页面仅允许本机访问，请使用 SSH 端口转发。'
        heading = '云服务器直连' if direct_origin else ('跨网络测试' if public_mode else '局域网测试')
        footer = ('服务由 systemd 管理；只要公网 IP、证书和设备凭据保持有效，重启无需重新绑定。'
                  if direct_origin else '无需填写域名或管理员令牌。停止服务请在终端按 Ctrl+C。' +
                  ('隧道重启后地址通常变化，需要重新绑定。' if public_mode else '地址不对时使用 --ip 指定电脑的 Wi-Fi IPv4 地址。'))
        page = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="{2 if pending else 60}">
<title>{heading}配对</title><style>body{{font:17px system-ui;max-width:620px;margin:40px auto;padding:16px}}
svg{{display:block;width:320px;height:320px;max-width:100%}}code{{overflow-wrap:anywhere}}</style>
<h1>{heading}：手机扫码即可</h1><p>{instructions}，打开新版客户端 → 扫码连接服务器 → 信任并启动</p>
<p>手机连接地址：<code>{html.escape(phone_origin)}</code></p>{svg}
<p>二维码 120 秒有效，页面每 60 秒自动刷新。已绑定设备：{devices}</p>
<h2>不需要上课：测试手机通知</h2>
<p>绑定后保持手机服务器模式运行，点击对应设备的按钮。只发送测试通知，不会调用签到接口。同一登录再次扫码会替换旧设备；若列表里仍有多台，带「最新」的是最后一次扫码，旧的可以撤销。</p>
{forms if forms else '<p>暂无已绑定设备，先扫描二维码绑定，随后刷新页面。</p>'}
<p role="status">{html.escape(test_status)}</p>
<p><strong>连接说明：</strong>{warning}</p>
<p>{footer}</p></html>'''
        return HTMLResponse(page, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                                          'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"})

    async def read_local_form(request: Request):
        if not browser_form_allowed(request.headers.get('origin'), request.headers.get('sec-fetch-site'),
                                    request.headers.get('sec-fetch-mode'), port):
            raise HTTPException(403, 'invalid_local_origin')
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 4096:
                raise HTTPException(413, 'request_too_large')
        try:
            fields = parse_qs(raw.decode('utf-8', errors='replace'), max_num_fields=10)
        except ValueError:
            raise HTTPException(400, 'invalid_form') from None
        csrf = fields.get('csrf', [''])[0]
        if not hmac.compare_digest(csrf.encode(), app.state.local_csrf.encode()):
            raise HTTPException(403, 'invalid_form_token')
        return fields

    @app.post('/dev/notifications/test', include_in_schema=False)
    async def send_test(request: Request):
        device = (await read_local_form(request)).get('device', [''])[0]
        queued = app.state.store.notify(device, '雨课堂助手：测试通知',
                                        '已收到服务器测试消息。此通知不依赖上课，也不会执行签到。')
        return RedirectResponse('/dev/pair?test=' + queued['task_id'], status_code=303)

    @app.post('/dev/devices/revoke', include_in_schema=False)
    async def revoke_local_device(request: Request):
        device = (await read_local_form(request)).get('device', [''])[0]
        with app.state.store.db() as db:
            db.execute('UPDATE devices SET revoked=1,credential=NULL WHERE id=? AND revoked=0', (device,))
        return RedirectResponse('/dev/pair', status_code=303)

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ip', help='IPv4 address the phone can reach (default: auto detect)')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--public', action='store_true', help='Create a temporary public HTTPS tunnel for cross-network tests')
    parser.add_argument('--cloudflared', help='Path to the official cloudflared executable (default: PATH)')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    executable = args.cloudflared or shutil.which('cloudflared')
    if args.public and not executable:
        parser.error('cloudflared is required. Install the official package: winget install -e --id Cloudflare.cloudflared; then reopen the terminal')
    ip = '127.0.0.1' if args.public else (args.ip or detect_ip())
    app = create_dev_app(Path.cwd() / '.local-dev', ip, args.port, public_mode=args.public)
    local_url = f'http://127.0.0.1:{args.port}/dev/pair'
    print('PUBLIC TEST: HTTPS via Cloudflare; the provider can access sid/device tokens. Temporary URL, no uptime guarantee.'
          if args.public else f'LAN TEST ONLY: HTTP sends credentials in plaintext. Phone address: http://{ip}:{args.port}')
    print(f'Open on this computer: {local_url}\nKeys and test database: .local-dev/ (keep private; do not delete between runs)')

    class DevServer(uvicorn.Server):
        tunnel = None

        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if self.started and args.public:
                from server.tunnel import QuickTunnel
                self.tunnel = QuickTunnel(app, executable)
                try:
                    public_url = await self.tunnel.start()
                    set_public_origin(app, public_url)
                    print(f'Phone public address: {public_url}')
                except Exception:
                    app.state.tunnel_error = '无法建立公网隧道，请检查 cloudflared 安装和网络后重启服务'
                    print(app.state.tunnel_error)
            if self.started and not args.no_browser:
                webbrowser.open(local_url)

        async def shutdown(self, sockets=None):
            if self.tunnel:
                await self.tunnel.close()
            await super().shutdown(sockets=sockets)

    DevServer(uvicorn.Config(app, host='127.0.0.1' if args.public else '0.0.0.0', port=args.port,
                            access_log=False, proxy_headers=False)).run()


if __name__ == '__main__':
    main()
