# 第三阶段迁移记录：0.3.0

日期：2026-10-12。范围为独立 agentBot 项目中的 Python Agent、Skill、MCP、网页工具与管理界面；旧 Java 项目继续保留媒体、后台任务、Laya、soloncode、工作流和 TTS。

## 已交付行为

- 同一个 AgentService/AgentEngine 为 QQ、管理同步请求和 SSE 提供执行能力。工具调用结果按原生协议回传模型，支持 Responses、兼容 Chat Completions 和 Ollama；不会把工具描述当成普通文本伪装成 Agent。
- Agent、Skill、MCP 目录具有启停、分页、详情、关联、revision CAS 和引用保护。Agent 默认使用预设模型；QQ 不会先用 Java 模型目录校验 Python alias。Skill 提示与示例按名称/ID 稳定排序注入，依赖工具不增加权限。
- 通用 MCP 支持 Streamable HTTP/SSE，使用当前锁定 SDK；搜索服务独立于通用 MCP。客户端限制来源、初始化、发现页数、工具数量、schema、参数及结果大小。
- 本地 `memory`、永劫查询和 `mention_member` 复用原 Chat 工具注册表、短期令牌及身份范围。记忆首屏标签保持完整，@ 工具只登记最终回复目标。
- 网页读取验证每次跳转和所有 DNS 结果，并连接已验证的公网数字地址，保留原 Host/TLS 名称；限制压缩前后大小、内容类型和输出分页。
- GUI 增加 Agent/Skill/MCP 三页、普通/Agent 对话模式、真实增量输出、当前轮草稿、工具步骤、停止及运行记录详情。模型先输出工具前草稿时，该草稿不进入最终回复或成功历史。

## 能力路由与授权差异

`dispatch_chat_plain_ai_backend` 与新增 `dispatch_chat_agent_backend` 独立，默认均为 Java。`dispatch_chat_python_agent_code` 可绑定 Python 预设；不绑定时保留 Chat 的模型、提示词与预算。`dispatch_chat_python_search_mcp_code` 指向 search 分类 MCP。

图片/视频、Laya 和本次授权集合中的第四阶段工具使整个 QQ 请求明确走 Java。GUI 选择要求第四阶段能力的 Python Agent 会在执行前失败。Python 的模型或工具错误不会触发 Java 回退。概览说明混合能力路由，Agent 记录显示 Python 后端与入口；Java 请求继续沿用既有网关流程，不伪造 Python Agent 记录。

与旧 Java 合并工具列表的行为不同，可执行集合取服务端启用能力、Agent 预设、本次选择及入口授权的交集。显式空工具列表关闭工具；缺失/停用预设直接失败。QQ MCP code 必须同时由运行配置授权、由 Agent 关联；Skill 只能使用已经可用的依赖。管理 API 不接受 model 覆盖、QQ 令牌/身份或 system 消息。

MCP 凭据仅使用 `AGENT_BOT_MCP_*` 环境引用；供应商沿用第二阶段凭据引用。不将令牌、凭据、原始参数、模型内部推理或工具正文写入步骤记录及 SSE。最终回复和用户输入仍属于聊天数据，需由部署方安排保留策略。

## 明确的限制

| 项目 | 限制 |
| --- | --- |
| 活动 Agent | 8 个，额外请求 429；准备和记录阶段也占名额 |
| 工具循环 | 默认 3 轮，可设 1–5；每轮最多 8 次，总计 40 次，额外一次禁用工具收尾 |
| 总预算 | 最多 300 秒；请求只能收紧预设轮次/配置超时 |
| MCP | 最多 3 个通用服务、20 个远端工具、10 页发现；初始化最多 10 秒，单服务配置 1–30 秒 |
| schema/参数 | 各 64 KiB、JSON 深度 16；只接受结构、局部 `$defs`/非循环 `$ref`、基本约束和组合；展开访问最多 512 节点 |
| 不支持的 schema | 远程引用、正则 pattern/patternProperties、未知关键字、循环引用；明确拒绝，不放宽校验 |
| 结果/事件 | 单工具 32,000 字符，模型正文 1 MiB，事件帧 256 KiB；模型工具 ID 和整批参数先验证再执行 |
| 提示与历史 | 角色 32,000，单 Skill 提示 16,000，系统组合 65,536；模型上下文 120,000，包含工具定义及私有续传材料 |
| 搜索/网页 | 查询 300 字符、5 个搜索结果、10,000 字符搜索上下文；网页 3 次跳转、解压后 1 MiB，正文支持 offset 分页 |

远端工具描述最多 2,000 字符；本地 Chat 工具描述允许 16,000，以保留原业务返回的首屏标签 JSON，而不是截断 JSON。总模型上下文仍受统一上限约束。Responses 的 reasoning 输出及 Ollama thinking 只在内存中用于下一轮原生续传，不显示或持久化。

非必需 MCP 仅在连接不可用/超时时跳过并发出警告；认证、来源、协议、schema 和授权错误均失败。工具调用没有自动重试；取消后已经发生的远端副作用无法撤回，未确认结果标为 unknown。

数据库读写取消均等待有界 DB-API 工作线程结束后再释放运行时租约。ASGI 断开时保护收尾记录，SDK 连接由专用任务在同一任务中进入/退出；关闭 SSE 先关闭公共发送流，避免 SDK 的取消通知阻塞关闭。资源清理使用最多 5 秒关闭预算，不允许继续执行工具。数据库连接/读写操作自身具有 5/10 秒超时；线程不能被强制安全终止，取消可能等待正在进行的数据库操作完成。

运行记录属于尽力保存：成功结果保存失败返回 `record_saved=false`；数据库不可用、超时预算耗尽或进程被终止时可能留下 running 记录。查看详情只读，不重新运行 Agent，也不将 running 自动转成成功。

## 启用与回退

按 [README](../../README.md#启用-python-agent) 初始化第二阶段模型表和新的 `sql/agent_bot_agent_init.sql`，在 GUI 登记模型、MCP、Skill、Agent，再切换 Agent 后端为 Python。新 SQL 只创建 7 张表，手动执行，启动时不自动迁移生产数据。

只回退能力时，将 Agent 后端设置为 Java，恢复 Java 可用的模型与 MCP code，并核对保存/应用版本。退回 0.2.0 二进制前：

1. 停止新实例，备份配置与数据库；避免两个机器人消费者同时工作。
2. 经 SettingsStore revision CAS 恢复 Java 配置，移除 `dispatch_chat_agent_backend`、`dispatch_chat_python_agent_code`、`dispatch_chat_python_search_mcp_code` 三个新增字段。
3. 保留原有 agent_max_steps、web_search_max_steps、mcp_services、普通 AI 后端及所有凭据，使用实际 0.2.0 wheel 启动；新增表保留，不执行 DROP。

本次使用 Fake SettingsStore 完成 revision 1→2，然后由单独环境中实际 0.2.0 wheel 校验、写入和读取恢复配置。旧预算、MCP code 与明确标识的测试凭据保持一致，没有对真实数据库实施回退。

## 验证记录

完整 Python 回归、安装产物摘要见本文件末尾的最终验证数据。已运行标准 npm ci、TypeScript、21 项前端测试及生产构建，npm 审计为 0 漏洞。构建保留约 659 kB 主包的 Vite 提示，没有通过关闭告警隐藏体积。

真实 loopback MCP 测试覆盖初始化、发现、调用、SSE 跨源拒绝、重复游标、可选离线、鉴权失败和两种协议的取消。真实 ASGI receive/send 断开测试证明工具只调用一次、取消/unknown 记录保存、无 done、租约及执行名额释放。新增回归还覆盖混合 QQ 路由/模型预检、预设模型选择、并发第九个请求拒绝、完整记忆标签、JSON 转义结果分页和 schema 展开上限。

Edge 浏览器使用真实管理 API、真实 AgentService/工具往返、隔离数据库和模型替身、本机 MCP。桌面/390px 手机完成三个目录页、Agent 对话、停止与取消记录、详情以及 401/422 权限拒绝。九项导航完整，文档宽度等于 390px，无页面级横向溢出，最终应用默认执行上下文异常为 0。截图中的内容明确来自隔离验证环境。

截图：[Agent](phase-three-agents.png)、[Skill](phase-three-skills.png)、[MCP](phase-three-mcp.png)、[对话](phase-three-chat.png)、[记录](phase-three-records.png)、[手机](phase-three-mobile.png)。

原始来源清单的 179 个 SHA-256 均保持一致，迁入适配摘要单独更新。按已确认的执行方式，仅主代理作者复核，没有独立子代理审查、Git 初始化、提交、推送或部署。

实际 MySQL DDL/锁行为、QQ、供应商工具能力、生产 MCP 和公网 DNS/TLS 尚需部署环境联调。本次没有连接真实 QQ、付费模型或生产服务；SQL 测试使用 SQLite DB-API 替身执行参数化操作与真实外键约束。

原生协议参考：[OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling)、[Ollama tool calling](https://docs.ollama.com/capabilities/tool-calling)。适配使用原生调用 ID、参数、结果及续传材料，未把私有推理写入展示/记录。


## 最终验证数据

- Python：1690 passed，1 个既定真实数据库破坏性测试排除；含分支覆盖率 86.21%，门槛保持 80%。
- 前端：21 passed；TypeScript、标准 npm ci、Vite 生产构建通过，npm 审计 0 漏洞。
- Ruff 与 uv lock --check 通过；0.3.0 wheel/sdist 构建成功，wheel 154 个条目，包含两个 Python 包、Agent SQL 与生产界面。
- 独立环境验证导入无机器人启动副作用、0.3.0 版本、SQL/静态资源和 agent-bot / dispatch-chat 两条 CLI 帮助命令。
- 实际 0.2.0 wheel 的 Fake SettingsStore 回退验证通过；旧预算、MCP code 和凭据保留。
- 浏览器目录保存、工具往返、取消、记录、401/422 与手机布局验证通过。应用默认上下文异常为 0；浏览器扩展的隔离上下文出现了无接收端的 promise 错误，按 CDP executionContext.auxData.isDefault 区分并保留原始日志，没有将其算作应用成功证据，也没有屏蔽未知应用错误。
- 来源：179 个原始摘要一致，13 个迁入适配文件（含 2 个前端来源）的摘要单独记录。

可复现命令与测试日志保留在 `.superpowers/sdd/2026-10-12-agentbot-phase-three/`；该目录不进入发布包。

最终 wheel SHA-256：`7c00756b72ed7afff70613bfd9ba4d5e4e86e899672bd2c581753c3cca4acd9f`。

本轮隔离 QA 服务与专用无界面浏览器已关闭，端口 8768/9228 已确认不再监听。
