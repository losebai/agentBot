# agentBot

基于 Python、NoneBot 和 OneBot V11 的机器人服务，提供 React 浏览器管理界面。支持普通对话、多轮 Agent、Skill、MCP、图片理解、图片与对话音频生成，以及 ComfyUI 和 GPT-SoVITS 工具。

管理界面集中提供机器人配置、供应商和模型管理、Agent/Skill/MCP 配置、对话测试及调用记录。普通对话、Agent 和媒体可以分别使用 Python 或 Java 后端；视频、通用任务、Laya 和代码执行由 Java `dispatch-ai` 处理。

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
  integrations/     OneBot、Redis、Java AI、ComfyUI 和 GPT-SoVITS 集成
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
- 选择 Java 后端或使用 Java 专属能力时，需要运行 `dispatch-ai`。
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

脚本创建全部 25 张业务表，覆盖配置、群成员、聊天记忆、标签记忆、工作流、模型、Agent、Skill、MCP 和媒体记录。它不插入运行配置或模型；服务不会自动执行该脚本。

### 3. 配置环境

从 [.env.example](.env.example) 创建本地配置文件：

```powershell
if (Test-Path .env) { throw ".env 已存在，请直接编辑已有文件" }
Copy-Item .env.example .env
```

填写数据库、Redis 连接参数及所需运行配置。示例中的 Token 是无效占位符；运行以下命令三次，分别生成管理员凭据、OneBot Token 和会话密钥，并填写 `AGENT_BOT_ADMIN_TOKEN`、`ONEBOT_V11_ACCESS_TOKEN` 和 `DISPATCH_CHAT_SESSION_SECRET`：

```powershell
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"
```

三项凭据使用不同的值。管理员凭据用于浏览器管理；OneBot Token 用于 NapCat 连接；会话密钥用于会话和记忆身份。需要机器人响应的用户或群，应配置对应的允许名单；示例默认不允许所有会话。

| 配置项 | 用途和存储位置 |
| --- | --- |
| `DISPATCH_CHAT_DB_*` | MySQL 启动连接参数，保留在 `.env` 或进程环境 |
| `DISPATCH_CHAT_REDIS_*` | Redis 连接和键前缀，保留在 `.env` 或进程环境 |
| `AGENT_BOT_ADMIN_TOKEN` | 管理员凭据，只从 `.env` 或进程环境读取 |
| `AGENT_BOT_PROVIDER_*` | Python 模型供应商凭据，数据库只保存引用名 |
| `AGENT_BOT_MCP_*` | 远程 MCP 凭据，数据库只保存引用名 |
| `AGENT_BOT_MEDIA_DIRECTORY` | 媒体文件启动目录，默认 `data/media`，不参与热刷新 |
| 机器人运行配置 | 首次从环境导入 MySQL，之后通过管理界面或离线命令修改 |

环境变量优先于本地 `.env`。供应商和 MCP 凭据不做变量插值，不提交真实凭据到仓库。每个实例使用独立数据库、Redis 键前缀和媒体目录。

### 4. 导入运行配置

```powershell
uv run agent-bot settings import-env
uv run agent-bot settings list
```

`import-env` 只允许导入空配置表。成功后，MySQL 是机器人运行配置的权威来源，修改 `.env` 中已入库的字段不会更新当前配置。

导入后可移除 `.env` 中已入库的运行字段，但继续保留数据库和 Redis 引导参数、管理员凭据、媒体目录及所需的供应商/MCP 凭据。运行配置包含敏感值，应限制数据库账户权限。

### 5. 启动服务并连接 QQ

```powershell
uv run agent-bot
```

也可使用 `uv run python -m agent_bot`。该入口同时启动机器人、管理 API 和浏览器界面。

默认管理地址为 [http://127.0.0.1:8080/](http://127.0.0.1:8080/)，用管理员凭据登录。实际监听地址由数据库中的 `host`、`port` 决定。

NapCat 配置 OneBot V11 反向 WebSocket 地址 `ws://127.0.0.1:8080/onebot/v11/ws`，Access Token 与 `ONEBOT_V11_ACCESS_TOKEN` 一致。不要同时运行两个连接同一机器人和队列前缀的实例。

## 管理界面与后端选择

工作台提供运行概览、机器人配置、对话测试、供应商管理、模型管理、会话记录、Agent 管理、Skill 管理和 MCP 服务。保存配置后可查看数据库版本、应用版本及需重启字段；热刷新只应用可在线修改的字段。关闭浏览器不会停止服务。

三个后端开关默认均为 `java`，使用 Python 时在“机器人配置”中分别切换：

| 配置字段 | 控制的能力 |
| --- | --- |
| `dispatch_chat_plain_ai_backend` | 不使用工具的普通文本对话 |
| `dispatch_chat_agent_backend` | Agent、联网和工具调用 |
| `dispatch_chat_media_backend` | 图片理解、图片/音频生成和媒体工具 |

纯图片理解由媒体开关控制；图片或媒体工具与 Agent 混用时，需要媒体和 Agent 两个后端均为 `python`。包含视频、Job、SolonCode 或 Laya 的 QQ 请求由 Java 处理。

Python 和 Java 分别登记自己的可用模型。选择 Python 后，调用失败不会自动切换到 Java，也不会自动重发模型请求或工具操作。

## 配置 Python 普通对话

1. 在 `.env` 或进程环境中添加供应商凭据，例如 `AGENT_BOT_PROVIDER_QWEN_KEY`；本机无凭据服务可以不填写引用。
2. 在“供应商管理”登记协议、API 根地址和凭据引用名，并启用供应商。支持 `openai-responses`、`openai-chat` 和 `ollama`。填写 API 根地址，不填写完整的 `/responses`、`/chat/completions` 或 `/api/chat` 端点。
3. 在“模型管理”登记本地别名、精确上游模型 ID 和所属供应商，并启用模型。模型 ID 按服务端实际名称填写，程序不自动拼接前缀。
4. 将 `dispatch_chat_plain_ai_backend` 设置为 `python`，将 `dispatch_chat_model` 设置为已启用的模型别名。
5. 在“对话测试”刷新模型状态，测试同步或流式回复。管理测试中的临时模型选择只影响本次请求。

模型服务需支持所选协议和生成参数。不支持的参数会明确报错，不会更换参数后重试。记忆压缩请求固定关闭深度思考；当前 Ollama 可表达该选项，其他协议不支持时会拒绝压缩并保留已有记忆。

会话记录包含调用正文、回复和元数据。记录写入失败时仍返回已经获得的模型结果，并提示未保存；无需重发。取消的轮次保留部分回复和取消状态，不加入成功历史。

## 配置 Python Agent 和工具

1. 完成供应商和模型登记，选择支持原生工具调用的模型。
2. 按需在“MCP 服务”登记 Streamable HTTP 或 SSE 地址、工具白名单和凭据引用名。网页搜索使用 `search` 分类，填写搜索工具名称，并配置 `dispatch_chat_python_search_mcp_code`；`read_url` 只读取公网网页。
3. 按需创建 Skill，再创建 Agent，选择模型、工具和 Skill。Agent 可关联最多 3 个通用 MCP 服务，工具轮次预算为 1–5；Skill 所需工具也必须在 Agent 中授权。
4. 将 `dispatch_chat_agent_backend` 设置为 `python`，在“对话测试 → Agent 对话”选择 Agent；执行步骤和结果可在“会话记录 → Agent 运行记录”查看。
5. QQ 使用指定 Agent 时设置 `dispatch_chat_python_agent_code`；留空时使用当前 Chat 模型、提示词和预算。QQ 使用的通用 MCP 服务还需在 `dispatch_chat_mcp_services` 中授权。

QQ 工具包括标签记忆、永劫无间查询、群成员提及、本地工作流和 TTS；网页工具为 `web_search`、`read_url`。QQ 本地工具链需要启用 `dispatch_chat_mcp_enabled`，通过会话短期令牌校验权限。

管理 Agent 可使用网页、授权的 MCP 和已启用的 `local_workflow_list`、`local_workflow_generate_image`、`tts_generate`；管理入口不接受客户端提供的 QQ 令牌、用户或群身份。

Job、SolonCode 和 Laya 默认关闭。启用这些 Java 专属能力时，QQ 请求使用 Java；管理界面的 Python Agent 不接受这类工具。本地工作流或 TTS 与 Agent 混用时还需将媒体后端设为 `python`。

停止请求、切换模式、离开对话页或退出登录会取消当前管理请求。已经完成的工具操作不会撤销；结果无法确认时记录为 `unknown`。残留的 `running` 记录不一定表示任务仍在执行。

## 配置图片和音频

1. 在“模型管理”显式启用图片理解、图片生成接口类型（`gpt-image` 或 `legacy-b64`）及对话音频能力。Ollama 支持图片理解，不提供这里的直接图片/音频生成；`legacy-b64` 输出仅支持 PNG。
2. 将 `dispatch_chat_media_backend` 设置为 `python`。使用图片 Agent 或媒体工具时，同时将 Agent 后端设为 `python`。
3. 根据部署位置设置 `AGENT_BOT_MEDIA_DIRECTORY`，默认 `data/media`。目录应由本实例独占且可读写，不使用符号链接或重解析点。修改启动目录后重启服务，再查看概览中的媒体就绪状态。
4. 在“对话测试 → 媒体生成”生成图片或对话音频；在“Agent 对话”上传图片，也可仅提交图片。

ComfyUI 工作流需要配置 `dispatch_chat_local_workflow_*` 并启用本地工作流；GPT-SoVITS 需要配置 `dispatch_chat_tts_*` 并启用 TTS。Agent 还需授权对应工具。

同步工作流目录并查看已登记工作流：

```powershell
uv run agent-bot workflows sync "D:\workflows"
uv run agent-bot workflows list
```

将示例路径替换为实际工作流目录。省略 `sync` 的目录参数时，使用数据库中的 `dispatch_chat_local_workflow_directory`。

图片单张原始数据最多 10 MiB，一次合计 20 MiB；数量默认 4 张且不超过 8 张，不支持动画。文件默认保留 24 小时，共用 1 GiB 配额；预览和下载需要管理员凭据，到期后返回 410。

附件只绑定一次执行，再次识图需要重新上传；历史只回传文字。工具已生成文件而最终回复失败或取消时，界面仍保留产物及状态提示，并允许下载未过期附件。

## Java 后端和自动发言

使用 Java 后端时，将 `DISPATCH_CHAT_AI_BASE_URL` 配置为 `dispatch-ai` 服务地址，并在 `ai_model` 表中登记 Java AI 已启用的模型。填写实际 `model_id`、`type='chat'` 和 `enabled=1`；该目录不保存供应商凭据，Java AI 对模型可用性做最终校验。

Java Agent 使用机器人工具时，需在 Java 服务启用 `dispatch.ai.chat-mcp.enabled`，将 `dispatch.ai.chat-mcp.url` 指向机器人 `/mcp` 地址，并在机器人配置中允许 Java 实际使用的 MCP 主机名。该链路使用短期工具令牌，不使用管理员凭据。

自动发言通过 `dispatch_chat_auto_speak_enabled` 启用。`dispatch_chat_auto_speak_trigger_mode=interval` 为定时触发，`message` 为群消息触发；群消息模式还要求启用 MCP、Laya 和群最近上下文。私聊不使用群消息触发规则，目标群和用户通过对应自动发言配置指定。

## 开发与验证

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
uv run python tools/check_package.py dist/agent_bot-0.4.0-py3-none-any.whl
```

wheel 文件名随 `pyproject.toml` 的版本变化。包检查会核对运行模块、静态界面和 SQL 资源，并排除凭据、缓存、日志及媒体文件。

在部署目录创建环境并安装构建出的 wheel：

```powershell
uv venv --python 3.12
uv pip install "D:\packages\agent_bot-0.4.0-py3-none-any.whl"
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
