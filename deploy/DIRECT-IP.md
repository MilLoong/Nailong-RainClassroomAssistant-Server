# 公网 IP 直连部署 🌐

## 连接链路 🔗

- **客户端：** HTTPS 连接公网 IP，二维码自动提供 `server` 和一次性 `code`
- **Nginx：** 443 端口终止 TLS，转发至 `127.0.0.1:8001`
- **客户端网关：** 仅允许健康检查、绑定、设备任务及回执接口
- **管理页面：** `http://127.0.0.1:8000/dev/pair`，仅允许本机访问
- **状态目录：** `/opt/nailong-test/.local-dev`，保存加密密钥、设备绑定和任务，不要删除或公开

## 云主机安装 ⚙️

- **适用配置：** TencentOS 4、Python 3.11、`/opt/nailong-test` 部署目录、`nailong-test` 系统用户
- **当前模板地址：** `121.4.26.119`，迁移时必须同时修改服务单元、Nginx 配置和证书路径
- **准备条件：** 项目依赖已安装到 `/opt/nailong-test/.venv`，Nginx 已安装，Certbot 5.4 或更新版本已安装到 `/opt/nailong-certbot`
- **安全组：** 只为此服务新增入站 TCP 80、443，8000 和 8001 不开放
- **证书申请：** 必须先确认订户协议，80 端口只提供 ACME 验证文件，不处理绑定凭据
  - 如下：

    ```bash
    mkdir -p /var/lib/letsencrypt
    install -m 644 deploy/nginx-bootstrap.conf /etc/nginx/conf.d/nailong.conf
    nginx -t && systemctl enable --now nginx
    /opt/nailong-certbot/bin/certbot certonly --non-interactive \
      --agree-tos --register-unsafely-without-email \
      --preferred-profile shortlived --webroot \
      --webroot-path /var/lib/letsencrypt --ip-address 121.4.26.119
    ```

- **切换服务：** 确认证书成功签发后再执行，保留原状态目录和旧源码备份
  - 如下：

    ```bash
    install -m 644 deploy/nailong-server.service /etc/systemd/system/
    install -m 644 deploy/nailong-cert-renew.service /etc/systemd/system/
    install -m 644 deploy/nailong-cert-renew.timer /etc/systemd/system/
    install -m 644 deploy/nginx-direct.conf /etc/nginx/conf.d/nailong.conf
    nginx -t
    systemctl daemon-reload
    systemctl stop nailong-cloud-test
    systemctl enable --now nailong-server
    systemctl reload nginx
    systemctl enable --now nailong-cert-renew.timer
    ```

- **验收：** 普通 curl 验证 TLS，不使用 `-k`，续期检查必须成功
  - 如下：

    ```bash
    curl --fail https://121.4.26.119/health
    curl -o /dev/null -w '%{http_code}\n' https://121.4.26.119/dev/pair
    systemctl is-enabled nailong-server nginx nailong-cert-renew.timer
    systemctl is-active nailong-server nginx nailong-cert-renew.timer
    /opt/nailong-certbot/bin/certbot renew --dry-run
    ```
  - `/health` 应返回 `{"status":"ok"}`，公网 `/dev/pair` 应返回 404
  - IP 证书有效期约 6 天，续期定时器每天检查两次，成功续期后重新加载 Nginx
  - 安装开机自启不等于实际验证了整机重启恢复，需要在允许中断时单独测试

## 手机绑定 📱

- **电脑打开管理页面：** 已具备云主机 SSH 登录方式时建立端口转发
  - 如下：

    ```bash
    ssh -N -L 8000:127.0.0.1:8000 root@121.4.26.119
    ```
  - 浏览器打开 `http://127.0.0.1:8000/dev/pair`，本地 8000 端口须空闲
  - 尚未配置 SSH 登录时，不要开放公网管理端口，使用云终端生成二维码或先由本人完成 SSH 登录配置
- **手机操作：** 安装服务器版客户端，登录雨课堂，打开「扫码连接服务器」，扫描电脑二维码，选择「信任并启动」
- **自动配置：** 二维码内包含 HTTPS 地址和 120 秒有效的一次性绑定码，不包含管理员令牌
- **已有临时隧道绑定：** 重新扫描直连二维码，将连接地址切换为公网 IP
- **测试通知：** 管理页面找到设备后点击「发送测试通知」，检查手机通知栏和回执
- **后台运行：** 手机端当前使用前台服务和长轮询，仍需通知权限及厂商后台运行设置，不是 QQ 同等的厂商推送保活机制

## 维护 🛠️

- **日志：** `journalctl -u nailong-server -n 100 --no-pager`
- **续期日志：** `journalctl -u nailong-cert-renew -n 100 --no-pager`
- **备份：** 状态目录必须与加密密钥一起私密保存
- **服务器有效期：** 当前试用实例于 2026-11-22 16:55 到期，需要提前续费或迁移
- **部署进度：** 文档是完整操作流程，不能代替现场验收结果

## 证书参考 📚

- [Let's Encrypt：Certbot 的 IP 证书与短期证书支持](https://letsencrypt.org/2026/03/11/shorter-certs-certbot/)
