# AgentBot Windows 桌面版

此包包含后端、Python 运行时和已构建的管理前端，无需安装 Python、uv 或 Node.js。
可以运行 `AgentBot-<版本>-windows-x64-Setup.exe` 安装，或复制整个便携版 `AgentBot` 文件夹。
便携版不要单独移动 EXE 或 `_internal`。

要求 Windows 10/11 x64、.NET Framework 4.6.2 以上和
[Microsoft Edge WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)。
桌面界面使用 WebView2，不会自动改用旧 IE 内核。

## 一键安装

当前发布版本为 **0.6.2**，命令始终读取下载站 `latest.json` 中的可用版本。

0.6.2 将 NapCat 改为独立 Node 运行方式，首次登录后记住账号，后续启动尝试快速登录。
从已完成数据库升级的 0.6.1 更新无需新增 SQL 迁移，保留配置、数据库和登录资料。
[本版升级说明](https://public.loserbai.cn/agentbot/UPGRADE-0.6.2.md) 与
[SQL 下载包](https://public.loserbai.cn/agentbot/AgentBot-0.6.2-sql.zip) 提供完整升级信息。

0.5.5 增加长期记忆版本、来源、冲突与遗忘管理，并统一模型身份与选择界面。
从 0.5.4 升级前须停服备份，按
[升级说明](https://public.loserbai.cn/agentbot/UPGRADE-0.5.5.md) 完成模型身份与记忆生命周期迁移。
[本版 SQL 下载包](https://public.loserbai.cn/agentbot/AgentBot-0.5.5-sql.zip) 同时提供
MySQL、SQLite 完整初始化、适用增量、升级说明与四个迁移工具；启动先按明确的版本清单自动升级适用结构，失败停止启动。

0.5.4 统一会话上下文、普通调用归档和长期标签记忆存储，并更新素材与队列管理。
从 0.5.3 升级须先停服、备份，按照
[升级说明](https://public.loserbai.cn/agentbot/UPGRADE-0.5.4.md) 执行对应数据库迁移。
[SQL 下载包](https://public.loserbai.cn/agentbot/AgentBot-0.5.4-sql.zip) 包含完整初始化 SQL、
增量 SQL、升级说明和两个离线迁移工具；未登记的历史数据转换仍按对应版本说明处理。

0.5.3 新增持久素材及图片记忆。已有部署升级前请备份业务数据库和媒体目录，
在所选业务库执行 `2026-10-17-material-memory.sql`（MySQL）或
`2026-10-17-material-memory.sqlite.sql`（SQLite）；脚本位于安装目录的
`_internal/agent_bot/resources/`。普通启动只升级所选业务库的已登记增量，不对独立队列库执行。

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
4. 在管理页面登记模型供应商、模型和机器人接入地址；可在“插件管理 → QQ 配置”填写新 OneBot Token。
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

## NapCat 桌面托管

在管理页面进入“插件管理 → QQ 配置”，点击“启动 NapCat”，就绪后直接扫描本页二维码完成 QQ 登录。
页面显示实际运行的管理端口；二维码支持刷新，并显示扫码确认、连接中和已登录状态。
首次成功登录后默认记住账号，之后启动尝试快速登录；已有自动登录账号不覆盖，不保存 QQ 密码。
后台会完成记忆，不要求 QQ 配置页保持打开；凭据失效时仍需扫码或安全验证。
“打开后台管理界面”会使用本机默认浏览器，按用户授权将密钥编码到 URL 一键登录。
桌面不再提供 QQ 菜单。启动窗口和查看状态不会自动启动 QQ；启动仅作用于本窗口所属的 NapCat，
不改写静态模式配置、不重启后端。启用 QQ 插件的选择仍须保存并手动重启后端后生效。
普通 `agent-bot.exe` 命令行服务保持外部接入，不提供桌面 NapCat 启动和登录能力。

桌面包携带固定版本 NapCat Node 运行资源，无需用户安装 Node 或另外启动 NapCat。
点击“启动 NapCat”后，AgentBot 在后台隐藏启动配套 Node 进程；二维码直接显示在项目内。
不定位、注入或修改系统安装的 QQ，也不接管其他已运行的 QQ 或 NapCat。
进程属于本窗口的 Windows Job，退出或重启后端时只清理所属进程树。
运行资源缺失或校验失败时明确提示安装包含 Node 运行包的新版桌面程序，不回退为 QQ 注入。

后台管理 URL 仅由所属桌面根据本次活动端口和本机密钥构造，不接受网页指定地址；
密钥和 WebUI 短期凭据不通过管理 API 返回、不写入项目日志。扫码响应禁止缓存。运行概览分别显示
NapCat 状态和实际 QQ 连接；“运行中”不代表已完成登录。
QQ 插件的启动和登录请求沿用管理员认证，并仅接受本机同源页面；不接受程序路径、端口或任意命令。
禁用 QQ 插件后不能启动 NapCat。缺运行文件时插件内给出明确错误，管理页面仍可使用。

`AGENT_BOT_NAPCAT_MODE=managed` 和 `AGENT_BOT_NAPCAT_WEBUI_PORT=6099`
属于静态配置，网页只读；端口可在启动文件修改后手动重启。
WebUI 只监听本机回环，端口被占用时明确报错，不自动换端口或连接其他实例。
OneBot Token 仍使用现有配置；页面保存新 Token 后保持当前连接，重启时才同步到所属 NapCat。
托管模式请清空正向 OneBot 地址与 HTTP API 根地址，避免重复连接。

NapCat 配置、锁和缓存保存到 `data/napcat/`，日志为 `logs/napcat.log`，
QQ SDK 会话资料固定使用 `data/napcat/qq/`，不更改系统 QQ 资料路径。
建议使用专用机器人账号。关窗只清理本窗口所属进程树，不按名称关闭其他 QQ。
升级和卸载保留用户资料；不要把实际配置、缓存、登录记录或日志放进程序载荷。

标准源码及 wheel 不附带第三方运行程序。制作集成 NapCat 的 Windows 包时，
构建者使用官方固定 `v4.18.38` 的 `NapCat.Shell.Windows.Node.zip`，配套 Node 为 `v22.11.0`。
将解压后的资源放在 `prepared/node/`，保留根目录原生 DLL、`wrapper.node`、`node.exe`、
`index.js`、`package.json`、`config.json`、`win64/` 和 `napcat/` 的程序资源。
排除 `napcat/config/`，并移除 `.bat`、`.ps1`、`NapCatWinBootMain.exe`、
`NapCatWinBootHook.dll`、`loadNapCat.js` 等不使用的注入启动文件；不得包含运行数据。
实际 OneBot、WebUI 配置继续保存在 `data/napcat/config/`，既有密钥保留。
Node 使用 `data/napcat/` 作为工作目录，用户资料环境指向 `data/napcat/profile/`。
项目维护的 Node 入口会在 SDK 会话初始化前固定原生资料路径为 `data/napcat/qq/`，
避免 Windows 原生目录发现绕过环境变量而采用日常 QQ 的资料目录；入口随载荷校验。
这些目录是运行数据隔离，不是操作系统权限沙箱；第三方进程仍以当前 Windows 用户权限运行。

```powershell
uv run python tools/prepare_napcat_payload.py prepared runtime/napcat-node
powershell -ExecutionPolicy Bypass -File tools/build_windows.ps1 -NapCatPayloadDirectory runtime/napcat-node
```

准备工具生成第 3 版逐文件 SHA256 清单，固定 Node 运行方式、NapCat 和 Node 版本，
拒绝旧注入清单、缺失原生组件、用户配置、目录链接及额外文件；不下载或执行第三方程序。
清单不能代替下载来源核验，构建者应同时核对官方发布包摘要并确认分发许可。
载荷源目录须位于构建输出之外；源与目标目录重叠时明确失败。

新程序载荷使用 `runtime/napcat-node/`，与旧 `runtime/napcat/` 分开，升级无需删除旧目录。
已有 `data/napcat/` 配置和密钥保持原位；不导入日常 QQ 登录资料。
本机升级后手动“服务 → 重启后端”，随后在 QQ 配置点击启动和登录。
真实扫码及 OneBot 收发须另用隔离测试账号验收，离线检查不连接 QQ。

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
- OneBot Token 在“插件管理 → QQ 配置”中编辑，不回填旧值；留空保留原值。
  保存使用启动文件版本校验，仅修改已编辑的启动字段，保留未编辑的配置和会话密钥；
  同时可提交其他分类的运行草稿。保存不切换当前连接，手动“服务 → 重启后端”后生效，
  NapCat 同步使用新 Token。QQ 插件仅可启动所属 NapCat 和请求登录，不提供通用进程控制。
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
