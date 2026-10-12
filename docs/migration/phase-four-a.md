# 第四阶段 A：Python 媒体与管理界面

交付版本：**0.4.0**。实施日期：2026-10-12。图片理解、直接图片/对话音频生成、Agent 绘图与语音工具已接入 Python 服务和浏览器界面，完成离线回归、真实浏览器验收及独立安装/回退演练。

本阶段在独立 `agentBot` 目录实施。旧 `dispatch-sdk`、`bai-web` 保持只读；179 份后端来源文件及 2 份前端来源文件的原始摘要均未变化。适配后的文件摘要见 [来源清单](source-manifest.json)。

## 已交付的行为

- 模型管理增加图片理解、图片生成接口类型和对话音频能力，随模型 revision 原子保存；版本冲突保留界面草稿。缺媒体表时普通模型管理和文本能力继续可用。
- 图片理解支持 Chat Completions、Responses 和 Ollama 的原生图片消息。图片经过实际解码、格式/尺寸检查和去元数据处理，只用于当前提问；历史继续传纯文本。
- 直接生成支持 `gpt-image`、`legacy-b64` 图片接口与 Chat Completions 对话音频。`legacy-b64` 仅输出 PNG；Ollama 不启用这里的直接图片/音频输出。
- QQ 复用现有工作流、TTS、短期工具令牌及绘图任务队列。管理 Agent 使用自身工具白名单调用 ComfyUI/GPT-SoVITS，不接受客户端伪造 QQ 身份。
- 管理界面提供图片上传、纯图片提问、认证预览/下载、语音播放、停止生成、媒体历史及过期提示。工具已产生文件而最终回答失败或取消时，仍展示文件和“工具已生成，回复未完成”。
- 文件采用独占目录、原子发布、摘要校验、配额和使用租约；热刷新及关闭等待正在进行的文件/数据库操作结束。管理端只能访问管理入口的媒体，QQ 文件不会通过这些接口公开。

Python 一经选中，失败不会自动改走 Java，也不会自动重发有副作用的生成请求。取消不会撤销已经完成的工具操作；QQ 图片任务成功入队后，停止 Agent 不等于撤销队列任务。

## 安装与 SQL 顺序

新实例先按 [README](../../README.md) 准备独立 MySQL、Redis、引导配置及管理员凭据，再只执行统一初始化脚本。合并后的统一初始化、三份兼容分阶段脚本及注释增量脚本共五份 SQL 均随 wheel 打包到 `agent_bot/resources/`，服务启动不会自动执行它们。详细使用范围见 [SQL 使用说明](../../sql/README.md)。

| 场景 | 文件 | 用途 |
| --- | --- | --- |
| 新库 | [agent_bot_init.sql](../../sql/agent_bot_init.sql) | 一次创建全部 25 张表，无须再执行分阶段脚本 |
| 旧库缺 AI 表 | [agent_bot_ai_init.sql](../../sql/legacy/agent_bot_ai_init.sql) | 三张 Python AI 目录/记录表 |
| 旧库已有 AI、缺 Agent 表 | [agent_bot_agent_init.sql](../../sql/legacy/agent_bot_agent_init.sql) | 七张 Agent、Skill、MCP 和运行记录表 |
| 旧库已有 AI/Agent、缺媒体表 | [agent_bot_media_init.sql](../../sql/legacy/agent_bot_media_init.sql) | 三张模型媒体能力、文件元数据、媒体运行记录表 |
| 旧库按需 | [2026-10-12-agentbot-schema-comments.sql](../../sql/2026-10-12-agentbot-schema-comments.sql) | 为旧十张表补中文表/字段注释 |

从 0.3.0 升级时保留已经安装的前两组表，新增媒体表；旧表补注释脚本可另外执行。首次建库的初始化脚本已经含中文注释，无须重复执行补注释脚本。补注释保留字段类型、默认值、可空性和键，未重声明主键或唯一键；执行前备份并评估 MySQL DDL 元数据锁。本次只做静态结构与注释校验，未执行真实 DDL。

不要批量执行 `sql/legacy/` 目录：归档的历史 `dispatch_chat_setting.sql` 含旧安装的删除表语句，部分迁移还会覆盖配置或处理历史数据。

安装 wheel 后可通过 `importlib.resources.files('agent_bot').joinpath('resources/agent_bot_init.sql')` 读取统一安装脚本；媒体旧库升级仍可读取 `resources/agent_bot_media_init.sql`。从源码开发时直接使用仓库 `sql/` 文件。wheel 已包含界面资源，不需要 Node.js；从源码构建时先执行前端构建。

## 后端开关与启用步骤

普通文本使用 `dispatch_chat_plain_ai_backend`，Agent 使用 `dispatch_chat_agent_backend`，媒体使用新增的 `dispatch_chat_media_backend`（默认 `java`）。以下组合描述 QQ 能力路由；管理测试入口要求相应 Python 开关已开启，不会转发到 Java。

| Agent 开关 | 媒体开关 | 纯识图 / 直接图片与音频 | 混合图片或媒体工具的 Agent |
| --- | --- | --- | --- |
| java | java | Java | Java |
| python | java | Java | Java |
| java | python | Python | Java |
| python | python | Python | Python |

不含媒体的 Agent 由 Agent 开关单独决定。包含视频、`job`、`soloncode` 或 Laya 的 QQ 请求仍整次走 Java。检查默认工具授权，避免不需要的未迁移能力使请求继续走 Java。

1. 手动应用所需 SQL，在“供应商管理”和“模型管理”登记并启用服务。凭据保留在本项目环境或 `.env`，目录只保存凭据引用名。
2. 为实际支持的模型显式启用媒体能力。图片生成选择 `gpt-image` 或 `legacy-b64`；对话音频并非 GPT-SoVITS 朗读，两者使用不同链路。
3. 在“机器人配置”保存媒体开关为 `python`；需要 Agent 图片/工具时也保存 Agent 开关为 `python`，确认保存版本与应用版本一致。
4. 启动配置 `AGENT_BOT_MEDIA_DIRECTORY` 默认 `data/media`，相对服务工作目录解析，环境变量优先于不插值的 `.env`。该目录不进入 MySQL 热刷新配置，修改目录须重启。
5. 媒体目录必须可读写、由本实例独占，不能通过符号链接/Windows 重解析点访问。安装媒体表或修复目录后重启，在概览确认媒体就绪。
6. 用“对话测试 → 媒体生成”验证直接输出；用“Agent 对话”验证上传与工具。ComfyUI/GPT-SoVITS 还需原有服务参数、工作流及 Agent 工具白名单。

## 使用限制与记录语义

| 项目 | 当前限制 |
| --- | --- |
| 图片输入 | 单张原始数据 10 MiB，一次原始数据合计 20 MiB；默认最多 4 张、硬上限 8 张；拒绝动画 |
| 模型图片副本 | 单张不超过 2 MiB、16,777,216 像素，去除元数据 |
| 上传 | 最多 2 个并发，30 秒接收/处理预算；未认证不读取上传 body |
| 直接媒体生成 | 最多 2 个并发、120 秒，单个输出最多 10 MiB；服务配置可进一步收紧 |
| 管理 Agent | 每次执行最多生成 1 张图片、1 段语音；保留既有 8 个活动请求、300 秒及工具轮次限制 |
| 文件保留 | 默认 24 小时，总配额 1 GiB，每分钟清理；使用中的文件等待租约释放 |
| 启动对账 | 核对中断写入、损坏文件和超一小时的临时孤儿文件 |

上传 ID 只能绑定一次执行，再次识图须重新上传原文件。预览/下载通过认证请求取得 Blob，退出、切页和迟到响应都会释放 Blob URL，凭据不会写入下载地址。PCM 只提供下载；其他音频是否可播放取决于浏览器格式支持。

过期文件返回 410，未知或 QQ 来源文件返回 404，损坏内容在发送响应头之前返回稳定错误。媒体记录保存元数据及必要的请求/结果文本，不保存二进制、Base64、签名 URL、令牌或模型内部推理。记录写入失败不会重发生成；数据库故障下残留 `running` 不能单独证明请求仍在执行。

## 验证结果

| 检查 | 实测结果 |
| --- | --- |
| Python 完整离线回归 | **1789 passed，1 deselected**；含分支覆盖率 **86.78%**，保留 80% 门槛 |
| 前端测试 | **17 个文件、38 项通过** |
| 静态与依赖检查 | Ruff、TypeScript 严格检查、`uv lock --check`、`npm ci` 均通过 |
| 构建 | Vite 生产构建、0.4.0 wheel/sdist 构建和包资源检查通过 |
| 中文注释 | 按阶段前 AST 基线核对 81 份变更 Python 文件；27 份忽略行尾差异后的变更 TS/TSX 文件具名函数/方法检查通过，并人工复核复杂状态与取消逻辑 |
| SQL | 十张旧表去注释后结构一致；十三张自有表的中文表/列 COMMENT 及增量属性校验通过 |
| 独立安装 | 实际安装 wheel，版本、模块导入、四份 SQL、构建界面资源通过；导入不启动插件 |
| CLI | 已安装环境的 `agent-bot --help`、`dispatch-chat settings --help` 通过 |
| 旧版回退 | 实际安装保留的 0.3.0 wheel，恢复快照仅移除新增字段，其他 139 个配置字段往返值全部一致 |
| 来源保全 | 179 份旧后端来源文件、2 份旧前端来源文件及保留的 0.3.0 安装包摘要未变化 |

Python 验证命令始终排除会清空真实配置数据的环境测试：

```powershell
uv run pytest -m "not e2e" -k "not database_onebot_token_reaches_adapter_without_log_disclosure" --allow-hosts=127.0.0.1,::1
```

跨层回归使用真实 Agent、工具桥、权限注册表、副作用收集器、队列处理器和回复网关，只替换模型/机器人/数据库等外部边界；验证开画答复未送达时不提交图片、入队后停止不撤销任务、客户端断开不重试及热刷新等待清理。取消测试发现并修复了工作流与 TTS 后台线程未结束就归还运行时租约的问题。

浏览器使用最终构建界面、真实 FastAPI/AgentEngine/协议适配器，外部 HTTP 和数据库为替身。完成上传识图、Comfy 出图、TTS 播放、直接图片/音频、停止、生成中退出、部分成果和历史过期流程。0.5 秒 WAV 实际播放并推进时间；桌面及 390px 页面检查通过，移动端文档宽度为 390px，无页面横向溢出。未登录 401、无效输入 422；验收结束剩余 Blob URL 为 0，应用及隔离上下文均无控制台错误。专属回环服务和浏览器已关闭。

以下八张截图均已实际打开检查：

| 场景 | 截图 |
| --- | --- |
| 模型能力 | [模型管理](phase-four-a-models.png) |
| 上传识图 | [Agent 图片输入](phase-four-a-vision.png) |
| 绘图与语音工具 | [Agent 工具输出](phase-four-a-agent-tools.png) |
| 回复失败保留成果 | [部分成果](phase-four-a-partial-result.png) |
| 直接生成图片 | [图片生成](phase-four-a-direct-image.png) |
| 移动端音频 | [390px 音频播放](phase-four-a-mobile-audio.png) |
| 移动端历史 | [390px 媒体记录](phase-four-a-mobile-history.png) |
| 过期历史附件 | [过期提示](phase-four-a-history.png) |

本阶段完成作者复核，按当前会话约定未运行独立子代理评审。未连接真实 QQ、付费模型或生产数据库，未执行真实 MySQL DDL，因此这些环境的部署联调仍需使用部署方实际配置完成。Vite 提示主 JS 块超过 500 kB，生产构建成功；本阶段未扩展为前端拆包优化。

## 回退到 0.3.0

1. 备份当前数据库运行配置及 revision、媒体目录和数据库。若只切能力路由，在 0.4.0 管理界面把媒体后端改回 `java`，恢复 Java 可用的模型标识和工具配置；已有 Python Agent 可按原第三阶段配置继续使用。
2. 退回二进制前停止 0.4.0 实例，确认没有第二个消费者连接同一机器人和 Redis 队列前缀。
3. 用版本校验恢复交接快照，移除本阶段新增的 `dispatch_chat_media_backend`。保留所有其他字段，特别是 OneBot/会话凭据、模型、MCP code、工具授权、Agent 轮次和超时；不要重新执行仅用于空表的 `settings import-env`。
4. 在独立环境安装保留的 `agent_bot-0.3.0-py3-none-any.whl`，按第三阶段流程运行。`AGENT_BOT_MEDIA_DIRECTORY` 属于 0.4.0 启动环境，不应写入旧版数据库配置快照。
5. 新增媒体表及媒体文件保留，不自动删除。0.3.0 不提供新媒体界面；可按原 Java 媒体链路处理请求。

离线演练先在 0.4.0 Fake SettingsStore 上完成 CAS revision 1→2，再移除唯一新增字段；随后由**实际 0.3.0 安装环境**解析并读写完整快照，核对全部字段值。该演练未修改真实配置或机器人连接。

保留的 0.3.0 wheel SHA256：`7c00756b72ed7afff70613bfd9ba4d5e4e86e899672bd2c581753c3cca4acd9f`。

## 交付文件

- [0.4.0 wheel](../../dist/agent_bot-0.4.0-py3-none-any.whl)
- [0.4.0 源码包](../../dist/agent_bot-0.4.0.tar.gz)
- [SHA256 摘要](../../dist/SHA256SUMS-0.4.0.txt)
- [实施计划](../superpowers/plans/2026-10-12-agentbot-phase-four-a.md)与[设计](../superpowers/specs/2026-10-12-agentbot-phase-four-a-design.md)

构建包排除 `.env`、本地账本、数据库文件、上传文件、TTS 输出、日志和运行目录。源码包显式保留根目录 `.env.example`，不会重新包含本地基线里的同名文件；wheel 不含环境文件。归档内容已逐项检查，源码包内文档与当前文件字节一致。详细命令记录与离线验收脚本保留在本地 `.superpowers/sdd/2026-10-12-agentbot-phase-four-a/`，不进入安装包。
