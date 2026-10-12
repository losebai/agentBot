# agentBot 第四阶段 A：图片理解、绘图与语音

日期：2026-10-12。状态：已按确认的[实施计划](../plans/2026-10-12-agentbot-phase-four-a.md)完成 0.4.0；离线回归、浏览器验收、独立安装及 0.3.0 回退验证通过。交付与限制见[迁移记录](../../migration/phase-four-a.md)；未执行真实 SQL。

## 1. 目标与约束

在现有 Python 服务和 React 管理界面中，让 QQ 与 GUI 可以通过 Python 完成图片理解、图片生成和语音输出，减少对 Java dispatch-ai 的依赖。第三阶段的 Agent、Skill、MCP、权限交集、运行时租约和执行记录继续作为基础。

用户本次明确要求开始第四阶段，并要求代码有注释、SQL 有中文注释。本设计采用上一轮建议的第四阶段 A；通用后台任务、视频及代码执行按后续阶段推进。

- 仅修改 `D:\items\web\agentBot`；旧 `dispatch-sdk`、`bai-web` 只读，保留原有修改。
- 延续当前会话内实施方式，不创建聊天、子代理、Git 仓库、分支、提交、推送或 PR。
- 使用 Python 3.12、uv、Node.js 22；保留当前包的 Python 版本兼容声明，不升级无关依赖。
- 验证使用 Fake 数据库、HTTP Mock 和回环协议服务，不连接真实 QQ、付费模型或生产服务，不执行真实数据库迁移。
- 新增或修改的 Python 模块、类、函数有中文文档字符串；TypeScript 组件、函数与复杂逻辑有中文注释。注释解释职责、约束与原因，不逐行复述代码。
- 新增或修改 SQL 的脚本说明、表 `COMMENT`、字段 `COMMENT` 使用中文；索引和外键的目的用中文行注释解释。

## 2. 核对到的现有实现

| 位置 | 当前行为及迁移影响 |
| --- | --- |
| `src/agent_bot/ai/routing.py` | 图片、视频及媒体工具使整个请求走 Java；`generate_media` 直接委托 Java，需要同时改路由和模型预检 |
| `src/agent_bot/agent/contracts.py` | `AgentRequest`、`TurnMessage` 只承载文本，不能把 Base64 图片塞进 prompt 冒充图片理解 |
| `src/agent_bot/ai/providers/agent_*.py` | 已有三种原生工具调用协议，可在消息适配边界添加图片内容 |
| `src/dispatch_chat/multimodal.py` | 已有 QQ 图片域名限制、内容校验、Pillow 解码和模型图片压缩；复用并补齐下载连接边界 |
| `src/dispatch_chat/services/chat/media.py` | 直接媒体请求：图片使用 prompt，音频使用对话 messages，不能把对话音频替换成朗读用户输入 |
| `src/dispatch_chat/services/chat_tools.py` | 已有工作流列表、绘图、任务查询/取消、TTS 及请求级副作用收集器 |
| `src/dispatch_chat/services/workflow_execution.py` | 已有工作流快照、图生图引用、调用额度与生成结果校验 |
| `src/dispatch_chat/services/image_tasks.py` | 已有 QQ 绘图任务状态与所属用户检查，本阶段复用，不重新建设任务系统 |
| `src/dispatch_chat/integrations/comfy.py`、`gpt_sovits.py` | 已有 Python 客户端、超时和资源生命周期，应接入现有运行时而非再创建一套连接 |
| `src/agent_bot/admin/agent_chat.py` | 管理入口只有网页/MCP 授权，不允许伪造 QQ 用户或工具令牌 |
| `web/src/components/AgentChat.tsx` | 已有流式执行、停止和运行结果；缺少图片上传、附件及音频预览 |
| `sql/agent_bot_ai_init.sql`、`agent_bot_agent_init.sql` | agentBot 自有初始化脚本目前缺少中文表、字段注释，需随本阶段补齐 |

只读核对旧 Java `ChatMediaService`、`OpenAiImageGenerationGateway`、`OpenAiAudioGenerationGateway` 后确认：图片是单张 Base64 输出，音频是 Chat Completions 的 text/audio 对话输出；GPT-SoVITS 是另一条工具语音链路。

本设计区分三件事：模型接口支持某种协议、管理员声明模型具备某项能力、真实上游接受该请求。前两项能本地预检；离线验收不声称验证了真实模型质量或服务可用性。

## 3. 方案选择与阶段边界

推荐方案 A：在当前进程新增 `agent_bot.media`，共享模型目录、预算与媒体校验；QQ 继续通过网关调用，GUI 通过已认证管理 API 调用。无需新增部署服务，能够串通授权和取消，成本是增加明确的媒体类型契约和文件生命周期。

方案 B：拆出独立媒体服务。便于将来单独扩容，但当前会增加部署、认证、文件传递和取消协议，不采用。

方案 C：只把现有 ComfyUI/TTS 工具加入 Python 白名单。改动小，但不能解决图片输入、QQ 直接媒体请求或 GUI 预览，不能作为本阶段完成标准。

采用 A，按依赖顺序交付图片输入、生成及工具接入、GUI 和迁移验证。它们共用媒体契约与文件服务，合为一个阶段设计。

本阶段包括：

1. Responses、Chat Completions、Ollama 的图片输入；QQ 和 GUI 使用同一个 Python Agent 内核。
2. QQ 直接图片生成、对话音频生成的 Python 实现，保留已有返回契约。
3. Python Agent 调用已有 ComfyUI 和 GPT-SoVITS；QQ 沿用现有任务、投递与权限逻辑。
4. GUI 上传图片、预览结果、播放语音、停止执行及查看记录附件。
5. 显式后端切换、能力预检、注释、SQL、测试和独立安装包。

后续阶段处理视频理解/生成、语音输入/识别、通用 job 调度与恢复、完整工作流编辑器、Laya、soloncode、动态 Java/Lua 执行。本阶段只接通已经存在的 QQ 图片任务查询/取消工具，不以此宣称后台任务全面迁移完成。

## 4. 能力声明与路由

新增持久化运行配置 `dispatch_chat_media_backend=java|python`，默认 `java`。普通文本仍由 `dispatch_chat_plain_ai_backend` 控制，工具执行仍由 `dispatch_chat_agent_backend` 控制。

| 请求 | 路由规则 |
| --- | --- |
| 纯文本、没有工具 | 保持普通文本后端规则 |
| 图片输入、没有工具 | 由媒体后端决定；Python 使用禁用工具的 Agent 执行，保持单一图片消息协议实现 |
| 图片输入并使用 Agent/工具，或授权媒体工具 | 媒体后端与 Agent 后端均为 Python 时整次请求走 Python，否则整次请求走 Java |
| QQ 直接图片或对话音频生成 | 由媒体后端决定 |
| 视频、job、soloncode、Laya 或其他尚未迁移的能力 | 整次请求明确走 Java；GUI 的 Python 请求在产生副作用前拒绝 |
| GUI 显式执行 Python 媒体操作 | 要求媒体后端为 Python；Agent 对话还要求 Agent 后端为 Python |

根据请求的有效授权能力集做决定，路由与模型预检共用一个结果。Python 被选中后，模型不支持、配置缺失或执行失败均返回明确错误，不按失败自动改走 Java，不拆成前半段 Python、后半段 Java。

给模型增加独立的能力配置：`vision_enabled` 默认 false、`image_api_profile` 默认 none（可选 gpt-image、legacy-b64）、`audio_chat_enabled` 默认 false。不根据模型名称猜能力，不在启动时调用付费接口探测。

模型 GUI 与 API 在同一个模型 revision 下编辑能力；执行前与供应商、Agent 配置一起取得快照。vision 可用于三种已有协议；图片生成及对话音频仅允许 OpenAI 兼容供应商，Ollama 对这两项在调用前拒绝。gpt-image 与 legacy-b64 的区别见第 6 节。

## 5. 图片输入与媒体文件

引入类型化图片内容，文字仍是文字。Agent 请求的图片只关联本次最后一条 user 消息，工具轮次内保留；本阶段下一条用户消息不会自动重传上轮图片，GUI 明确提示需要重新选择附件。只发图时使用现有默认识图提示词。

- QQ 接收网关提取的可信 QQ 图片引用；保持当前 HTTPS CDN 限制，逐次跳转校验，并复用第三阶段已验证地址连接的网络边界，防止域名解析与实际连接不一致。
- GUI 使用 `POST /api/v1/media/uploads?session_id=...` 上传单张原始图片字节，不接受 URL、本地路径或模型生成的文件位置。流式读取请求体，按实际累计字节限流；客户端 Content-Length 仅作提前拒绝依据。
- 输入支持 PNG/JPEG/WebP/非动画 GIF。使用真实解码校验 MIME、尺寸和像素，拒绝动画，去除元数据，统一生成模型可用的内容。
- 单图原始大小最多 10 MiB，单次请求全部原始图片最多 20 MiB；数量沿用运行配置，默认 4、硬上限 8；模型单图最多 2 MiB、16,777,216 像素。压缩后仍超限直接报错。
- 原始字节、Base64、QQ 签名 URL 不写入步骤摘要、应用日志或 SSE。记录只保存受限媒体元数据与附件 ID；临时输入文件按保留期删除。

新增 `MediaStore`，仅管理 `data/media` 下由服务端随机 ID 命名的文件，拒绝客户端路径和符号链接/目录联接逃逸。媒体根目录使用部署配置，不允许从工具参数或上传参数覆盖。

媒体条目包含 ID、来源入口、session、可空 execution_id、input/output 用途、类型、字节数、摘要、状态和时间。管理员上传绑定 `admin_` 会话；QQ 输入不会通过浏览器上传接口产生。执行引用时再次验证入口、会话、用途、到期时间和完整性。

文件写入临时文件后原子替换，再发布 ready 状态；失败时不返回可预览结果。启动时对账未完成写入及超过一小时的临时孤儿文件。默认保留 24 小时，固定总配额 1 GiB；已过期文件每分钟清理，未过期内容不因容量不足被悄悄删除，空间不足返回 `MEDIA_STORAGE_FULL`。运行中持有的文件在释放使用租约后清理。读取过期条目返回 410，未知或越权引用统一返回 404。

`GET /api/v1/media/{id}` 获取受限元数据，`GET /api/v1/media/{id}/content` 获取实际内容，两者都沿用管理 Bearer 认证；文件不得放进静态资源目录，不提供含令牌的 URL。浏览器认证 fetch 后使用 Blob URL 展示，切页、退出和替换附件时撤销 URL。响应包含正确 MIME、`nosniff` 和禁止缓存设置。

GUI 记录只显示 admin 入口附件；QQ 媒体仍通过既有 QQ 投递链路发送。媒体条目到期后运行记录保留附件已过期说明，不自动重新生成。

## 6. 图片协议与直接媒体生成

扩展 `AgentRequest`、`TurnMessage` 和各 Agent 供应商适配器，使用受限图片对象，二进制字段不参与普通序列化或 repr。模型上下文仍按原文本预算校验，图片使用独立字节预算，不能把 Base64 算进 120,000 字符文本预算。

- Responses：用户消息使用 input_text 与 input_image，图片使用校验后的 data URL。[官方图片输入说明](https://developers.openai.com/api/docs/guides/images-vision)
- Chat Completions：用户内容使用 text 与 image_url 项，不改变已有工具 call ID 往返。[官方图片输入说明](https://developers.openai.com/api/docs/guides/images-vision)
- Ollama：用户消息使用 images 数组承载 Base64。[Ollama Vision](https://docs.ollama.com/capabilities/vision)

图片理解支持同步与已有真实文本流，继续遵循 start/model_start/tool/done/error 及终态检查。模型不支持图片时，不丢掉图片继续回答。

新增 `MediaGenerationService`，复用模型目录、凭据引用与 HTTPX 传输。QQ `generate_media` 保持 `ChatMediaResponse` 单个 Base64 媒体项契约；GUI 使用 `POST /api/v1/media/generations`，返回执行标识、文字和附件元数据，不把 Base64 放在 JSON 结果或 SSE 中。两个入口共用生成用例与格式校验。

图片生成调用 `/images/generations`，固定 n=1，要求返回唯一且合法的 `b64_json`。gpt-image 配置发送 output_format（png/jpeg/webp），不发送旧 response_format；legacy-b64 发送 response_format=b64_json，只接受 PNG，不发送 output_format。模型名沿用管理员配置，无硬编码默认付费模型。返回 URL 而非 Base64、数量不符或格式不符均报错，不自动下载未知地址。[官方图片生成说明](https://developers.openai.com/api/docs/guides/image-generation)

对话音频沿用旧语义：向 `/chat/completions` 发送已有 messages、modalities 和 voice/format，解析唯一助手音频及配套文字或 transcript。不得改成朗读用户问题。输出格式保留现有请求类型，按格式校验实际内容；PCM 是无容器数据，只提供下载，GUI 说明无法直接播放。不在此阶段新增独立 `/audio/speech` 产品入口。[官方音频说明](https://developers.openai.com/api/docs/guides/audio)

直接生成最多 120 秒，继承调用者更短截止时间；每项解码后最多 10 MiB，HTTP 响应读取上限为 Base64 膨胀后的长度加 64 KiB 元数据，并对解压后数据计数。最多 2 个直接媒体生成同时执行，额外请求返回 429；不进行自动重试。保存失败与上游生成失败使用不同错误码，避免用户误以为上游没有执行。

## 7. ComfyUI、TTS 与授权

从未迁移工具集合中移出 `local_workflow_list`、`local_workflow_generate_image`、`local_workflow_tasks`、`local_workflow_cancel`、`tts_generate`，并在媒体后端路由规则中显式识别这些能力。

QQ 使用现有 ChatToolBridge、ToolSessionStore、ChatToolContext、收集器及副作用记录。每次调用重验令牌、用户/群/会话、工具范围和截止时间，不复制发送消息的业务逻辑。绘图继续保持“开画答复送达后才提交任务”的约束；停止 Agent 不等于撤销已经入队的图片任务，取消任务仍通过所属用户的取消工具完成。后台执行保留现有最多 500 秒的工作流预算，不提升 Agent 的 300 秒上限。

GUI 增加独立的管理媒体工具适配器，复用相同工作流与 TTS 业务服务，但不构造 QQ 身份或 Chat 令牌。管理员只在预设允许且服务端已启用时获得工作流列表、生成和 TTS 权限。QQ 记忆、@、群任务查询/取消等工具仍不向管理入口开放。

工具稳定名称相同，但 GUI 与 QQ 分别选择一个入口适配器；工具目录不能出现重名。GUI 工具结果只把尺寸、类型、附件 ID、状态等信息交回模型，实际字节通过媒体文件服务交付。

工作流输入只能来自当前请求已验证的图片。GUI 通过校验字节调用 Comfy 上传适配，不把 `data/media` 本地路径交给模型或远端服务。需要一张原图的工作流遇到零张或多张输入时明确拒绝；不得悄悄选择第一张。继续使用工作流快照摘要和请求独立的上传目录。

本地工作流 schema 含 pattern，而第三阶段的远端 schema 子集禁止正则。仅对已知本地工具投影模型 schema 时移除 pattern，保留类型和长度约束；真正执行前使用原 Pydantic 参数类型复验。禁止全局放开远端 MCP 正则或引用规则。

GUI 的绘图在当前 Agent 请求内执行，继承剩余预算，不额外创建后台任务。每个请求最多生成 1 张图片、1 段语音；已有 QQ 工具额度不放宽。GPT-SoVITS 保持固定配置音色和 WAV，清洗后的朗读文本及实际输出校验继续使用现有实现。

## 8. 取消、结果与运行记录

图片下载、解码、工具、生成、文件提交和记录均共享入口预算，不为后续步骤重新计时。GUI 停止、断开和切页会取消本次调用并关闭上游连接。Comfy 仅在已有可验证任务归属下尝试取消指定 prompt，不使用可能停止他人任务的全局中断。

请求已提交但无法确认上游停止时，步骤标记 unknown，运行标记 cancelled；不宣称回滚或安全重试。请求结束后继续持有运行时租约至所有本次清理完成，防止热刷新先关闭仍在使用的客户端或数据库池。

扩展 Agent 事件增加 artifact 事件，只携带已经持久保存的元数据，不携带文件字节、绝对路径或上游 URL。同步结果增加 attachments；流式结束仍需有效 done 及完整 EOF 才提交成功聊天历史。

已完成的工具附件独立于最终文字状态：后续模型失败或取消时，GUI 在执行详情中显示“工具已生成，回复未完成”，不能冒充成功对话，也不能隐藏已经发生的媒体副作用。记录保存失败时保留可用结果并显示明确警告，不重新调用模型。

直接媒体生成采用独立媒体运行记录，包含入口、会话、请求、配置模型及实际后端、类型、状态、耗时、错误码和受限文字。Agent 附件关联既有 execution_id。未确认结束的历史记录不自动重跑。

## 9. SQL 与兼容升级

新增 `sql/agent_bot_media_init.sql`，手动应用在第二、三阶段 SQL 之后，只创建以下表：

| 表 | 字段与作用 |
| --- | --- |
| `agent_bot_ai_model_media` | model_id 主键及外键，vision_enabled、image_api_profile、audio_chat_enabled；与模型 revision 在同一事务中更新 |
| `agent_bot_media_run` | execution_id 主键、entry、session_id、request_id、model、backend、output_type、status、duration_ms、prompt、content、error_code、created_at |
| `agent_bot_media_asset` | id 主键、entry、session_id、可空 execution_id、execution_kind（agent/media）、purpose、mime_type、size_bytes、sha256、受限 storage_key、status、created_at、expires_at |

媒体模型配置外键删除随模型记录级联；附件的两类执行关联通过类型和服务层校验，不能伪造跨入口引用。媒体条目的到期时间和执行关联建立索引；运行记录按时间和会话建立分页索引。不保存实际凭据或文件 Base64 到 MySQL。

新表全部带中文表和字段 COMMENT。同时给 agentBot 自有的两份既有初始化 SQL 补中文 COMMENT。另提供 `sql/2026-10-12-agentbot-schema-comments.sql`，为已经存在的这些自有表补同样注释，逐字段保留类型、默认值、可空性、字符排序及约束。脚本头中文说明 DDL 可能持有元数据锁，需由部署者在维护窗口手动执行。保留历史原始迁入 SQL，不改旧项目 SQL。

媒体表未初始化时，默认 Java 和原 Python 纯文本能力仍可用；媒体操作返回明确的未初始化状态。读取模型时把缺失媒体表视为能力未配置并附可见状态，不在正常请求中自动建表。更改媒体能力必须要求媒体表已初始化。

回退时先将媒体后端切回 Java，再按既有 CAS 快照流程恢复旧版本配置并删除该快照中的新增配置键；新增媒体表和文件不自动删除，旧版本忽略它们。需用保留的 0.3.0 wheel 验证回退配置可读取。

## 10. GUI、模块职责与验收

沿用现有视觉结构，不改整站风格：

- 模型页：媒体能力开关、图片 API 类型及不支持组合的明确说明。
- Agent 对话：选择图片、缩略图和移除、上传失败重试、尺寸/数量提示、结果图片、音频播放/下载、停止与异常状态。
- 对话页增加媒体生成模式：选择启用的图片/音频模型，显示真实后台状态和结果；音频输入说明是要求模型回答的提示。
- 运行详情：输入附件、已生成附件、过期标记和取消后副作用说明；不加入自动重跑按钮。
- 概览：显示媒体后端、相关本地初始化状态及仍依赖 Java 的能力。
- 页面离开和退出登录时中止上传/执行、释放 Blob URL。390px 宽度保持可操作，媒体不能撑破页面。

新增模块按职责拆分为 `agent_bot.media` 下的 contracts、inputs、store、records、service、providers，以及 Agent 管理媒体工具适配、管理路由和前端附件组件。现有 `RoutingAiClient` 只负责路由；HTTP/文件处理不放进 AgentEngine；不把整套业务堆到管理路由中。

实施计划应逐项覆盖以下验收：

1. 三种图片输入协议与工具轮次的请求内容通过 Mock 检查，工具调用后仍保留本次图片；无图请求回归通过。
2. Java 客户端设为任何调用都报错，Python 的图片理解、直接图片/对话音频、Comfy/TTS 流程仍能完成；显式 Java 模式调用原入口。
3. QQ 开画答复未送达时不得提交图片任务；重复调用、副作用缓存、图片任务归属和取消语义保持原样。
4. 超限上传、截断图片、伪 MIME、动画、解压/像素炸弹、越权附件、跨会话串图、过期、磁盘满、原子提交失败均给出确定状态。
5. 上游响应非 Base64、格式不符、零/多媒体项、慢流、半途断开和超时均不展示成功，不产生自动重试。
6. 真正 ASGI 断开和运行时热刷新验证资源释放；中断 Comfy 已提交任务时不能假装取消已完成，不能停止其他任务。
7. SQL 静态检查验证每张自有表、字段的中文 COMMENT，增量注释不改变结构语义；无 DROP/TRUNCATE/无条件删除数据。真实 MySQL DDL 未执行须在交付中明确说明。
8. Python 离线回归及分支覆盖率至少 80%，Ruff、前端类型检查、前端测试、构建、包内容和独立安装检查通过。仍排除会清空真实配置的数据库测试。
9. GUI 使用 Fake 业务及本地协议服务完成桌面、390px 上传/预览/播放/停止验收；截图验证后写入迁移文档。
10. 检查所有新增或修改函数/组件的中文注释与实现一致；更新 README、配置示例、源码适配清单和回退说明。只有验证通过后才发布 0.4.0 包，不提前覆盖旧包。

设计自审结果：已区分直接对话音频与 TTS、GUI 与 QQ 权限、现有图片队列与后续通用任务、模型能力声明与真实调用、取消与回滚；代码实施和 SQL 执行均尚未开始。
