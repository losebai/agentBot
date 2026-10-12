# agentBot

Python 源码平铺位于 `src/`，打包和可编辑安装统一映射为 `agent_bot`；原 `dispatch_chat` 的配置、业务服务、集成、插件和资源已合并。代码导入使用 `agent_bot.*`，NoneBot 插件路径为 `agent_bot.plugin`。数据库表名、`dispatch_chat_*` 配置字段和 `DISPATCH_CHAT_*` 环境变量继续兼容现有实例。历史迁移文档中的旧路径仅用于记录来源。

Python Agent 机器人服务与浏览器管理界面。0.4.0 增加图片理解、图片/对话音频生成、ComfyUI 与 GPT-SoVITS 工具、认证附件及媒体运行记录。普通对话、Agent 和媒体可分别选择 Python 或 Java；视频、通用任务、Laya 与代码执行继续使用 Java。

## 环境与安装

使用 Python 3.12、uv、Node.js 22。

```powershell
uv sync --extra dev --python 3.12
uv run agent-bot --help
Set-Location web
npm ci
npm run build
Set-Location ..
```

## 初始化与运行

1. 准备独立 MySQL 数据库 `agent_bot`、外部 Redis。QQ 接入需要 NapCat；使用 Java 后端或未迁移能力时需要 Java `dispatch-ai`。Python 普通对话、Agent 和媒体可直接调用已配置供应商；本地工具分别连接 ComfyUI、GPT-SoVITS。
2. 新库只需执行统一脚本 `sql/agent_bot_init.sql`，包含全部 25 张业务表和 Java 模型兼容目录。例如，在项目目录运行 `mysql -h 127.0.0.1 -u root -p agent_bot --execute="source sql/agent_bot_init.sql"`。脚本只创建不存在的表，不插入配置或模型，也不升级既有表；旧实例按 [SQL 使用说明](sql/README.md) 选择对应增量脚本。
3. 在新库的 `ai_model` 中显式登记 Java AI 已启用的模型标识。只写入 `model_id`、`type='chat'`、`enabled=1`，不要复制供应商密钥。标识必须与 Chat 配置一致；Java AI 仍做最终校验，停用模型时也要更新此兼容目录。
4. 创建本项目的 `.env`，填写自己的引导参数、运行配置和新 Token。以下命令只允许在文件不存在时复制示例，不会覆盖已有配置：

```powershell
if (Test-Path .env) { throw ".env 已存在，请直接编辑已有文件" }
Copy-Item .env.example .env
```

示例 Token 故意无效。使用 `uv run python -c "import secrets; print(secrets.token_urlsafe(48))"` 分别生成管理员凭据、OneBot Token 和会话密钥；它们必须不同。数据库连接参数以 `DISPATCH_CHAT_DB_*` 开头，Redis 参数以 `DISPATCH_CHAT_REDIS_*` 开头；新实例使用独立 Redis key 前缀。`DISPATCH_CHAT_AI_BASE_URL` 指向 Java AI 的 `/dispatch-ai` 服务地址。

管理员凭据使用 `AGENT_BOT_ADMIN_TOKEN`，只从本项目 `.env` 或进程环境读取，不存入机器人配置表。环境变量优先于 `.env`。

首次初始化空配置表：

```powershell
uv run agent-bot settings import-env
uv run agent-bot settings list
```

`import-env` 只允许导入空配置表，不会覆盖现有快照。成功后，MySQL 是运行配置的唯一权威来源，`.env` 保留数据库、Redis 引导参数、管理员凭据，以及所需的供应商/MCP 凭据引用值。已有实例不要重新生成其会话密钥，否则历史会话与记忆身份会变化。

启动服务：

```powershell
uv run agent-bot
```

`agent-bot` 或 `python -m agent_bot` 启动机器人和浏览器管理服务。旧 `dispatch-chat` 命令继续由 `agent_bot.bot_cli` 提供，保持只启动机器人的行为；离线 `settings` 和 `workflows` 子命令也保留。

默认地址为 `http://127.0.0.1:8080/`；以数据库中实际的 `host`、`port` 为准。输入管理员凭据进入工作台。NapCat 反向 WebSocket 配置为 `ws://127.0.0.1:8080/onebot/v11/ws`，其 Access Token 与 OneBot Token 一致。不要同时启动两个连接同一机器人、队列前缀的服务实例。

图形界面包含运行概览、机器人状态、完整配置编辑、普通文本对话测试。保存会显示数据库版本、应用版本和需重启字段；热刷新只应用可在线修改的字段。关闭浏览器不会停止后台服务。

新库包含敏感运行配置，使用专用数据库账户。现有 Chat 启动会初始化记忆/标签表，开启工作流后还会执行表结构兼容检查和升级，应在该独立数据库范围内授予所需的建表/升级权限。

使用 Java Agent 后端的机器人工具时，还需让 Java AI 的 `dispatch.ai.chat-mcp.enabled` 为真，并将 `dispatch.ai.chat-mcp.url` 指向新实例的 `/mcp` 地址；Chat 的 `DISPATCH_CHAT_MCP_ALLOWED_HOSTS` 要允许 Java 实际使用的主机名。该链路使用 Chat 签发的短期工具令牌，不使用管理员凭据。普通文本对话测试不依赖 MCP。

## 开发与验证

```powershell
uv run ruff check .
uv run pytest -m "not e2e" -k "not database_onebot_token_reaches_adapter_without_log_disclosure" --allow-hosts=127.0.0.1,::1
npm --prefix web run typecheck
npm --prefix web test -- --run
npm --prefix web run build
uv build
uv run python tools/check_package.py dist/agent_bot-0.4.0-py3-none-any.whl
```

Windows 事件循环需要本机 socketpair，离线测试允许 loopback，但阻止机器外连接。上述命令排除了会清空真实 MySQL 配置表的环境联调测试；其余机器人行为使用 Fake/Mock 验证，覆盖率门槛保留 80%。本项目验证时没有登录 QQ、调用真实模型或修改生产数据。

前端开发使用 `npm --prefix web run dev`，默认代理 `127.0.0.1:8080`；修改服务端口时同时更新 `web/vite.config.ts`。生产构建输出进入 Python 包的静态目录，安装 wheel 后无需旧项目或 Node.js 即可提供界面。安装源码后必须先构建前端。

离线命令统一使用 `uv run agent-bot settings import-env`、`uv run agent-bot settings list`；也可使用 `uv run python -m agent_bot` 加相同子命令。旧 `dispatch-chat` 命令仍兼容；`python -m dispatch_chat` 需改为新包入口。

## 自动发言兼容行为

`DISPATCH_CHAT_AUTO_SPEAK_TRIGGER_MODE=interval` 表示定时触发，`message` 表示群消息触发。群消息触发要求启用 `DISPATCH_CHAT_MCP_ENABLED`、`DISPATCH_CHAT_LAYA_ENABLED` 和 `DISPATCH_CHAT_GROUP_RECENT_CONTEXT_ENABLED`；私聊不再使用群消息触发规则。保存后通过数据库 revision 热刷新，部分启动字段仍需要重启。

## 迁移范围

Chat 源码、运行资源、业务测试与 SQL 单独迁入。旧仓库 GitLab CI、Helm 部署配置测试不迁入，原因记录在来源清单中。源码来源摘要见 [来源清单](docs/migration/source-manifest.json)，阶段状态与验证结果见 [第一阶段迁移记录](docs/migration/phase-one.md)。

新安装统一使用 `sql/agent_bot_init.sql`；合并前的 25 份脚本原样归档至 `sql/legacy/`。其中 `dispatch_chat_setting.sql` 含旧安装的删除表操作，其他迁移也可能覆盖配置或处理历史数据，不批量执行归档目录。执行顺序和旧实例升级范围见 [SQL 使用说明](sql/README.md)。

第二阶段迁移与回退见 [第二阶段记录](docs/migration/phase-two.md)，本次 Agent/Skill/MCP 迁移及回退见 [第三阶段记录](docs/migration/phase-three.md)。第四阶段 A 的媒体功能及 0.3.0 回退见 [第四阶段 A 记录](docs/migration/phase-four-a.md)。通用任务、视频、Laya 和代码执行留在后续阶段。没有自动迁移生产数据。旧项目完整保留，切换实例前停止旧机器人消费者，再调整 NapCat 地址；回退时停止新实例并恢复旧连接，避免重复发消息。

## 启用 Python 普通 AI

1. 新库已执行 `sql/agent_bot_init.sql` 后无需再建 AI 表；已有实例只缺 Python AI 表时显式执行 `sql/legacy/agent_bot_ai_init.sql`。安装 wheel 后可用 `importlib.resources.files('agent_bot').joinpath('resources/agent_bot_init.sql')` 读取统一脚本，旧分阶段资源仍保留供指定版本升级。SQL 不会在服务启动时自动执行。
2. 在本项目 `.env` 或进程环境添加供应商凭据，例如 `AGENT_BOT_PROVIDER_QWEN_KEY`。环境优先，不做变量插值；供应商只保存引用名。本机无凭据服务可留空。
3. 在供应商管理页登记协议与 API 根地址，然后启用供应商。Responses 地址示例 `https://api.openai.com/v1`；兼容 Chat Completions 使用服务的 `/v1` 根地址；Ollama 使用 `http://127.0.0.1:11434`。不要填写完整对话端点。
4. 在模型管理页登记本地别名、精确上游 ID 和供应商，启用模型。例如别名 `local-qwen` 对应上游 `Qwen/Qwen3.5-9B`，不会再拼前缀。
5. 在机器人配置中将 `dispatch_chat_plain_ai_backend` 改为 `python`，将 `dispatch_chat_model` 改为模型别名。查看保存/应用版本后，在对话页刷新模型状态，再测试同步或流式回复。临时模型选择只影响本次管理测试。

Python 失败不会切 Java，不自动重发付费请求。无工具文本由普通后端决定；Agent、联网和已迁移工具由 Agent 后端决定；纯图片理解由媒体开关决定；混合图片/媒体工具与 Agent 的请求需要两个开关均为 Python；未迁移能力整次请求走 Java。两边分别登记可用模型。记忆压缩固定设置 `enableThinking=false`，目前 Ollama 可以表达，Responses/兼容协议会明确拒绝并保留原有记忆。兼容 Chat Completions 的输出上限使用 `max_completion_tokens`，要求服务支持该参数；不会报错后换参数重试。

会话记录保存 Python 调用的当前输入、回复和元数据，包含聊天正文；部署方应安排保留与清理策略。记录失败仍返回付费结果，并提示未保存，无需重发。取消轮次保留部分回复和取消状态，不加入成功历史。页面关闭/退出会取消正在执行的管理对话。

## 启用 Python Agent

1. 新库的统一初始化已创建 Agent/Skill/MCP/运行记录表；已有 AI 表的旧实例只缺 Agent 表时执行 `sql/legacy/agent_bot_agent_init.sql`。完成 Python 供应商和模型登记后继续配置；兼容 wheel 资源路径为 `agent_bot/resources/agent_bot_agent_init.sql`。
2. 在 MCP 服务页登记 Streamable HTTP 或 SSE 地址、工具白名单及是否必需。凭据只填写 `AGENT_BOT_MCP_*` 变量名，实际值保留在本项目环境或 `.env`。网页搜索需单独登记 `search` 分类、搜索工具名称，并设置 `dispatch_chat_python_search_mcp_code`；`read_url` 只读取公网网页。
3. 创建 Skill（可选），再创建 Agent，选择已启用模型、允许工具、Skill、最多 3 个通用 MCP 和 1–5 轮工具预算。关联 Skill 的工具依赖必须已经授权。保存后启用 Agent。
4. 在机器人配置将 `dispatch_chat_agent_backend` 设置为 `python`，等保存/应用版本一致。在“对话测试 → Agent 对话”选择 Agent，测试同步或流式执行；“会话记录 → Agent 运行记录”查看结果与步骤。
5. QQ 使用预设时设置 `dispatch_chat_python_agent_code`；留空则使用当前 Chat 模型、提示词和预算。QQ 通用 MCP 还需在 `dispatch_chat_mcp_services` 中授权对应 code。Python 本地 Chat 工具复用现有短期令牌和业务注册表，需开启 `dispatch_chat_mcp_enabled`；不需要经 Java HTTP 转发。

QQ 已迁移工具包括 `memory`、`naraka_query`、`naraka_search`、`mention_member`、本地工作流与 TTS；网页工具为 `web_search`、`read_url`。管理对话可使用网页、预设 MCP 及已启用的 `local_workflow_list`、`local_workflow_generate_image`、`tts_generate`，不接受 QQ 令牌、用户或群身份。模型实际支持原生工具调用时才能使用 Agent。

如果本次 QQ 授权集合包含 `job`、`soloncode`，或启用了 Laya，整个请求仍走 Java。要使用 Python Agent，请关闭不需要的未迁移工具（特别检查默认启用的 job、soloncode、Laya）。本地工作流/TTS 的混合 Agent 请求还要求媒体后端为 Python。GUI 中含未迁移能力的 Python Agent 会明确拒绝执行。

停止、切换模式、离开对话页或退出登录会取消当前管理请求。工具已经发生的副作用不会撤销或重试；步骤结果不明时记录为 `unknown`。数据库不可用或截止时间耗尽时收尾记录可能不完整，不能仅凭残留的 `running` 状态判断请求仍在执行。

回退可先将 Agent 后端改回 `java` 并恢复 Java 可用的模型/code。退回 0.2.0 二进制前停止实例，备份配置，通过版本校验恢复快照并移除 `dispatch_chat_agent_backend`、`dispatch_chat_python_agent_code`、`dispatch_chat_python_search_mcp_code` 三个新增字段；保留原轮次、MCP code 和凭据。新增表可保留，完整演练记录见第三阶段文档。

## 启用 Python 媒体（0.4.0）

1. 新库只需 `sql/agent_bot_init.sql`；已有 AI/Agent 表的旧实例只缺媒体表时显式执行 `sql/legacy/agent_bot_media_init.sql`。该升级脚本创建模型能力、媒体执行和文件元数据三张表。统一初始化、三份兼容分阶段脚本及注释增量脚本均打包到 `agent_bot/resources/`，服务不会自动执行它们。
2. 从 0.3.0 升级的旧十表可额外应用 `sql/2026-10-12-agentbot-schema-comments.sql`，仅补中文注释，保留字段类型、默认值和约束。首次建库使用带中文注释的初始化脚本，不需此增量脚本。执行前按部署流程备份、审查元数据锁影响。
3. 在“模型管理”显式启用图片理解、图片生成接口（`gpt-image` 或 `legacy-b64`）、对话音频。能力随模型版本一起保存，冲突会保留草稿。Ollama 可识图，不支持这里的直接图片/音频输出；`legacy-b64` 输出仅 PNG。
4. 通过“机器人配置”将 `dispatch_chat_media_backend` 改为 `python`。纯识图不要求 Agent 后端切换；Agent 加图片或媒体工具需要 `dispatch_chat_agent_backend=python` 和媒体开关同时启用。普通对话的开关独立。默认仍为 Java，Python 调用失败不会自动回退或重试。
5. 服务启动读取 `AGENT_BOT_MEDIA_DIRECTORY`，默认 `data/media`；环境变量优先于本项目 `.env`。它不进入 MySQL 热刷新配置。目录需本实例独占、可读写，不能是符号链接或重解析点。应用脚本或修复目录后重启，再查看概览中的媒体就绪状态；缺媒体表不会阻断普通文本。
6. 在“对话测试 → 媒体生成”选择模型，生成图片或对话音频；在“Agent 对话”添加图片，也可只传图片。ComfyUI/TTS 工具还需原有服务配置与 Agent 工具白名单。文件预览和下载都需要当前管理员凭据；PCM 只提供下载，其他音频通过浏览器支持的格式播放。

图片单张原始数据最多 10 MiB、一次合计 20 MiB；数量默认 4 张且不超过 8 张；不支持动画。模型使用去元数据且不超过 2 MiB 的副本。上传并发最多 2 个、30 秒；直接生成并发最多 2 个、120 秒；管理 Agent 每轮执行最多产出一张图片及一段语音。服务器配置可进一步收紧预算。

文件默认保留 24 小时，共用 1 GiB 配额，每分钟清理；到期后返回 410。执行和下载持有的文件先释放租约再删除。上传 ID 只绑定一次执行；再次识图要重新上传原文件，历史只回传文字。工具先生成而最终回复失败/取消时，界面保留产物并提示“工具已生成，回复未完成”，详情页仍可下载未过期的附件。

退回 0.3.0 前停止 0.4.0，备份运行配置，恢复旧版本可用的模型与路由设置，并从交接快照移除 `dispatch_chat_media_backend`，保留其他字段、凭据、工具和预算。安装保留的 0.3.0 wheel 后按原版本流程启动；新增媒体表和目录保留，不自动删除。详细 SQL 顺序、验证结果和隔离回退演练见 [第四阶段 A 记录](docs/migration/phase-four-a.md)。
