# agentBot

基于 Python、NoneBot 和 OneBot V11 的机器人服务，提供 React 浏览器管理界面。支持普通对话、多轮 Agent、Skill、MCP、图片理解、图片与对话音频生成，以及 ComfyUI 和 GPT-SoVITS 工具。

管理界面集中提供机器人配置、供应商和模型管理、Agent/Skill/MCP 配置、对话测试及调用记录。当前使用纯 Python 服务，支持普通对话、Agent、媒体、一次性 Job 和自动发言决策。视频、通用 Laya 指令和 SolonCode 暂未迁移。

普通文本和 Agent 的模型调用统一通过 LangChain，显式支持 Chat Completions、Responses 和 Ollama 三种协议。Agent 由 LangGraph 编排模型调用、整批工具校验、顺序执行和收尾回答；业务工具通过 StructuredTool 接入，继续使用项目的 QQ 权限、MCP 会话、媒体作用域、总超时及取消机制。模型地址和凭据来自数据库快照，SDK 不自动重试或切换协议，外部 LangSmith tracing 在调用期间关闭。依赖由 `uv.lock` 固定，更新后重启服务加载新执行路径。

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
- MySQL 或内置 SQLite，用于运行配置、记忆及业务记录；Redis 用于任务队列。
- Node.js 22，用于前端开发和源码构建；安装已构建的 wheel 后无需 Node.js。
- QQ 接入需要 NapCat，支持 OneBot V11 正向或反向 WebSocket。
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

### 2. 配置静态启动参数

从 [.env.example](.env.example) 创建本地配置文件，已有文件不要覆盖：

```powershell
if (Test-Path .env) { throw ".env 已存在，请直接编辑已有文件" }
Copy-Item .env.example .env
```

新安装示例默认 `CHAT_DB_BACKEND=sqlite`，数据库保存到 `CHAT_DB_SQLITE_PATH=data/agent_bot.sqlite3`，不需要 MySQL 服务。使用 MySQL 时显式设置 `CHAT_DB_BACKEND=mysql`，填写 `CHAT_DB_HOST`、`CHAT_DB_PORT`、`CHAT_DB_USER`、`CHAT_DB_PASSWORD`、`CHAT_DB_NAME`，并先创建独立数据库及专用账户。未指定后端仍默认为 MySQL，保持旧部署兼容；连接失败不会切换数据库。SQLite 适合单实例部署，Redis 仍负责队列。

填写 Redis 连接参数。管理员密码沿用 `AGENT_BOT_ADMIN_TOKEN` 配置名，支持自定义 8–256 位字母、数字及常见符号，不含空格；含 `#` 等 dotenv 特殊字符时用引号包裹。留空或不配置时直接进入系统，所有管理接口均无需密码。已有随机令牌仍可作为登录密码。

OneBot Token 和会话密钥仍需使用不同随机值。示例 Token 是无效占位符，运行以下命令两次，分别填写 `ONEBOT_V11_ACCESS_TOKEN`、`CHAT_SESSION_SECRET`：

```powershell
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"
```

| 配置分类 | 内容 | 存储与生效方式 |
| --- | --- | --- |
| 静态引导 | 数据库类型与连接、Redis、管理员凭据、媒体目录 | `.env`／环境变量，修改后重启 |
| 静态机器人与资源 | 驱动、监听地址、OneBot Token 和签名密钥、会话密钥、命令匹配、MCP 挂载、目录、连接池、固定队列参数 | `.env`／环境变量，管理页面只读，修改后重启 |
| OneBot 接入地址 | 正向 WebSocket 地址、HTTP API 根地址 | 所选数据库，管理页面可修改，重启适配器连接后生效 |
| 运行行为 | 机器人名称、模型别名、提示词、访问名单、业务开关、生成参数、历史与记忆策略、任务及自动发言行为 | 所选数据库，管理页面或离线命令保存后热刷新 |
| 供应商／MCP 目录 | 每条记录独立的 URL、协议、密钥和工具权限 | 所选数据库，管理页面录入，新请求使用最新配置 |

静态参数中环境变量优先于 `.env`，管理员密码环境变量显式留空也会关闭登录校验；动态环境变量不会覆盖数据库。供应商和 MCP 地址及凭据不从环境变量读取。管理员密码、OneBot Token 和会话密钥职责不同，设置时使用不同的值。每个实例使用独立数据库、Redis 键前缀及媒体目录，不提交真实凭据或运行文件。

### 3. 显式初始化数据库

配置后执行：

```powershell
uv run agent-bot database init
```

命令按所选后端创建全部 27 张业务表，覆盖配置、群成员、记忆、工作流、模型、Agent、Skill、MCP、媒体、Job 和记录。SQLite 会在明确初始化时创建文件及父目录，并启用外键和 WAL；普通启动只打开已有文件。MySQL 需先创建数据库，初始化账户需要建表权限。

两种脚本分别为 [MySQL 初始化](sql/agent_bot_init.sql) 和 [SQLite 初始化](sql/agent_bot_init.sqlite.sql)。重复初始化保留数据，不插入配置或模型，也不修正已有不兼容结构。服务启动不会自动执行完整初始化或媒体 SQL。

### 4. 初始化运行配置与迁移旧配置

新安装写入默认运行配置，再查看有效配置：

```powershell
uv run agent-bot settings init
uv run agent-bot settings list
```

默认访问策略不允许所有会话；启动后在“运行配置”设置允许的群和用户，再登记模型供应商及模型。初始化命令不读取动态环境值；若需要导入已有旧 `.env` 的业务参数，可用 `settings import-env` 代替 `settings init`。两者只允许首次初始化，重复执行拒绝覆盖。配置表只保存动态字段，静态字段始终保留在启动文件中。

已有混合配置表升级前先停止服务，使用现有数据库连接配置导出历史静态行：

```powershell
uv run agent-bot settings export-static .env.static-export
```

导出包含 OneBot Token、会话密钥等敏感值；终端只报告路径，目标文件必须不存在。备份当前 `.env` 后，把导出的静态字段合并到启动文件，保留数据库、Redis、管理员凭据和媒体目录；确认 `CHAT_DB_BACKEND=mysql` 后启动。不要在合并前运行配置保存：新版本读取时忽略历史静态行，下一次动态保存会清理这些行。导出文件合并后按部署权限管理，勿提交。

静态 OneBot Token 或会话密钥缺失时启动明确失败，不会从数据库回填。管理员密码可选，非空时须满足上述格式要求。运行配置缺失或数据库结构不匹配时按错误提示初始化或升级；SQLite 错误需检查文件路径、目录权限与结构，MySQL 错误需检查服务、账户与数据库。错误不回显配置值或驱动原文。

切换 `CHAT_DB_BACKEND` 只选择另一份独立数据库，不迁移业务数据；本次不提供 MySQL／SQLite 数据互迁。旧 MySQL 库从 `dispatch_chat_*` 改名后若缺少当前索引，执行 `sql/2026-10-13-chat-index-names.sql` 原位升级，保留历史记录。

### 5. 启动服务并连接 QQ

```powershell
uv run agent-bot
```

也可使用 `uv run python -m agent_bot`。该入口同时启动机器人、管理 API 和浏览器界面。

默认管理地址为 [http://127.0.0.1:8080/](http://127.0.0.1:8080/)，设置管理员密码时输入密码登录，密码留空时直接进入工作台。密码只保留在本次页面内存，退出或刷新后重新判断登录模式。实际监听地址由静态启动配置 `HOST`、`PORT` 决定。

使用正向 WebSocket 时，在管理页面的“运行配置”中保存 `onebot_v11_ws_urls`，例如 `["ws://10.0.8.6:3001/"]`；启动时从数据库读取，旧 `.env` 中的空地址不会覆盖数据库。`onebot_v11_api_roots` 同样保存在数据库。地址修改后需重启适配器连接，页面会显示重启提示。静态 `DRIVER` 使用 `~fastapi+~websockets`（新默认值），NapCat Access Token 与静态 `ONEBOT_V11_ACCESS_TOKEN` 一致。

使用反向 WebSocket 时，在 NapCat 配置地址 `ws://127.0.0.1:8080/onebot/v11/ws`，跨机器连接时改用 agentBot 所在主机地址。不要同时运行两个连接同一机器人和队列前缀的实例。

## 管理界面与运行配置

工作台提供运行概览、机器人配置、对话测试、供应商与模型、会话记录、Agent 管理、Skill 管理和 MCP 服务。“供应商与模型”左侧选择供应商，右侧显示其模型及启用开关；保存供应商后自动获取并保存全部模型，无需二次导入。供应商、Agent、Skill、MCP 的新增与编辑在弹窗中填写；取消丢弃草稿，保存失败保留输入并提示错误，保存成功关闭弹窗并刷新列表。机器人配置分为“运行配置”、“Agent 运行配置”和“静态配置”：运行字段保存后显示数据库与应用版本，静态字段只读；修改启动文件后需重启。关闭浏览器不会停止服务。

模型统一在本地目录登记，QQ 功能按各自开关启用。无需设置普通文本、Agent 或媒体的后端选择字段。模型或工具调用失败会返回对应错误，未迁移能力会在路由预检时拒绝。

旧外部 AI 地址、令牌和后端选择配置已删除。已有数据库快照中的旧字段在读取时忽略，下次保存配置时清理；升级代码后需重启服务。历史 SQL 仅用于归档，新库不再创建旧模型兼容目录。

会话记录分为“调用记录”、“系统调用”和“媒体运行记录”。系统调用集中显示自动发言决策及机器人显式指令（如总结、查询）触发的模型调用，支持按“自动发言决策”或“指令调用”筛选；普通调用页排除这些记录。所有详情在弹窗内查看。新决策只归档脱敏结果，不保存用于判断的群聊状态正文。

已有独立决策记录可手动执行 `uv run agent-bot records migrate` 回填到统一记录表（需已安装当前数据库结构）。命令按来源去重，重复执行不覆盖已有记录；普通启动不会自动回填。历史指令仅在已有请求标识或指令输入能明确识别时归入系统调用。

统一调用记录的 `create_time` 使用北京时间（UTC+8），与旧系统历史记录保持一致；原始会话、Agent 执行及自动发言决策表的 `created_at` 继续使用 UTC。升级后重启服务，再执行 `uv run agent-bot records repair-times`，按原始 UTC 时间校正已有 Python 调用记录。命令仅修改统一记录的时间，分批提交且可重复执行，不会重复增加 8 小时；原始记录已不存在的归档不会被猜测性修改。校正后刷新记录并回到第一页，原分页游标应丢弃。

## 配置模型与对话

1. 在“供应商与模型”点击“新增供应商”，在弹窗中填写名称、协议、API 根地址和 API 密钥，保存到所选数据库。支持 `openai-responses`、`openai-chat` 和 `ollama`，无需鉴权的本地服务可留空密钥。可以新增多个供应商，每个供应商有自己的 URL 和 Key。
2. API 根地址不包含 `/responses`、`/chat/completions` 或 `/api/chat`。保存后自动获取并保存该供应商的全部上游模型，也可点击“刷新模型”再次同步。兼容协议读取 `/models`，Ollama 读取 `/api/tags`；服务未实现列表接口时明确提示错误。同步失败保留供应商和已完成批次，点击“刷新模型”可继续，无需重复新增供应商。
3. 新模型默认停用，在右侧列表直接切换“是否启用”。新模型别名直接使用完整上游模型 ID，例如 `glm`，不附加供应商编号。别名须全局唯一，同名冲突会报错；可通过“修改别名”调整已有模型（包括旧的 `1/glm`）。修改后，机器人配置中引用的旧别名需同步更新，Agent 通过记录 ID 绑定，无需重选。重复同步跳过已有模型，保留别名、启用状态和媒体配置；不删除上游已下架的本地模型，可自行停用。
4. 将 `chat_model` 设置为已启用的模型别名。在“对话测试”验证同步或流式回复；临时模型选择只影响本次请求。
5. 编辑已有供应商时，密钥不会回填；留空保留，填写新值替换，明确勾选清除才移除。保存后下一次请求生效，无需重启，已开始执行的请求继续使用原快照。

QQ 对话入队前检查实际使用的模型；模型未登记或未启用时直接返回固定配置错误，不调用模型、不创建任务。已排队请求遇到模型失效时也会报错并清理任务，不进入死信；上游请求拒绝、协议错误等执行故障仍按原队列策略处理。

QQ 日常聊天统一进入 Agent，包括关闭联网搜索、未授权工具的情况；无工具时由 Agent 直接生成回答。普通文本模型仅用于总结等现有专用指令和内部辅助生成，`/chat`、`/聊天` 仍不开放。管理页面的普通对话测试入口保留。

机器人配置中的系统提示词 `chat_system_prompt` 允许留空；保存空值后不会恢复默认提示词，调用时省略这条系统消息。Agent 自身配置的提示词和必要的工具规则仍然生效。

既有库先执行 `sql/2026-10-13-dynamic-provider-mcp-credentials.sql`，只增加字段并保留旧表和数据。旧环境变量引用需要在页面重新录入密钥；不会自动导入 `.env`。新库直接使用统一初始化脚本。

模型服务需支持所选协议和生成参数。不支持的参数会明确报错，不会更换参数后重试。记忆压缩请求固定关闭深度思考；当前 Ollama 可表达该选项，其他协议不支持时会拒绝压缩并保留已有记忆。

会话记录包含调用正文、回复和元数据。记录写入失败时仍返回已经获得的模型结果，并提示未保存；无需重发。取消的轮次保留部分回复和取消状态，不加入成功历史。

## 配置 Python Agent 和工具

1. 完成供应商和模型登记，选择支持原生工具调用的模型。
2. 按需在“MCP 服务”登记 Streamable HTTP 或 SSE 地址、工具白名单和访问令牌。网页搜索使用 `search` 分类，填写搜索工具名称，并在“机器人配置 → Agent 运行配置”设置 `chat_python_search_mcp_code`；`read_url` 只读取公网网页。
3. 按需创建 Skill，再创建 Agent，选择模型、工具和 Skill。Agent 可关联最多 3 个通用 MCP 服务，白名单筛选后所有服务合计最多 32 个 MCP 工具，超限时明确报错；工具轮次预算为 1–5，Skill 所需工具也必须在 Agent 中授权。
4. 在“对话测试 → Agent 对话”选择 Agent；执行步骤和结果可在“会话记录 → Agent 运行记录”查看。
5. 在“机器人配置 → Agent 运行配置”设置默认 Agent 预设代码 `chat_python_agent_code`、工具轮次、通用 MCP 服务 `chat_mcp_services`、联网搜索和群成员 @ 工具授权。Agent 管理页只维护预设，运行字段集中在机器人配置；保存沿用配置版本校验和热刷新，页签切换保留草稿。预设代码留空时仍走 Agent，使用当前 Chat 模型、提示词和预算。旧 `chat_agent_enabled` 开关已移除，数据库中的历史值不再生效，下次保存配置时清理；不需要 SQL 结构迁移。每个 MCP 服务独立保存地址与令牌，编辑令牌留空保留、明确清除才删除，保存后新请求立即使用新配置。

“Agent 管理 → 编辑 → 模型路由”分别配置聊天模型、图片理解模型和错误转发模型。纯文本走聊天模型；QQ 当前消息或引用消息携图、管理对话上传图片时走图片理解模型。图片模型留空时沿用聊天模型，所选模型仍必须在供应商页面显式启用图片理解能力。Agent 路由优先于机器人全局模型别名；不按模型名称推断能力。

错误转发默认关闭，选择备用模型后才开启。主模型配置或上游调用失败时，备用每请求最多接管一次，只继续失败的模型轮次，保留已完成的工具结果，不重复执行工作流。带图请求的备用也需支持图片理解；跨模型接管移除旧供应商私有延续，不丢弃输入图片。权限拒绝、工具执行错误、取消和请求总超时不会触发转发；备用仍失败时返回实际错误。执行记录的 warning 步骤保存原错误码及转交模型，后续请求重新从主路由开始。

既有数据库升级此功能需在新版本启动前执行一次 `sql/2026-10-13-agent-model-routing.sql`（MySQL）或 `sql/2026-10-13-agent-model-routing.sqlite.sql`（SQLite）。脚本仅新增两个可空模型引用，原聊天模型绑定和数据保留；新库直接执行对应 `agent_bot_init` 初始化脚本。普通服务启动不会自动升级数据库。升级并由运维重启一次后，在管理页修改这些模型路由对新请求即时生效。

QQ 工具包括标签记忆、永劫无间查询、群成员提及、本地工作流和 TTS；网页工具为 `web_search`、`read_url`。QQ 本地工具链需要启用 `chat_mcp_enabled`，通过会话短期令牌校验权限。

QQ 文字回复在分段和投递前统一清理空白行：连续换行以及只包含空格或制表符的空行合并为单个换行；保留正常换行和正文缩进。普通对话、工具回复、命令回复和已生成的定时任务正文均遵循此规则。

在“机器人配置 → 运行配置 → 机器人名称与触发别名”中每行填写一个名称或别名，例如“桃桃”和“小桃”。群聊消息包含任一名称即可触发，英文名称忽略大小写；名称配置保存到数据库并热更新，同时刷新 OneBot 名称识别。清空后保留 At、回复机器人消息及命令触发。旧 `.env` 的 `NICKNAME` 不再作为启动名称来源；升级时如有仅保存在环境文件的名称，请在管理页面重新保存。加载本次代码变更需要由用户重启服务，之后修改名称无需重启。

`read_url` 遇到 HTTP 拒绝或网络不可用时，向模型返回明确的失败工具结果，由模型根据已有资料说明限制，不因此重跑整段对话。公开地址校验失败、请求取消和总预算耗尽仍会终止执行。网页失败日志只记录目标主机及 HTTP 状态或异常类型，不记录链接查询参数和响应正文。

管理 Agent 可使用网页、授权的 MCP 和已启用的 `local_workflow_list`、`local_workflow_generate_image`、`tts_generate`；管理入口不接受客户端提供的 QQ 令牌、用户或群身份。

Job 默认关闭，启用后作为 QQ 专属工具执行。管理 Agent 不接受 QQ Job 工具；SolonCode 和通用 Laya 指令尚未迁移。自动发言的固定 Laya 决策已由独立 Python 服务接入。

停止请求、切换模式、离开对话页或退出登录会取消当前管理请求。已经完成的工具操作不会撤销；结果无法确认时记录为 `unknown`。残留的 `running` 记录不一定表示任务仍在执行。

## 配置图片和音频

1. 在“供应商与模型”左侧选择供应商，右侧模型列表直接开启“图片理解”，开关会立即保存；同时启用所选模型和供应商。回到 Agent 管理点击“刷新模型状态”，即可将该模型选作图片理解模型。识图能力须由供应商实际提供，开关只声明能力。图片生成接口类型（`gpt-image` 或 `legacy-b64`）及对话音频能力由媒体配置接口保存，切换图片理解不会覆盖这些配置。Ollama 支持图片理解，不提供这里的直接图片/音频生成；`legacy-b64` 输出仅支持 PNG。
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

Windows 独立桌面版可执行 `powershell -ExecutionPolicy Bypass -File tools/build_windows.ps1`。
构建后分发整个 `dist/windows/AgentBot` 目录，双击 `AgentBot.exe` 打开内嵌管理界面；
`agent-bot.exe` 用于初始化数据库和命令行运行。配置、数据库及媒体保持外置，
首次配置、WebView2 要求与升级说明见 [Windows 桌面版说明](WINDOWS_README.md)。

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

将示例 wheel 路径替换为实际文件。部署目录需提供 `.env`，完成数据库初始化后，用安装后的入口初始化配置并启动服务：

```powershell
.venv\Scripts\agent-bot.exe database init
.venv\Scripts\agent-bot.exe settings init
.venv\Scripts\agent-bot.exe settings list
.venv\Scripts\agent-bot.exe
```

已构建的界面随 wheel 提供。

安装 wheel 后可通过 `importlib.resources.files('agent_bot').joinpath('resources/agent_bot_init.sql')` 或 `resources/agent_bot_init.sqlite.sql` 读取对应初始化 SQL；`database init` 会自动使用当前后端资源。源码开发时两份脚本位于 `sql/`。
