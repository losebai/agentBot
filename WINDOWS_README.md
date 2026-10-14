# AgentBot Windows 桌面版

此包包含后端、Python 运行时和已构建的管理前端，无需安装 Python、uv 或 Node.js。
可以运行 `AgentBot-<版本>-windows-x64-Setup.exe` 安装，或复制整个便携版 `AgentBot` 文件夹。
便携版不要单独移动 EXE 或 `_internal`。

要求 Windows 10/11 x64、.NET Framework 4.6.2 以上和
[Microsoft Edge WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)。
桌面界面使用 WebView2，不会自动改用旧 IE 内核。

## 一键安装

当前发布版本为 **0.5.0**，命令始终读取下载站 `latest.json` 中的可用版本。

Windows PowerShell：

```powershell
irm https://public.loserbai.cn/agentbot/install.ps1 | iex
```

Windows CMD：

```cmd
curl -fsSL -o "%TEMP%\agentbot-install.bat" https://public.loserbai.cn/agentbot/install.bat && "%TEMP%\agentbot-install.bat"
```

脚本从同一下载站读取 `latest.json`，下载对应 Windows x64 安装器并校验 SHA256，
校验通过后显示安装进度，创建桌面和开始菜单快捷方式。安装完成后手动打开 AgentBot；
不会自动重启系统。下载、校验或安装失败会明确报错，不切换下载源。
无需管理员权限；已有安装使用原目录升级并保留配置、数据库和媒体。
桌面运行前仍需安装上方列出的 WebView2 Runtime。

### 检查与更新版本

再次执行相同的一键安装命令即可检查更新：

- 未安装：下载并安装服务器当前版本。
- 已安装旧版：显示本机版本与服务器版本，校验安装包后在原目录升级。
- 版本相同：提示已是最新版，不重复下载或安装。
- 本机版本高于服务器版本：保留本机版本，不自动降级。

版本号采用 `主版本.次版本.修订号`，按数字分段比较，例如 `0.5.10` 高于 `0.5.9`。
本机版本来自当前用户 Windows“已安装的应用”中的 AgentBot 登记；安装成功后，
安装器自动更新登记的版本，升级失败时脚本不提前写入新版本。
检测只适用于通过安装器安装的桌面版，便携版继续按下方说明手动升级。

## 首次使用

1. 运行安装器，默认安装到 `%LOCALAPPDATA%\Programs\AgentBot`，无需管理员权限，
   可创建开始菜单与桌面快捷方式；便携版解压到当前用户可写的目录，例如 `D:\Apps\AgentBot`。
2. 直接双击 `AgentBot.exe`，无需提前复制配置文件、初始化数据库或安装 Redis。
3. 首次启动自动生成 `.env`、两个独立随机密钥、默认运行配置，以及
   `data/agent_bot.sqlite3` 业务库和 `data/queue.sqlite3` 本地持久队列。
   OneBot Token 与会话密钥均保存在 `.env`。
   默认仅监听 `127.0.0.1:8080`，管理员密码为空，直接进入管理界面。
4. 在管理页面登记模型供应商、模型和机器人接入地址；可在“机器人配置 → QQ”填写新 OneBot Token。
   保存到 `.env` 后保持当前连接，再手动“服务 → 重启后端”使其生效。
   NapCat Access Token 设置为相同值。

已有 `.env` 或 `.env.prod` 会继续使用，不覆盖配置、密钥或数据。
若已有数据库但配置丢失，请恢复配置备份；程序不会重新生成密钥覆盖已有部署。
手动部署可复制 `.env.example`，填写静态 OneBot Token 和会话密钥后执行 `agent-bot.exe database init` 和
`agent-bot.exe settings init`；不要对已有配置重复执行初始化。
OneBot Token 与会话密钥须使用至少 32 字符的不同随机值。

已有 `.env`／环境变量中的 `ONEBOT_V11_ACCESS_TOKEN` 继续使用，修改后重启后端。
会话密钥仍由静态 `CHAT_SESSION_SECRET` 提供。页面提供的静态字段可在严格授权、
存在启动文件且无环境覆盖时保存，之后手动重启；环境覆盖或缺少启动文件时相应字段只读；写入权限不足时保存明确报错。

## 可选 Redis 与 MySQL

默认业务库和任务队列均使用 SQLite，机器人运行不依赖 Redis 或 MySQL。
本地队列支持单实例持久任务、重启恢复、图片取消和工具副作用去重。
Redis 是可选后端：在 `.env` 设置 `CHAT_QUEUE_BACKEND=redis`，填写 `CHAT_REDIS_*`，
启动 Redis 后重启程序。MySQL 业务库则显式设置 `CHAT_DB_BACKEND=mysql`。
队列后端切换不会迁移待处理任务，请先处理完原队列；连接失败不会自动切换后端。

## 运行与排查

- `AgentBot.exe` 启动桌面窗口及所属后端，关窗会停止该后端。
- 桌面窗口顶部“服务 → 重启后端”可应用启动配置变更，无需关闭整个窗口。
- “服务 → 修改端口…”用弹窗填写 1-65535 的端口，保存后自动重启并连接新地址。
  新端口被占用时保留当前服务与配置；取消弹窗不作修改。后端启动失败时菜单仍可使用。
  端口保存到 `.env.prod`（存在时）或 `.env`；若 `PORT` 由环境变量固定，需先移除环境变量。
- OneBot Token 在“机器人配置 → QQ”中编辑，不回填旧值；留空保留原值。
  保存使用启动文件版本校验，仅修改已编辑的启动字段，保留未编辑的配置和会话密钥；
  同时可提交其他分类的运行草稿。保存不切换当前连接，手动“服务 → 重启后端”后生效，
  NapCat 同步使用新 Token。网页不持有进程控制能力。
  环境变量覆盖或启动文件不可写时 Token 字段只读，需先调整启动配置或文件权限。
- 其他静态启动字段也可在页面允许时编辑；保存写入启动文件，保持当前连接、监听地址
  和数据库后端，手动“服务 → 重启后端”后应用。静态密钥留空保留；可空密钥须明确清除。
  会话密钥修改并重启后会使用新的会话与身份命名空间；旧历史和记忆数据保留，
  供应商与 MCP 密钥不受影响。
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
- 安装器和便携包均未签名，未包含自动更新功能；安装器升级只替换程序资源，
  不覆盖 `.env`、数据库或媒体。卸载保留这些运行数据，确认不再需要后可自行删除。

## 从源码构建

在 Windows x64 安装 Python 3.12、uv、Node.js 22 后，在仓库根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File tools/build_windows.ps1
# 安装 Inno Setup 6 后，同时生成安装器与 SHA256 校验文件：
powershell -ExecutionPolicy Bypass -File tools/build_windows.ps1 -BuildInstaller
```

脚本先构建前端，再使用 `build/windows/venv` 安装锁定的非 editable 发布包，
最终生成 `dist/windows/AgentBot/AgentBot.exe`、`agent-bot.exe` 和共享 `_internal`。
只附带 `.env.example`，不会打包本地 `.env`、数据库或媒体文件。
构建目录已有运行数据时会拒绝覆盖，可通过 `-OutputDirectory dist/windows-new` 换目录。
安装器、SHA256 校验文件、`latest.json` 和两种一键安装脚本输出到
`dist/windows/installer/`；编译器不在标准路径时可使用 `-IsccPath` 或
`ISCC_PATH` 环境变量指定 `ISCC.exe`。安装器不捆绑 WebView2 Runtime，目标电脑需提前安装。
GitLab 作业与 Windows Runner 准备步骤见 [CI/CD 部署说明](deploy/README.md)。

开发依赖已安装时，可验收无配置、无 Redis 的首次启动，或可选 Redis 路径：

```powershell
uv run --no-sync python tools/check_windows.py dist/windows/AgentBot
# 同时打开实际桌面窗口，截图后自动关窗并检查后端停机：
uv run --no-sync python tools/check_windows.py dist/windows/AgentBot --first-run --gui-screenshot reports/desktop.png
```

验收不会读取部署配置或连接真实 QQ、模型和 Redis；截图验收需要交互式 Windows 桌面。

打包方式参考 [PyInstaller 文档](https://www.pyinstaller.org/en/stable/usage.html)，
桌面运行要求参考 [pywebview Web engine](https://pywebview.flowrl.com/guide/web_engine)。
