# AgentBot Windows 桌面版

此包包含后端、Python 运行时和已构建的管理前端，无需安装 Python、uv 或 Node.js。
请复制整个 `AgentBot` 文件夹，不要单独移动 EXE 或 `_internal`。

要求 Windows 10/11 x64、.NET Framework 4.6.2 以上和
[Microsoft Edge WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)。
桌面界面使用 WebView2，不会自动改用旧 IE 内核。

## 首次使用

1. 将程序解压到当前用户可写的目录，例如 `D:\Apps\AgentBot`。
2. 直接双击 `AgentBot.exe`，无需提前复制配置文件、初始化数据库或安装 Redis。
3. 首次启动自动生成 `.env`、两个独立随机密钥、默认运行配置，以及
   `data/agent_bot.sqlite3` 业务库和 `data/queue.sqlite3` 本地持久队列。
   默认仅监听 `127.0.0.1:8080`，管理员密码为空，直接进入管理界面。
4. 在管理页面登记模型供应商、模型和机器人接入地址；接入 OneBot 时使用 `.env` 中生成的 Token。

已有 `.env` 或 `.env.prod` 会继续使用，不覆盖配置、密钥或数据。
若已有数据库但配置丢失，请恢复配置备份；程序不会重新生成密钥覆盖已有部署。
手动部署可复制 `.env.example`，填写密钥后执行 `agent-bot.exe database init` 和
`agent-bot.exe settings init`；不要对已有配置重复执行初始化。

## 可选 Redis 与 MySQL

默认业务库和任务队列均使用 SQLite，机器人运行不依赖 Redis 或 MySQL。
本地队列支持单实例持久任务、重启恢复、图片取消和工具副作用去重。
Redis 是可选后端：在 `.env` 设置 `CHAT_QUEUE_BACKEND=redis`，填写 `CHAT_REDIS_*`，
启动 Redis 后重启程序。MySQL 业务库则显式设置 `CHAT_DB_BACKEND=mysql`。
队列后端切换不会迁移待处理任务，请先处理完原队列；连接失败不会自动切换后端。

## 运行与排查

- `AgentBot.exe` 启动桌面窗口及所属后端，关窗会停止该后端。
- `agent-bot.exe` 保留命令行操作；不带参数则只启动服务，可通过浏览器访问。
- 两种 EXE 都从程序目录读取 `.env` / `.env.prod`；相对数据库、媒体路径也基于此目录。
- 默认地址 `127.0.0.1:8080`，可在 `.env` 修改 `HOST`、`PORT`。端口冲突会明确失败，
  桌面窗口不会接管已经运行的其他服务。
- 桌面后端日志位于 `logs/desktop-backend.log`，每次桌面启动覆盖上一份。
  启动页会显示当前阶段；配置错误会直接显示缺失字段，等待超过 60 秒会报告超时阶段。
  启动失败时先检查日志、初始化状态、端口及 Redis；无需密码时管理页面直接进入。
- 数据库、配置、媒体和日志均不存放在 `_internal`。升级时保留这些外部文件，
  整体替换程序及 `_internal`，不要将运行数据放入构建输出目录后再次执行构建。
- QQ 接入和模型调用仍需配置 NapCat/OneBot 与模型供应商；Redis、MCP、ComfyUI 按需启用。
- 这是未签名的便携包；尚未包含安装器或自动更新功能。

## 从源码构建

在 Windows x64 安装 Python 3.12、uv、Node.js 22 后，在仓库根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File tools/build_windows.ps1
```

脚本先构建前端，再使用 `build/windows/venv` 安装锁定的非 editable 发布包，
最终生成 `dist/windows/AgentBot/AgentBot.exe`、`agent-bot.exe` 和共享 `_internal`。
只附带 `.env.example`，不会打包本地 `.env`、数据库或媒体文件。
构建目录已有运行数据时会拒绝覆盖，可通过 `-OutputDirectory dist/windows-new` 换目录。

开发依赖已安装时，可验收无配置、无 Redis 的首次启动，或可选 Redis 路径：

```powershell
uv run --no-sync python tools/check_windows.py dist/windows/AgentBot
# 同时打开实际桌面窗口，截图后自动关窗并检查后端停机：
uv run --no-sync python tools/check_windows.py dist/windows/AgentBot --first-run --gui-screenshot reports/desktop.png
```

验收不会读取部署配置或连接真实 QQ、模型和 Redis；截图验收需要交互式 Windows 桌面。

打包方式参考 [PyInstaller 文档](https://www.pyinstaller.org/en/stable/usage.html)，
桌面运行要求参考 [pywebview Web engine](https://pywebview.flowrl.com/guide/web_engine)。
