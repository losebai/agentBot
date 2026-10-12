# agentBot 第二阶段迁移记录

日期：2026-10-12。版本：0.2.0。

## 已实现能力

Python 普通 AI 服务与现有 Chat/管理 API 位于同一服务进程；普通请求直接调用供应商适配器，未向旧源码目录或本机 Java 转发以冒充迁移。

| 能力 | 实现 |
| --- | --- |
| 供应商与模型配置 | 三种显式协议、启用/禁用、行 revision 冲突、删除引用约束、精确模型别名与上游 ID |
| 模型协议 | OpenAI Responses、兼容 Chat Completions、Ollama native chat；HTTPX 异步请求，无自动重发或协议探测 |
| 普通同步对话 | 配置模型或 Python 管理测试临时模型；无状态历史、系统提示保护、结果格式和用量 |
| 普通流式对话 | 带鉴权 fetch/SSE、增量 UTF-8、有限帧缓冲、完成/错误互斥、停止与断开清理 |
| 调用记录 | completed/failed/cancelled、正文与元数据、稳定分页、过滤与按需详情 |
| 图形界面 | 新供应商/模型/记录页面、后端与能力状态、流式开关和停止、未保存记录提示 |
| 运行时切换 | `dispatch_chat_plain_ai_backend` 默认 java，可热切 python；租约保护运行中的调用 |

Python 目录采用 `agent_bot_ai_provider/model/session` 新表；第一阶段 `ai_model` 仍仅供 Java 能力预检。新供应商/模型默认禁用。来源清单保留原摘要并补充适配后的摘要，179 个原始文件摘要均一致。

## 未迁移的能力与协议限制

Agent、联网工具、Chat 授权工具、通用 MCP、Laya、图片/视频理解、图片/音频生成及任务投递仍显式请求 Java。Python 故障不触发 Java 回退。QQ 最终请求带任一上述能力时仍使用 Java；默认启用的联网/工具设置可能让 QQ 请求继续走 Java，管理对话始终是普通文本。

供应商只保存 `AGENT_BOT_PROVIDER_` 变量引用。实际密钥由运行环境或本项目 `.env` 提供，环境优先，不插值、不从旧目录复制。界面无实际 Key 编辑框。

Responses 支持输出上限、temperature、top-p；兼容 Chat Completions 增加 presence/frequency penalty、stop，输出上限字段为 `max_completion_tokens`；Ollama 支持 num_predict、temperature、top-p/top-k/min-p、repeat penalty、stop 和布尔 think。不支持的非空选项明确拒绝。兼容服务不接受 `max_completion_tokens` 时，需要显式协议扩展，不能自动换字段重发。

现有记忆压缩固定请求 `enableThinking=false`，Ollama 可以表达；Responses/兼容协议拒绝该选项并保留旧记忆。因此这些协议的模型可以普通对话，但本阶段不能宣称已经完成其记忆压缩选项兼容。

不包含原生 Anthropic、自动模型同步、旧数据库自动导入、外部 Java HTTP 兼容服务或自动服务器历史续聊。取消或预算耗尽时尽力保存记录；若数据库不可用或无剩余预算，记录可能缺失。成功结果保存失败仍返回结果，并明确未保存。

## 启用与回退

启用步骤见根 README：显式导入新 SQL，配置本项目环境凭据，登记并启用供应商/模型，然后切换普通后端与 Chat 模型，等待配置应用。默认 Java 模式不因新表缺失而破坏原有机器人启动；新 AI 操作给出未初始化错误。SQL 包资源也随 wheel 发布。

回退步骤：

1. 备份独立库中的运行配置与模型目录，记录当前保存/应用 revision。
2. 在 0.2.0 界面把普通后端改回 java，同时恢复 Java 服务实际已登记的 Chat 模型 ID、地址和凭据；不能沿用只存在于 Python 目录的别名。
3. 等待保存与应用版本一致，完成普通 Java 对话验证；仅改变后端不会关闭 GUI 或删除记录。
4. 如需退回 0.1.0 二进制，先停止新实例；从备份恢复第一阶段字段集合，去掉 `dispatch_chat_plain_ai_backend`，通过原 SettingsStore 的 revision 流程校验后启用旧版本。不要在运行中的配置表随意删行或忽略 revision。
5. 新 AI 表保留，不执行 DROP。QQ 实例切换时确保只有一个消费者/相同队列前缀，恢复对应 NapCat 连接。

回退演练通过 Fake SettingsStore 将 revision 从 1 更新到 2，同时恢复 java 与原模型，再用单独环境中实际安装的 0.1.0 wheel 校验移除新字段后的配置，测试凭据保留。没有对真实数据库执行回退。会话表包含聊天正文，保留与清理由部署方管理。

## 验证证据

| 检查 | 结果 |
| --- | --- |
| 完整 Python 离线回归 | 1602 passed，1 deliberately deselected；覆盖率 86.96%，门槛 80% |
| 真实 ASGI 断开测试 | 断开后上游关闭，取消记录保存，运行时租约释放，无成功 done |
| 前端类型、行为测试、生产构建 | TypeScript 通过；Vitest 4.1.11 下 12 passed；Vite 6.4.4 构建通过 |
| 前端可重复安装 / 依赖审计 | 标准 npm ci 通过；npm audit 为 0 漏洞 |
| Ruff / uv lock | 通过 |
| wheel/sdist | 0.2.0 可构建；125 个 wheel 条目，包含 SQL、两个包与生产界面 |
| 独立 wheel 安装 | 新环境内模块导入、版本、SQL/JSON/静态资源与 CLI 验证 |
| Edge 浏览器 | 实际管理 API + 隔离 DB/HTTP Mock；新增页面、同步对话、桌面/手机截图，无控制台异常 |
| 来源保护 | 179 个原始摘要一致；旧项目用户工作区修改保留 |

最终 wheel SHA-256：`1d1d88e4e40c220352fd695a451d1092724a637aaf4a8a0c0b32c049fff2a5ca`。

测试命令保留第一阶段的非 e2e 和真实 MySQL 破坏性测试排除项，并只允许 loopback。数据库测试用 SQLite DB-API double 执行仓库 SQL和约束；没有声称测试了真实 MySQL 的锁、DDL 或生产网络。

作者复核覆盖规格、能力路由、错误与取消、包资源、界面与来源摘要；按已确认的无子代理约束，未进行独立子代理审查。没有连接真实 QQ、调用付费模型、迁移生产数据或部署服务。

截图：[供应商](phase-two-providers.png)、[模型](phase-two-models.png)、[记录](phase-two-sessions.png)、[对话](phase-two-chat.png)、[手机](phase-two-mobile.png)。截图中的回复来自明确隔离的模型 Mock。

## 发布验证中发现的补充修正

断开连接时 Starlette 不保证自动关闭已暂停的 body iterator。新增响应类在发送任务原上下文内关闭生成器，并屏蔽清理期间的重复取消，避免 ContextVar 和数据库事务/运行时租约被遗留。

依赖检查发现旧 Vitest 3.2.7 所用 mocker/tinypool 的已公开漏洞，升级测试工具为 Vitest 4.1.11，保持 Vite 6 与 Node 22；显式声明 Testing Library DOM peer 以保证标准 npm ci 可重复安装。来源：[Vitest 公告](https://github.com/vitest-dev/vitest/security/advisories/GHSA-82fw-gwwq-j7x9)、[Tinypool 公告](https://github.com/tinylibs/tinypool/security/advisories/GHSA-85c8-ppgw-ccpr)。

手机屏幕的六个导航项改为三列两行，避免旧样式导致中文逐字换行。生产构建的单包大小仍有 Vite 提示；没有通过关闭提示掩盖体积，也未把无关代码拆分加入本阶段。
