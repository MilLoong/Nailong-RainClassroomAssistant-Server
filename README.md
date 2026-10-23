# Nailong-RainClassroomAssistant-Server

将雨课堂的 **课程监听与普通签到放到服务端**，手机在线时同步结果，离线后重新打开应用补取通知

支持在自己的 电脑 或 云服务器 运行，主流程使用 **Cloudflare 临时 HTTPS 隧道 + 客户端扫码绑定**，无需自备域名，手机无需与服务端连接同一 Wi-Fi

> 当前为开发测试版本，新版 Android 客户端需要支持模式确认与结果同步接口；不依赖 ntfy、UnifiedPush 或厂商推送，手机息屏不保证即时通知，但不影响服务器独立执行

### 新版升级与模式切换

- **定位与延迟配置**：Android `1.1.0-dev.11` 在绑定、开启服务器模式时上传配置，保存设置后主动同步，断线后恢复连接补传；服务端加密保存坐标和地址，不额外做距离判断，仍以雨课堂接口结果为准
- **定位签到**：复用客户端的 eligibility 与 `source=54` 提交流程；未配置坐标时返回 `location_config_required`，不编造位置
- **统一签到请求**：非定位流程直接使用客户端相同的 `source: 21`、`lessonId` 和 `joinIfNotIn: true`，不因课堂标记为动态二维码而提前跳过；上游实际拒绝时记录失败，不提示手动扫码，也不伪报成功
- **配置接口**：`PUT /v1/device/checkin-config` 整体替换配置，`location: null` 清除云端定位，服务端不回传或记录明文坐标到日志

- **先升级服务器，再安装 Android `1.1.0-dev.10` 或更新版本**，旧服务器不支持模式确认，新客户端不会静默降级为本地签到
- **已有绑定可以保留**，在客户端重新选择或开启服务器模式，等待服务器确认
- **服务器模式**：服务器使用加密保存的手机 `sid` 执行普通签到；手机只同步结果，不领取签到任务
- **本地模式**：客户端先停止本地旧任务，再请求服务器暂停；服务器等待正在提交的签到结束后确认，客户端才启动本地监听
- **切换失败**：不启动另一端，显示切换未完成；请求已到达但响应丢失时服务器模式可能已改变，需要联网重试确认
- **签到去重**：切换时同步双方已成功签到的课程，避免同一课程重复提交
- **结果补取**：`GET /v1/device/events?after=0` 按递增游标分页返回，每页最多 100 条；结果持久保存，不受旧任务 180 秒有效期限制
- **打开应用恢复同步**：客户端保存设备独立游标，网络恢复时重连；系统杀死进程后需要打开应用，不承诺仅亮屏就能唤醒
- **执行结果不明**：提交前记录不确定状态，断网或进程重启不自动重试非幂等签到，交由用户核实
- **部署约束**：同一数据库只运行一个服务进程、一个监控循环；不得使用多个 Uvicorn workers 或多个副本共享数据库，模式互斥锁仅在单进程内有效
- **尚未实测**：云端真实课堂签到、手机端模式切换和离线补取仍需联调；自动化测试使用模拟上游响应
- **兼容路径**：未确认新版模式的旧设备保留 `legacy` 任务领取协议，不会因升级服务器自动启用云端签到

## 1. 工作流程 🔄

1. 电脑 或 云服务器 启动服务端，自动创建 **临时 HTTPS 地址**
2. 管理页面 生成 **一次性配对 二维码**
3. 手机登录雨课堂，扫描二维码 并 **确认 信任服务器**
4. 客户端自动读取地址、注册设备，并 **授权服务端** 使用手机端 `sid` 监听并执行普通签到
5. 客户端请求服务器模式，服务器确认后独立监听并执行签到
6. 手机通过 HTTPS 同步持久化结果，恢复运行后补取遗漏通知

- **服务端负责：** 课程监听、设备管理、任务持久化、任务交付和结果记录
- **客户端负责：** 本机登录、模式切换确认、结果同步与通知；仅本地模式在手机执行签到
- **执行边界：** 不接受任意 URL、脚本或远程命令供手机执行

## 2. 当前功能与限制 🧩

- ✅ **扫码绑定：** 自动填写地址和绑定码，不需要手动输入管理员令牌
- ✅ **凭据保护：** 监听凭据加密存储，设备令牌在数据库中只保存摘要
- ✅ **任务队列：** SQLite 持久化、课程去重、延迟领取、过期控制、领取租约和回执
- ✅ **测试通知：** 不上课也能检查任务链路与手机通知回报
- ✅ **设备信息：** 自动上报品牌、厂商、型号、Android 版本、系统构建号与应用版本
- ✅ **设备撤销：** 手机解绑或管理员撤销设备，并清除服务端监听凭据
- ⚠️ **课程监听：** 检测到开课后创建一次任务，不代表已确认课堂开放签到
- ⚠️ **后台限制：** 息屏、省电策略、强制停止或断网可能导致接收延迟
- ⚠️ **临时地址：** 隧道重启后地址通常变化，需要重新扫码绑定
- ⚠️ **未完成：** 厂商推送、动态二维码签到、课堂内追加签到事件监听

## 3. 环境准备 ⚙️

- **运行环境：** 建议 Python 3.11 或 3.12，支持 Windows 与 Linux
- **隧道工具：** 官方 `cloudflared`，用于创建 Cloudflare Quick Tunnel
- **手机客户端：** 服务器模式已合并到「雨课堂签到」APP，与本地模式共用原包名，不再维护独立服务器版客户端，旧发布 APK 需要更新
- **关联项目：**
  - [Nailong-RainClassroomAssitant](https://github.com/MilLoong/Nailong-RainClassroomAssitant)
  - [Android-Scan-DynamicQRCode-Of-RainClassroom-APP](https://github.com/MilLoong/Android-Scan-DynamicQRCode-Of-RainClassroom-APP)，私有仓库需要相应访问权限
  - Android 工程同时提供本地与服务器模式，客户端与服务端分开构建，本仓库不包含客户端 APK 发布产物

### 3.1 Windows 🖥️

- 在项目目录安装依赖和隧道工具
  - 如下：

    ```powershell
    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    winget install -e --id Cloudflare.cloudflared
    ```

- 安装后重新打开终端，用 `cloudflared --version` 确认可执行文件可用
- 不需要激活虚拟环境，直接使用虚拟环境内的 Python 即可

### 3.2 Linux 或云服务器 ☁️

- 将项目放到固定目录并安装依赖
  - 如下：

    ```bash
    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
    cloudflared --version
    ```

- 缺少 `venv` 或 `pip` 时，先安装系统对应的 Python 组件
- `cloudflared` 请按 [Cloudflare 官方说明](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/)安装
- 建议用普通服务账号运行，确保项目状态目录可写

## 4. 本机与云端启动 🌐

### 4.1 自己的电脑 💻

- **Windows：**
  - 如下：

    ```powershell
    .\.venv\Scripts\python.exe -m server.dev --public
    ```

- **Linux：**
  - 如下：

    ```bash
    .venv/bin/python -m server.dev --public
    ```

- 程序自动创建隧道，并尝试打开管理页面 `http://127.0.0.1:8000/dev/pair`
- 等待页面显示公网 HTTPS 地址与二维码，再用手机扫描
- 手机可以使用移动数据或其他 Wi-Fi，不要求与电脑同网
- 电脑、服务进程和隧道必须持续运行，电脑关机后服务停止

### 4.2 云服务器 ☁️

- 在云服务器项目目录使用相同入口，不自动打开浏览器
  - 如下：

    ```bash
    .venv/bin/python -m server.dev --public --no-browser
    ```

- 服务运行在云主机，自己的电脑不需要一直开机
- **前台命令不是后台部署：** 关闭 SSH 会话可能结束前台进程，需要持续运行时配置 systemd 等服务管理器
- **管理页面仅允许回环访问：** 已有云主机 SSH 登录方式时，在自己的电脑建立端口转发
  - 如下：

    ```bash
    ssh -N -L 8000:127.0.0.1:8000 <user>@<server-ip>
    ```
  - 替换实际 SSH 用户与云主机地址，按需使用已有密钥或登录方式
  - 保持转发连接，电脑浏览器打开 `http://127.0.0.1:8000/dev/pair`
  - 电脑的 8000 端口须空闲，若服务端修改端口，转发两端端口也保持一致
  - SSH 转发仅用于管理，手机直接访问二维码中的 HTTPS 地址
- **不要公开管理端口：** 无需向公网开放 8000，隧道仅公开设备接口

### 4.3 云端后台运行示例 🔧

- 以下单元须按实际用户名和路径调整，再保存至 `/etc/systemd/system/nailong-tunnel.service`
  - 示例假设项目在 `/opt/nailong-server`，普通系统用户 `nailong` 已创建
  - 该用户须能读取项目、运行虚拟环境，并写入 `.local-dev/`
  - 确认 `cloudflared` 的实际安装路径，示例使用 `/usr/bin/cloudflared`

    ```ini
    [Unit]
    Description=Nailong RainClassroom temporary HTTPS tunnel
    Wants=network-online.target
    After=network-online.target

    [Service]
    User=nailong
    WorkingDirectory=/opt/nailong-server
    Environment=PYTHONUNBUFFERED=1
    ExecStart=/opt/nailong-server/.venv/bin/python -m server.dev --public --no-browser --cloudflared /usr/bin/cloudflared
    Restart=always
    RestartSec=10
    TimeoutStopSec=45
    UMask=0077

    [Install]
    WantedBy=multi-user.target
    ```

- 启用并查看运行状态
  - 如下：

    ```bash
    sudo systemctl daemon-reload
    sudo systemctl enable --now nailong-tunnel
    systemctl status nailong-tunnel --no-pager
    journalctl -u nailong-tunnel -f
    ```

- **开机自启不等于地址固定：** 服务重启后仍需检查临时地址并重新绑定
- 不要在相同端口同时启动 `server.dev`、`server.hosted` 或其他实例
- 临时隧道适合开发测试，不提供持续可用性保证

### 4.4 常用参数 🛠️

- `--no-browser`：不自动打开本机浏览器
- `--port 8001`：修改主服务端口，管理页面改用相应端口
- `--cloudflared <path>`：指定官方 `cloudflared` 的可执行文件路径
- **停止运行：** 前台按 `Ctrl+C`，systemd 部署使用 `systemctl stop nailong-tunnel`
- **状态位置：** 当前工作目录下的 `.local-dev/`，更换工作目录会使用另一套状态

## 5. 手机扫码绑定 📱

1. 安装 客户端，登录 雨课堂
2. 打开 **扫码连接服务器**，扫描管理页面的二维码
3. 核对地址是自己的服务器，点击 **信任并启动**
4. 按系统提示授予相机或通知权限，客户端自动保存配置并启动服务器模式

- **无需手填：** 域名、地址、管理员令牌、配对码或电脑端会话
- **绑定码：** 默认 120 秒有效，仅能兑换一次，过期后刷新管理页面重新扫描
- **恢复连接：** 地址和账号会话未改变时，点击「恢复服务器模式」即可
- **地址变化：** 重新扫码，同一手机登录会话的新绑定会替换旧设备
- **本地模式：** 选择「停止并切回本地模式」
- **设备信息：** 新版客户端在绑定和启动服务器模式时自动上报，不采集 IMEI、序列号或 Android ID
- **推送状态：** 品牌识别只提供候选通道，当前仍为轮询，不代表已启用厂商推送

## 6. 不上课也能测试通知 🔔

1. 手机保持服务器模式运行
2. 在电脑管理页面找到对应设备，点击 **发送测试通知**
3. 检查手机通知栏与管理页面回执

- **独立测试：** 不需要正在上课，不调用签到接口
- **新版通知：** 持久保存到结果流，手机恢复同步后展示，不受 120 秒限制；管理页保存成功不等于手机已展示
- **旧版任务时限：** 立即可领取，创建后 120 秒过期
- **旧版成功回执：** `test_notification_posted` 表示已提交 Android 系统，不等于用户看到横幅或读到通知
- **测试顺序：** 先亮屏确认，再测试返回桌面、息屏、断网恢复和应用重启

## 7. 安全与数据保存 🔐

- **手机端凭据：** 明确确认信任后上传 `sid`，新版服务器模式用于云端监听与普通签到
- **账号权限：** `sid` 具有账号访问能力，并非只读专用令牌，只绑定自己的可信服务器
- **中转方：** Cloudflare 终止 HTTPS，技术上能够接触传输的 `sid`、设备令牌和任务
- **密钥文件：** `.local-dev/keys.json` 保存管理员密钥和凭据加密密钥
- **数据库：** `.local-dev/nailong.db` 保存设备、加密凭据与任务
- **备份：** 保留整个状态目录，不要删除或更换解密已有凭据所需的加密密钥
- **禁止公开：** 状态目录、登录凭据、管理员令牌、有效配对二维码、数据库、控制台截图和临时源码包
- **管理隔离：** 隧道不公开 `/dev/pair`、`/v1/admin/*`、`/docs` 或 `/openapi.json`
- **解绑：** 撤销设备并清除监听凭据，仅清除手机本地状态不会自动撤销云端设备

## 8. 任务语义与边界 ⏱️

- **监听接口：** `/api/v3/classroom/on-lesson`，支持手机端 `sid` 与兼容的电脑端 `sessionid`
- **轮询间隔：** `POLL_INTERVAL` 默认 10 秒，最低 3 秒
- **签到任务：** 默认发现课程 15 秒后执行，创建后 180 秒未执行则过期；旧版客户端领取租约 45 秒
- **课程去重：** `(device, lessonId)` 唯一约束跨重启保留，同一设备同一节课创建一个任务
- **租约重投：** 未完成且未过期的任务可在租约到期后重新领取，旧租约不能提交结果
- **执行语义：** 新版在提交前持久化不确定状态，不自动重试结果未知的签到；无法保证跨系统严格恰好一次
- **旧版交付语义：** 至少一次，旧客户端持久化任务执行记录
- **失败终态：** `success=false` 为终态，不盲目重复签到
- **状态含义：** `listening` 表示上一次课程轮询成功，不代表手机当前在线或息屏可唤醒
- **课堂事件：** 现有课堂 WebSocket 握手依赖签到返回的 `lessonToken`，签到前事件监听尚未完成
- **单实例：** 只运行一个应用实例，不使用多个 worker 同时监听同一数据库
- **历史记录：** 尚无自动归档，不要清空去重记录后继续监听同一节课

## 9. 接口概览 🔌

- **绑定：** `POST /v1/pair`，请求包含 `code`、`name`、可选 `monitor_sid`、`device_info`
  - 返回 `device_id`、仅返回一次的 `device_token`、`protocol_version: 2` 和 `monitor_configured`
  - 二维码格式仍为 `version: 1`，与响应协议版本分开处理
  - 过期或已用绑定码返回 400，监听凭据拒绝返回 401，上游验证网络失败返回 502
  - 省略 `monitor_sid` 仍可仅注册设备，新客户端不会视为监听已配置
- **设备鉴权：** `Authorization: Bearer <device_token>`
- **设备信息：** `PUT /v1/device/info`，更新品牌、型号和版本元数据
- **模式确认：** `PUT /v1/device/mode`，`mode` 为 `server` 或 `local`，可附带 `checked_lessons`，返回服务器已成功签到课程与确认模式
- **结果同步：** `GET /v1/device/events?after=0`，返回 `events`、`cursor`、`mode` 和服务器最近监听状态
- **旧版领取任务：** `GET /v1/device/tasks/next?wait=25`，最多等待 30 秒
  - 无任务返回 `{"task":null}`，有任务包含 `id`、`kind`、`payload`、`expires_at`、`lease_token`
  - `checkin` 为签到任务，`notification` 为独立通知测试
- **任务回执：** `POST /v1/device/tasks/{id}/result`，请求包含 `lease_token`、`success`、`message`
- **手机解绑：** `DELETE /v1/device`
- **管理员接口：** 使用独立的管理员 Bearer 令牌
  - `POST /v1/admin/pairings`：生成绑定码
  - `GET /v1/admin/devices`：查看设备状态、设备信息与推送候选状态
  - `POST /v1/admin/devices/{device}/notifications/test`：发送测试通知，传 `{}` 使用默认文案
  - `DELETE /v1/admin/devices/{device}`：撤销设备
- **电脑端兼容：** `PUT /v1/device/credentials`，传入 `sessionid` 后切换为电脑端监听凭据
- **数据升级：** 自动为旧数据库补充新增字段，旧客户端未上传元数据时保持为空，不影响已有绑定

## 10. 其他入口与结构 📁

- `server/app.py`：监听、数据库、鉴权与任务接口
- `server/dev.py`：管理页面、扫码入口、局域网与临时隧道启动
- `server/tunnel.py`：隧道进程管理与设备接口网关
- `server/pair.py`：面向可达管理员接口的高级二维码生成工具
- `server/hosted.py`：保留的固定 HTTPS 代理部署入口，不是临时隧道主流程
- `deploy/`：固定地址部署参考配置，包含环境相关地址与路径，复用前必须调整
- `tests/`：服务端自动化测试
- `Dockerfile`：基础 API 容器入口，不自动创建隧道或管理页面

- **局域网调试：** `python -m server.dev`，手机需要能访问电脑局域网地址，通常连接同一 Wi-Fi
  - 使用 HTTP，仅限可信局域网与显式允许本地 HTTP 的 Debug 客户端
  - 不要将这个 HTTP 端口转发到公网
- **独立 API：** `server.app:create_app`，自行配置 HTTPS 与管理员访问控制
  - 需要不少于 32 字符的 `ADMIN_TOKEN`，以及 Fernet 格式的 `ENCRYPTION_KEY`
  - `DATABASE_PATH` 默认 `nailong.db`，与测试入口的 `.local-dev/nailong.db` 不同
  - 不应直接公开全部管理员接口

## 11. 开发与验证 🧪

- **运行服务端测试：**
  - 如下：

    ```bash
    python -m pip install -r requirements.txt pytest
    python -m pytest -q
    ```

- **自动化结果：** 服务端 40 项测试通过，配套客户端 6 项单元测试通过，Debug APK 已构建
- **云端联调：** 已验证 HTTPS、扫码绑定、任务领取、测试通知回执与设备信息上报
- **尚未验收：** 真实课堂完整签到、动态二维码、息屏可靠交付和厂商推送
- 自动化通过或通知回执成功，不等于真实课堂签到已验证成功

## 12. TODO 📝

- [ ] **厂商推送与 FCM：非必需，暂缓开发**
  - 接入对应 SDK、配置包名和签名，申请平台要求的应用凭据
  - 获取设备推送 `token`，实现注册、刷新、失效与解绑处理
  - 按设备实际能力选择已配置通道，不只按品牌名称判断
  - 如需息屏即时通知，可推送结果同步提示，客户端不承担服务器模式的签到执行
  - 核实通知展示、透传和后台执行权限，验证配额及业务开通资格
  - 完成息屏、Doze、进程终止和断网恢复真机测试
- [ ] 动态二维码签到与课堂内追加签到事件适配
- [ ] 断线退避、异常诊断和后台状态可视化
- [ ] 任务历史归档与去重记录保留策略
- [ ] 固定地址部署配置通用化，移除环境相关硬编码
- [ ] 完善本机和云端端到端自动化验证
- [ ] 可能会用 **Server 酱** 等服务来进行 通知？不过前面如果能做好就用前面

## 13. 参考资料 📚

- [Cloudflare Quick Tunnels](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/)
- [cloudflared 下载与安装](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/)
- [Android Doze 与 App Standby](https://developer.android.com/training/monitoring-device-state/doze-standby)
- [FCM 消息优先级](https://firebase.google.com/docs/cloud-messaging/android-message-priority)
