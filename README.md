# agentBot

基于 Python、NoneBot 和 OneBot V11 的机器人服务，提供 React 浏览器管理界面。支持普通对话、多轮 Agent、Skill、MCP、图片理解、图片与对话音频生成，以及 ComfyUI 和 GPT-SoVITS 工具。

管理界面集中提供机器人配置、供应商和模型管理、Agent/Skill/MCP 配置、对话测试及调用记录。当前使用纯 Python 服务，支持普通对话、Agent、媒体、一次性 Job 和自动发言决策。视频、通用 Laya 指令和 SolonCode 暂未迁移。

## 项目结构

```text
src/
  __main__.py       服务入口和离线命令分发
  application.py    NoneBot、管理 API 和静态界面装配
  admin/            管理 API、认证和流式对话
  ai/               模型供应商、普通对话和后端路由
  agent/            Agent、Skill、MCP 和工具执行
  media/            图片、音频、附件存储和媒体记录
  services/         聊天、记忆、工作流和任务队列等业务服务
  integrations/     OneBot、Redis、ComfyUI 和 GPT-SoVITS 集成
  plugin/           机器人事件处理和运行生命周期
  resources/        内置工作流资源
  static/           前端构建产物
web/src/            React/TypeScript 管理界面
tests/              后端测试、替身和夹具
sql/agent_bot_init.sql  完整数据库初始化脚本
tools/              开发安装映射和包检查工具
docs/               设计文档
```

Python 源码平铺在 `src/`，通过构建配置和开发安装钩子映射为 `agent_bot` 包。代码导入使用 `agent_bot.*`，NoneBot 插件路径为 `agent_bot.plugin`。请通过 `uv sync` 安装项目，再使用 `agent-bot` 或 `python -m agent_bot` 入口。

## 环境要求

- Python 3.12 和 uv。
- MySQL 和 Redis，用于运行配置、记忆、记录及任务队列。
- Node.js 22，用于前端开发和源码构建；安装已构建的 wheel 后无需 Node.js。
- QQ 接入需要 NapCat，并使用 OneBot V11 反向 WebSocket。
- 自动发言需要一个提供 `laya_predict` 的 MCP 服务，通过管理端目录登记。
- 使用本地绘图或语音工具时，分别需要 ComfyUI 或 GPT-SoVITS。

## 安装与首次运行

以下命令均在仓库根目录执行。

### 1. 安装依赖并构建界面

```powershell
uv sync --extra dev --python 3.12
npm --prefix web ci
npm --prefix web run build
uv run agent-bot --help
```

前端构建产物写入 `src/static/`，由后端提供。不要手动编辑生成文件。

### 2. 初始化数据库

准备独立的 `agent_bot` 数据库和专用服务账户，使用数据库管理账户执行 [统一初始化脚本](sql/agent_bot_init.sql)：

```powershell
mysql -h 127.0.0.1 -u root -p agent_bot --execute="source sql/agent_bot_init.sql"
```

脚本创建全部 28 张业务表，覆盖配置、群成员、聊天记忆、标签记忆、工作流、模型、Agent、Skill、MCP、媒体、Job 和自动发言决策记录。它不插入运行配置或模型；服务不会自动执行该脚本。

### 3. 配置环境

从 [.env.example](.env.example) 创建本地配置文件：

```powershell
if (Test-Path .env) { throw ".env 已存在，请直接编辑已有文件" }
Copy-Item .env.example .env
```

填写数据库、Redis 连接参数及所需运行配置。示例中的 Token 是无效占位符；运行以下命令三次，分别生成管理员凭据、OneBot Token 和会话密钥，并填写 `AGENT_BOT_ADMIN_TOKEN`、`ONEBOT_V11_ACCESS_TOKEN` 和 `CHAT_SESSION_SECRET`：

```powershell
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"
```

三项凭据使用不同的值。管理员凭据用于浏览器管理；OneBot Token 用于 NapCat 连接；会话密钥用于会话和记忆身份。需要机器人响应的用户或群，应配置对应的允许名单；示例默认不允许所有会话。

| 配置项 | 用途和存储位置 |
| --- | --- |
| `CHAT_DB_*` | MySQL 启动连接参数，保留在 `.env` 或进程环境 |
| `CHAT_REDIS_*` | Redis 连接和键前缀，保留在 `.env` 或进程环境 |
| `AGENT_BOT_ADMIN_TOKEN` | 管理员凭据，只从 `.env` 或进程环境读取 |
| 模型供应商配置 | 每个供应商独立的 URL、Key 和协议，通过管理页面保存到 MySQL |
| MCP 服务配置 | 每个服务独立的地址、访问令牌与工具白名单，通过管理页面保存到 MySQL |
| `AGENT_BOT_MEDIA_DIRECTORY` | 媒体文件启动目录，默认 `data/media`，不参与热刷新 |
| 机器人运行配置 | 首次从环境导入 MySQL，之后通过管理界面或离线命令修改 |

启动参数中环境变量优先于本地 `.env`。供应商和 MCP 配置及凭据只读取 MySQL，不读取环境变量或 `.env`，不提交真实凭据到仓库。每个实例使用独立数据库、Redis 键前缀和媒体目录。

### 4. 导入运行配置

```powershell
uv run agent-bot settings import-env
uv run agent-bot settings list
```

`import-env` 只允许导入空配置表。成功后，MySQL 是机器人运行配置的权威来源，修改 `.env` 中已入库的字段不会更新当前配置。

命令失败时会提示需要检查的配置字段或数据库错误类别，不回显凭据。出现“无法连接 MySQL”时，先确认 MySQL 正在运行且 `CHAT_DB_HOST`、`CHAT_DB_PORT` 正确；连接成功后再检查数据库、初始化表和账户权限。配置校验失败时按提示修改 `.env`，不要把示例中的 Token 占位符作为实际凭据。

导入后可移除 `.env` 中已入库的运行字段，但继续保留数据库和 Redis 引导参数、管理员凭据及媒体目录。运行配置包含敏感值，应限制数据库账户权限。

### 5. 启动服务并连接 QQ

```powershell
uv run agent-bot
```

也可使用 `uv run python -m agent_bot`。该入口同时启动机器人、管理 API 和浏览器界面。

默认管理地址为 [http://127.0.0.1:8080/](http://127.0.0.1:8080/)，用管理员凭据登录。实际监听地址由数据库中的 `host`、`port` 决定。

NapCat 配置 OneBot V11 反向 WebSocket 地址 `ws://127.0.0.1:8080/onebot/v11/ws`，Access Token 与 `ONEBOT_V11_ACCESS_TOKEN` 一致。不要同时运行两个连接同一机器人和队列前缀的实例。

## 管理界面与运行配置

工作台提供运行概览、机器人配置、对话测试、供应商管理、模型管理、会话记录、Agent 管理、Skill 管理和 MCP 服务。保存配置后可查看数据库版本、应用版本及需重启字段；热刷新只应用可在线修改的字段。关闭浏览器不会停止服务。

模型统一在本地目录登记，QQ 功能按各自开关启用。无需设置普通文本、Agent 或媒体的后端选择字段。模型或工具调用失败会返回对应错误，未迁移能力会在路由预检时拒绝。

旧外部 AI 地址、令牌和后端选择配置已删除。已有数据库快照中的旧字段在读取时忽略，下次保存配置时清理；升级代码后需重启服务。历史 SQL 仅用于归档，新库不再创建旧模型兼容目录。

## 配置 Python 普通对话

1. 在“供应商管理”新增供应商，直接填写名称、协议、API 根地址和 API 密钥，保存到 MySQL。支持 `openai-responses`、`openai-chat` 和 `ollama`，无需鉴权的本地服务可留空密钥。可以新增多个供应商，每个供应商有自己的 URL 和 Key。
2. API 根地址不包含 `/responses`、`/chat/completions` 或 `/api/chat`。保存后自动获取该供应商的上游模型列表，也可点击“获取模型 / 刷新模型”。兼容协议读取 `/models`，Ollama 读取 `/api/tags`；服务未实现列表接口时明确提示错误。
3. 从上游列表搜索并勾选模型，设置唯一的本地别名后导入；新模型默认停用，需要在“模型管理”启用。也可在模型管理选择供应商后，从自动获取的列表选择准确上游 ID，或显式手动登记。不同供应商的同名模型使用不同别名，并分别绑定各自供应商。
4. 将 `chat_model` 设置为已启用的模型别名。在“对话测试”验证同步或流式回复；临时模型选择只影响本次请求。
5. 编辑已有供应商时，密钥不会回填；留空保留，填写新值替换，明确勾选清除才移除。保存后下一次请求生效，无需重启，已开始执行的请求继续使用原快照。

既有库先执行 `sql/2026-10-13-dynamic-provider-mcp-credentials.sql`，只增加字段并保留旧表和数据。旧环境变量引用需要在页面重新录入密钥；不会自动导入 `.env`。新库直接使用统一初始化脚本。

模型服务需支持所选协议和生成参数。不支持的参数会明确报错，不会更换参数后重试。记忆压缩请求固定关闭深度思考；当前 Ollama 可表达该选项，其他协议不支持时会拒绝压缩并保留已有记忆。

会话记录包含调用正文、回复和元数据。记录写入失败时仍返回已经获得的模型结果，并提示未保存；无需重发。取消的轮次保留部分回复和取消状态，不加入成功历史。

## 配置 Python Agent 和工具

1. 完成供应商和模型登记，选择支持原生工具调用的模型。
2. 按需在“MCP 服务”登记 Streamable HTTP 或 SSE 地址、工具白名单和访问令牌。网页搜索使用 `search` 分类，填写搜索工具名称，并配置 `chat_python_search_mcp_code`；`read_url` 只读取公网网页。
3. 按需创建 Skill，再创建 Agent，选择模型、工具和 Skill。Agent 可关联最多 3 个通用 MCP 服务，工具轮次预算为 1–5；Skill 所需工具也必须在 Agent 中授权。
4. 在“对话测试 → Agent 对话”选择 Agent；执行步骤和结果可在“会话记录 → Agent 运行记录”查看。
5. QQ 使用指定 Agent 时设置 `chat_python_agent_code`；留空时使用当前 Chat 模型、提示词和预算。QQ 使用的通用 MCP 服务还需在 `chat_mcp_services` 中授权。每个 MCP 服务独立保存地址与令牌，编辑令牌留空保留、明确清除才删除，保存后新请求立即使用新配置。

QQ 工具包括标签记忆、永劫无间查询、群成员提及、本地工作流和 TTS；网页工具为 `web_search`、`read_url`。QQ 本地工具链需要启用 `chat_mcp_enabled`，通过会话短期令牌校验权限。

管理 Agent 可使用网页、授权的 MCP 和已启用的 `local_workflow_list`、`local_workflow_generate_image`、`tts_generate`；管理入口不接受客户端提供的 QQ 令牌、用户或群身份。

Job 默认关闭，启用后作为 QQ 专属工具执行。管理 Agent 不接受 QQ Job 工具；SolonCode 和通用 Laya 指令尚未迁移。自动发言的固定 Laya 决策已由独立 Python 服务接入。

停止请求、切换模式、离开对话页或退出登录会取消当前管理请求。已经完成的工具操作不会撤销；结果无法确认时记录为 `unknown`。残留的 `running` 记录不一定表示任务仍在执行。

## 配置图片和音频

1. 在“模型管理”显式启用图片理解、图片生成接口类型（`gpt-image` 或 `legacy-b64`）及对话音频能力。Ollama 支持图片理解，不提供这里的直接图片/音频生成；`legacy-b64` 输出仅支持 PNG。
2. 按需启用图片理解、图片生成或 TTS 对应功能开关，媒体调用统一由 Python 处理。
3. 根据部署位置设置 `AGENT_BOT_MEDIA_DIRECTORY`，默认 `data/media`。目录应由本实例独占且可读写，不使用符号链接或重解析点。修改启动目录后重启服务，再查看概览中的媒体就绪状态。
4. 在“对话测试 → 媒体生成”生成图片或对话音频；在“Agent 对话”上传图片，也可仅提交图片。

ComfyUI 工作流需要配置 `chat_local_workflow_*` 并启用本地工作流；GPT-SoVITS 需要配置 `chat_tts_*` 并启用 TTS。Agent 还需授权对应工具。

同步工作流目录并查看已登记工作流：

```powershell
uv run agent-bot workflows sync "D:\workflows"
uv run agent-bot workflows list
```

将示例路径替换为实际工作流目录。省略 `sync` 的目录参数时，使用数据库中的 `chat_local_workflow_directory`。

图片单张原始数据最多 10 MiB，一次合计 20 MiB；数量默认 4 张且不超过 8 张，不支持动画。文件默认保留 24 小时，共用 1 GiB 配额；预览和下载需要管理员凭据，到期后返回 410。

附件只绑定一次执行，再次识图需要重新上传；历史只回传文字。工具已生成文件而最终回复失败或取消时，界面仍保留产物及状态提示，并允许下载未过期附件。

## Python Job 与自动发言

已有数据库先手动执行 [Job 与决策增量 SQL](sql/2026-10-12-python-jobs-auto-speak.sql)，增加 `agent_bot_job_owner`、`agent_bot_job`、`agent_bot_auto_speak_decision` 三张表。新库直接使用统一脚本。代码不会自动建表，也不会复制旧 Java 待执行任务；切换前应处理旧任务，避免两套服务重复投递。

在“机器人配置”启用 `chat_mcp_enabled` 和 `chat_job_enabled`；前者的挂载变更需要重启。若 QQ 使用 Agent 预设，还须在其工具列表中授权 `job`。模型可以创建、查询、列出和取消当前发起人的一次性任务；每条请求最多创建一项，每人每会话最多 50 项活动任务，延时范围为 1 秒至 365 天。绝对时间必须带时区。

Job 到期使用创建时的模型别名和系统提示快照，执行一次普通文本模型调用。结果先持久保存，再由 QQ 队列投递；读取不等于消费，完整发送后才确认。中断执行标记失败，不自动重放模型调用；取消执行中的任务阻止结果发布，但已产生的调用费用无法撤回。

自动发言在“MCP 服务”登记已启用的 `general` 服务，工具白名单至少包含 `laya_predict`；访问令牌直接在管理页面填写并保存到数据库。在“机器人配置”填写 `chat_auto_speak_mcp_code`、机器人 QQ 号和最多 20 个目标群，开启群近期上下文，保留至少 2 个 AI 并发名额，再启用 `chat_auto_speak_enabled`。`interval` 为定时判断（最短 60 秒），`message` 为合格群消息触发。决策只接受固定 `speak/silent` 的结构化结果，异常或取消时保持静默；决策记录不保存群聊输入正文。

Job 执行检查及结果轮询间隔由 `chat_job_poll_seconds` 控制。首次导入后以上配置通过管理界面保存并热刷新。实现和验证范围见 [迁移记录](docs/python-jobs-auto-speak-migration.md)。

## 开发与验证

GitLab CI、Docker 构建、服务器变量和回滚流程见 [CI/CD 部署说明](docs/gitlab-cicd.md)。流水线检查通过后才允许构建镜像与手动生产部署。

前端开发：

```powershell
npm --prefix web run dev
```

Vite 将 `/api` 和 `/health` 代理到 `http://127.0.0.1:8080`。修改后端端口时，同时调整 `web/vite.config.ts`。

按修改范围选择检查：

```powershell
uv run ruff check .
npm --prefix web run typecheck
npm --prefix web test -- --run
```

完整后端离线测试：

```powershell
uv run pytest -m "not e2e" -k "not database_onebot_token_reaches_adapter_without_log_disclosure" --allow-hosts=127.0.0.1,::1
```

测试使用 Fake/Mock，不连接 QQ、不调用付费模型或生产服务。Windows 事件循环需要本机 socketpair，因此允许 loopback。命令排除了会清空真实 MySQL 配置表的联调测试；完整覆盖率门槛为 80%。文档修改只需核对内容、命令和路径。

## 构建与安装发布包

```powershell
npm --prefix web run build
uv build
uv run python tools/check_package.py dist/agent_bot-0.5.0-py3-none-any.whl
```

wheel 文件名随 `pyproject.toml` 的版本变化。包检查会核对运行模块、静态界面和 SQL 资源，并排除凭据、缓存、日志及媒体文件。

在部署目录创建环境并安装构建出的 wheel：

```powershell
uv venv --python 3.12
uv pip install "D:\packages\agent_bot-0.5.0-py3-none-any.whl"
.venv\Scripts\agent-bot.exe --help
```

将示例 wheel 路径替换为实际文件。部署目录需提供 `.env`，完成数据库初始化后，用安装后的入口导入配置并启动服务：

```powershell
.venv\Scripts\agent-bot.exe settings import-env
.venv\Scripts\agent-bot.exe settings list
.venv\Scripts\agent-bot.exe
```

已构建的界面随 wheel 提供。

安装 wheel 后可通过 `importlib.resources.files('agent_bot').joinpath('resources/agent_bot_init.sql')` 读取完整初始化 SQL。源码开发时直接使用仓库中的 `sql/agent_bot_init.sql`。
