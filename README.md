# Nailong-RainClassroomAssistant-Server

将雨课堂的 **课程监听与签到放到服务端**，手机在线时同步结果，离线后重新打开应用补取通知

支持在自己的电脑或云服务器运行，使用 **Cloudflare 临时 HTTPS 隧道 + 客户端扫码绑定**，无需自备域名，手机无需与服务端连接同一 Wi-Fi

## 主要功能 🧩

- **云端签到：** 服务端监听课程，执行普通签到和定位签到
- **扫码绑定：** 自动填写服务器地址，不需要手动输入管理员令牌
- **结果通知：** 签到结果持久保存，手机恢复连接后补取遗漏通知
- **模式切换：** 切回本地监听前，先等待服务器确认暂停，避免两端同时签到
- **配置同步：** 绑定、启用服务器模式及保存设置时，同步签到延迟和定位配置
- **测试通知：** 管理页面可发送测试消息，不上课也能验证接收
- **凭据保护：** 登录凭据与定位配置加密保存，支持解绑撤销设备

## 手机绑定 📱

1. 启动服务端，打开管理页面 `http://127.0.0.1:8000/dev/pair`
2. 安装支持服务器模式的「雨课堂签到」APP，登录雨课堂
3. 点击 **扫码连接服务器**，扫描管理页面的二维码
4. 核对服务器地址，点击 **信任并启动**，按提示授予权限

- **绑定码：** 默认 120 秒有效，仅能使用一次，过期后刷新管理页面
- **定位签到：** 在客户端保存坐标和地址，配置会同步到服务器
- **恢复使用：** 地址和账号未改变时无需重新绑定，临时隧道地址变化后需要重新扫码
- **离线通知：** 息屏不保证即时通知，重新打开应用后同步结果，不影响服务器独立签到
- **信任范围：** 绑定会上传雨课堂 `sid` 和已保存的定位配置，只绑定自己的可信服务器
- **关联项目：** [Nailong-RainClassroomAssitant](https://github.com/MilLoong/Nailong-RainClassroomAssitant)

## 开发方式 🛠️

需要 Python 3.11 或 3.12，以及 [cloudflared](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/)

### Windows

- 在项目目录安装依赖并启动
  - 如下：

    ```powershell
    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    winget install -e --id Cloudflare.cloudflared
    .\.venv\Scripts\python.exe -m server.dev --public
    ```

### Linux / 云服务器

- 安装 cloudflared 后，在项目目录执行
  - 如下：

    ```bash
    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
    .venv/bin/python -m server.dev --public --no-browser
    ```

- 云端管理页面通过 SSH 转发访问，电脑浏览器打开 `http://127.0.0.1:8000/dev/pair`
  - 如下：

    ```bash
    ssh -N -L 8000:127.0.0.1:8000 <user>@<server-ip>
    ```

- **持续运行：** 服务进程和隧道需要保持运行，云端可用 systemd 管理，每个数据库只运行一个服务进程
- **状态目录：** `.local-dev/` 保存密钥、数据库与绑定状态，不要删除或公开，也不要将管理端口暴露到公网
- **运行测试：** 使用虚拟环境中的 Python 执行
  - 如下：

    ```bash
    python -m pip install pytest
    python -m pytest -q
    ```
