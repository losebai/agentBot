# agentBot Phase Four A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking. 延续当前会话内实施，不创建子代理；每任务验证账本代替 Git 提交。

**Goal:** 让 QQ 和管理界面通过 Python 完成图片理解、图片生成和语音输出，交付带中文代码/SQL 注释、可离线验证及独立安装的 0.4.0。

**Architecture:** 在同一服务内增加 `agent_bot.media`，统一处理图片输入、文件生命周期、媒体生成与记录。复用现有模型目录、Agent 执行、QQ 工具及 ComfyUI/GPT-SoVITS；通过明确的能力路由接通 QQ 和管理 API。

**Tech Stack:** Python 3.12、uv、Pydantic 2、FastAPI、HTTPX、Pillow、MySQL；React/TypeScript、Node.js 22、Vite、pytest/Vitest。维持当前 Python 3.10 语法兼容与依赖锁。

**Spec:** [已确认设计](../specs/2026-10-12-agentbot-phase-four-a-design.md)。范围、行为和验收以该设计为依据。

状态：14 个任务已完成，0.4.0 功能、离线回归、浏览器验收、独立安装和 0.3.0 回退验证通过；详见第四阶段 A 迁移记录。

## Global Constraints

- 仅修改 `D:\items\web\agentBot`；旧 `dispatch-sdk`、`bai-web` 只读，保留原有修改。
- 延续当前会话内实施方式，不创建聊天、子代理、Git 仓库、分支、提交、推送或 PR。
- 使用 Python 3.12、uv、Node.js 22；保留当前包的 Python 版本兼容声明，不升级无关依赖。
- 验证使用 Fake 数据库、HTTP Mock 和回环协议服务，不连接真实 QQ、付费模型或生产服务，不执行真实数据库迁移。
- 新增或修改的 Python 模块、类、函数有中文文档字符串；TypeScript 组件、函数与复杂逻辑有中文注释。注释解释职责、约束与原因，不逐行复述代码。
- 新增或修改 SQL 的脚本说明、表 `COMMENT`、字段 `COMMENT` 使用中文；索引和外键的目的用中文行注释解释。
- 媒体后端默认 Java；Python 被选中后不因失败改走 Java，不自动重试有副作用的操作。
- 输入单图原始数据 10 MiB、单请求原始数据合计 20 MiB；数量默认 4、硬上限 8；模型单图最多 2 MiB、16,777,216 像素。动画明确拒绝。
- 文件默认保存 24 小时，总配额 1 GiB，每分钟清理过期文件；超过一小时的临时孤儿文件启动对账。运行中的文件先释放使用租约再清理。
- Agent 维持最多 8 个活动请求、总预算 300 秒、原工具轮次限制；直接媒体生成最多 2 个并发、120 秒、输出单项 10 MiB。已有 QQ 后台绘图最多 500 秒的预算不改变。
- GUI 每个 Agent 请求最多生成 1 张图片、1 段语音；QQ 既有额度不放宽。
- 不记录二进制、Base64、QQ 签名 URL、令牌、实际凭据或模型内部推理；保留确定的失败/取消/unknown 状态。
- 不迁移视频、语音输入、通用 job、Laya 或代码执行；不建设新任务队列或工作流编辑器。
- 最终 Python 回归分支覆盖率至少 80%；始终排除 `database_onebot_token_reaches_adapter_without_log_disclosure`，不降低既有断言或覆盖率门槛。

## Review Focus

1. 上传会经过现有 64 KiB 管理中间件：只有准确匹配的上传入口可以流式读取 10 MiB；未认证请求不得先读取大文件。任务 10 覆盖无 Content-Length、分块超限和未授权不读 body。
2. 本地工具 schema 的 pattern 被移除后，一批调用中的后一个非法参数不能在前一个产生副作用后才被发现。任务 7/8 覆盖整批本地参数预校验，远端 MCP schema 限制不放宽。
3. 媒体表尚未安装，或模型能力与供应商协议同时被编辑：纯文本仍能运行，能力更新原子化，在途图片请求使用一致快照。任务 2/5 覆盖缺表、CAS 冲突、协议切换和模型快照。
4. 文件已经替换但元数据尚未提交时取消，或热刷新期间到达清理时间：不能遗留错误 ready 状态、重复释放配额或删除使用中文件。任务 4/9/13 覆盖提交故障、取消对账、文件租约和关闭顺序。
5. QQ 开画答复未送达，或图片任务已经入队后 Agent 被停止：不能提前提交、重复提交或宣称图片任务已撤销。任务 7/9/13 覆盖原队列和收集器，不只使用简化工具替身。

## 文件结构与实施约定

| 模块/路径 | 职责 | 任务 |
| --- | --- | --- |
| `src/agent_bot/media/contracts.py`, `errors.py`, `config.py` | 媒体类型、错误、部署配置与限制 | 1 |
| `sql/agent_bot_media_init.sql`, 自有初始化及注释增量 SQL | 新表、中文注释、无损升级 | 1 |
| `src/agent_bot/media/catalog.py` 与 `ai/catalog.py` | 模型能力、CAS、可选表兼容 | 2 |
| `src/agent_bot/media/inputs.py`, `download.py`, `validation.py` | 解码、标准化、QQ 图片获取和输出校验 | 3 |
| `src/agent_bot/media/files.py`, `store.py`, `records.py`, `resources.py` | 文件与元数据、配额、执行记录、进程资源 | 4 |
| `agent/contracts.py`, `service.py`, `ai/providers/agent_*.py` | 图片消息和三种协议 | 5 |
| `src/agent_bot/media/providers.py`, `service.py` | 直接图片及对话音频生成 | 6 |
| `agent/tools/local_media.py`, `media.py`, Chat/Comfy 原有实现 | 本地 schema、QQ 工具、管理媒体工具 | 7 |
| `agent/engine.py`, `tools/runtime.py`, `service.py` | 整批校验、附件事件与部分成果 | 8 |
| `ai/routing.py`, Chat 媒体用例及插件运行时 | 后端选择、模型预检、资源装配 | 9 |
| `admin/media_routes.py`, `media_models.py`, 既有中间件 | 已认证上传、生成、下载和查询 | 10 |
| `web/src/media-types.ts`, `media-api.ts`, 媒体组件与模型页 | 能力编辑、上传和直接生成 | 11 |
| Agent 对话、流解析及运行详情 | Agent 附件与可中断界面 | 12 |
| `tests/media/test_end_to_end.py`, 本地浏览器验收材料 | 跨层取消、QQ 投递、GUI 验证 | 13 |
| 包检查、README、来源清单、迁移文档 | 0.4.0 发布与 0.3.0 回退验证 | 14 |

上表省略前缀的 `agent/`、`ai/`、`admin/` 均在 `src/agent_bot/` 下。新 package 的 `__init__.py` 随所属任务创建，不允许导入触发建表、联网、插件启动或文件清理。

实施时维护 `.superpowers/sdd/2026-10-12-agentbot-phase-four-a/progress.md`，记录各任务 RED/GREEN、中文注释审查、实际变更和验证限制；不把待实施步骤勾成完成。首先保存本阶段触及的现有文件和 0.3.0 包摘要，方便最后比对。

以下测试代码仅列出核心断言，夹具在每任务规定的测试文件中构建；必须真实调用生产实现，不能仅镜像实现。聚焦命令固定为 `uv run pytest <任务测试文件> --no-cov --allow-hosts=127.0.0.1,::1 -q`，首次 RED 应是目标功能尚未实现，而非夹具损坏；GREEN 退出码必须为 0。前端聚焦命令为 `npm --prefix web test -- --run <测试路径>`。

所有任务依次实施；跨任务接口以下文 Interfaces 为准。每个任务结束审查当前差异并写账本，不需要重新请求授权。最终验证通过后才把实现状态标为完成。

### Task 1: 媒体契约、中文 SQL 与可验证的离线数据结构

**Files:** 新增 `src/agent_bot/media/{__init__,contracts,errors,config}.py`、`sql/agent_bot_media_init.sql`、`sql/2026-10-12-agentbot-schema-comments.sql`、`tests/media/{__init__,conftest,test_contracts,test_sql}.py`、`tests/media/schema_baseline.json`；修改 `src/agent_bot/ai/contracts.py`、`sql/agent_bot_ai_init.sql`、`sql/agent_bot_agent_init.sql`。

**Interfaces:**
- 复用 `AiModel`、`Page[T]`、`ChatMediaRequest/Response` 和 `AiError`。定义 `MediaError(AiError)`，稳定错误覆盖未初始化、能力不支持、无效图片、超限、磁盘满、未找到、过期、保存失败和结果未确认；对外不暴露参数或底层异常。
- 在 `ai/contracts.py` 的 AiModel 后定义 `ModelMediaCapabilities(vision_enabled=False, image_api_profile='none', audio_chat_enabled=False)`；image_api_profile 仅 none/gpt-image/legacy-b64。它属于模型目录，不能放在会反向导入 AiModel 的 media/contracts.py 后再被 ai/contracts.py 导入，避免循环依赖。
- `MediaOwner(entry: Literal['admin','qq'], session_id: str)`；`MediaScope(owner: MediaOwner, execution_id: str, execution_kind: Literal['agent','media'])`。均由服务器构造，不接受外部 identity/token 字段。
- `ModelImage(media_type, data: bytes, source_size_bytes: int)`：data 排除 repr 与默认序列化，类型仅 PNG/JPEG/WebP；只允许构建通过任务 3 校验的内容。`ValidatedImage(media_type, data: bytes, model_image: ModelImage)` 保存已校验原图及模型副本，均不公开序列化。
- `MediaAssetView(id, purpose, mime_type, size_bytes, sha256, status, created_at, expires_at)` 只提供公开元数据；`MediaAssetRecord` 另含 owner、可空 execution_id/execution_kind、storage_key。持久化显式把 owner 展开为 entry/session_id，不直接把嵌套对象写入 SQL。ID 为 32 位随机小写十六进制；purpose=input/output；状态 writing/ready/expired/failed。执行字段同空或同非空。
- `MediaRunStart/Finish/Summary/Detail` 对应设计第 9 节字段，运行状态 running/completed/failed/cancelled；Detail 增加 attachments。`MediaGenerationResult(execution_id, request_id, backend='python-media', model, text, attachments, duration_ms, record_saved, response)` 中 response 是供 QQ 消费的私有 `ChatMediaResponse`，不进入 API 序列化。
- `MediaConfig.from_environment()` 读取 `AGENT_BOT_MEDIA_DIRECTORY`，环境优先于不插值 `.env`，默认 `data/media`，启动时固定；配额、TTL 与硬上限用具名常量。这里只校验配置，不创建目录。
- 新 SQL 按设计创建三张表，时间为 UTC 的 DATETIME(6)，状态字段带中文取值说明。模型媒体表用 model_id 外键 ON DELETE CASCADE；资产以 `(execution_kind, execution_id)`、`(status, expires_at, id)` 索引查询；运行采用 `(created_at, execution_id)` 和会话索引。
- 对两份旧自有初始化脚本只补 COMMENT，不修改类型/默认值/可空性/排序规则/键。增量脚本为这十张既有表提供 ALTER COMMENT/MODIFY COLUMN，并完整重述原字段属性；不包含删除或重建数据语句。新媒体表注释直接来自新初始化脚本。
- `tests/media/conftest.py` 复用 SQLite DB-API 替身，提供带三张媒体表的 `media_database`、参数、已启用模型、`tmp_path` 文件根、可推进时钟及 Mock 传输；保留独立的缺媒体表夹具。SQLite DDL 不冒充 MySQL 注释验证。

- [x] **Step 1:** 编写 `test_private_media_bytes_are_not_serialized`、`test_media_limits_and_defaults`、`test_owned_sql_comments_preserve_schema`。将修改前十张自有表的结构基线保存为 `tests/media/schema_baseline.json`，测试不依赖被排除的本地账本目录；比较去除注释后的规范化结构，逐表逐字段检查中文 COMMENT；忽略字符串和注释后检查危险 SQL，而非仅搜索单词。
  ```python
  assert ModelMediaCapabilities().model_dump() == {
      'vision_enabled': False, 'image_api_profile': 'none', 'audio_chat_enabled': False}
  assert 'data' not in model_image.model_dump()
  assert owned_table_count == 13
  assert schema_before_comments == schema_after_comments
  ```
- [x] **Step 2:** 运行 `tests/media/test_contracts.py tests/media/test_sql.py`，确认缺少类型/脚本的 RED。
- [x] **Step 3:** 实现上述契约、离线夹具及四份 SQL 变更；对 SQL 执行顺序、DDL 元数据锁和无自动执行补中文说明。
- [x] **Step 4:** 重跑两个测试文件，执行 `uv run ruff check src/agent_bot/media tests/media`，预期全部通过。
- [x] **Step 5:** 保存原 SQL 结构基线、文件摘要和校验结果到执行账本。

### Task 2: 媒体模型能力、配置持久化及一致快照

**Files:** 新增 `src/agent_bot/media/catalog.py`、`tests/media/test_catalog.py`；修改 `src/agent_bot/ai/{contracts,catalog}.py`、`src/agent_bot/agent/catalog.py`、`src/dispatch_chat/config.py`、`.env.example`、`tests/unit/test_config.py`、`tests/unit/test_settings.py`。

**Interfaces:**
- `ModelCreate.media`、`ModelView.media` 使用 `ModelMediaCapabilities`；`ModelUpdate.media` 省略表示保留、显式 null 拒绝、对象表示完整替换三项；View 另含只读 `media_initialized: bool`。
- `read_model_media(connection, model_id: int) -> tuple[ModelMediaCapabilities, bool]` 在已有事务中读取；缺表仅把 MySQL 1146 映射为未初始化，其他数据库故障继续上报。存在表但无配置行表示全禁用、initialized=true。
- `write_model_media(connection, model_id: int, capabilities: ModelMediaCapabilities) -> None` 被模型新增/修改事务调用；能力与模型 revision 原子提交。明确提供 media 时要求新表存在，即使三项都关闭。
- AiCatalog SQL 使用显式持久化列白名单，禁止把新 View 嵌套字段传入 INSERT/SELECT readiness。无 media 的旧创建/更新在缺媒体表时仍能工作。
- `AiCatalog.resolve()`、`AgentCatalog.snapshot()` 在同一连接内取得能力；供应商改为 Ollama 且仍有图片生成/对话音频能力时拒绝；调整供应商协议与能力检查使用同一事务和锁顺序。
- RuntimeConfig 增加 `dispatch_chat_media_backend: Literal['java','python']='java'`，配置说明使用中文，沿用 SettingsStore CAS 和热刷新，不新增运行时目录字段。

- [x] **Step 1:** 编写 `test_plain_catalog_works_without_media_tables`、`test_media_update_uses_model_revision`、`test_provider_protocol_change_rejects_incompatible_media`、`test_snapshot_keeps_previous_media_capability` 及配置往返测试。
  ```python
  assert old_model.media_initialized is False
  assert conflict.status_code == 409
  assert snapshot.resolved.model.media.vision_enabled is True
  assert reloaded.dispatch_chat_media_backend == 'python'
  ```
- [x] **Step 2:** 运行 `tests/media/test_catalog.py tests/ai/test_catalog.py tests/agent/test_catalog.py tests/unit/test_config.py tests/unit/test_settings.py`，确认目标行为 RED。
- [x] **Step 3:** 实现嵌套字段与 SQL 列分离、原子能力更新、可选表读取和新配置；全模型列表在一次事务内批量读取媒体配置，避免逐项额外开连接。
- [x] **Step 4:** 重跑上述范围，补查“不带 media 更新普通字段”不清空已有能力，预期全绿。
- [x] **Step 5:** 记录缺表兼容、CAS 和协议切换结果及中文字段说明审查。

### Task 3: 统一图片校验、压缩与 QQ 下载边界

**Files:** 新增 `src/agent_bot/media/{inputs,download,validation}.py`、`tests/media/{test_inputs,test_download,test_validation}.py`；按需提取复用 `src/dispatch_chat/multimodal.py` 的标准化函数；复用 `src/agent_bot/agent/tools/public_http.py` 的 `PublicTransport`。

**Interfaces:**
- `ImagePreparer.prepare(data: bytes, content_type: str | None) -> ValidatedImage`：真实解码，拒绝截断、动画、MIME 不符及超限；生成去除 EXIF/元数据的模型副本，保留校验后的原图用于图生图。单图 10 MiB、模型 2 MiB/16,777,216 像素；重用既有压缩算法，不新增图像依赖。
- `ImagePreparer.prepare_many(images: Sequence[ValidatedImage], *, maximum_count: int) -> tuple[ModelImage,...]` 按原图字节总和检查 20 MiB，max_count 取配置且不超过 8，不静默截断。
- `QQImageDownloader.fetch(url: str) -> ValidatedImage`：只接受现有可信 QQ HTTPS 域、每次重定向重新校验，最多 2 跳、15 秒且继承更短请求预算；使用已验证数字地址连接及原 Host/SNI，不能把公网限制用于管理员配置的本地 Comfy/TTS 地址。
- `validate_generated_media(response: ChatMediaResponse, *, output_type: str, format: str, max_bytes: int) -> bytes`：唯一媒体项、严格 Base64、类型和格式匹配；图片实际解码；音频复用并强化已有签名校验，PCM 必须非空且 16 位样本字节对齐，无法据此声称音频内容正确。

- [x] **Step 1:** 写图片格式/动画/截断/总量矩阵、重绑定及跨域跳转测试；用有效小尺寸合成图片，像素炸弹不实际分配巨大内存。
  ```python
  assert len(prepared.model_image.data) <= 2 * 1024 * 1024
  assert normalized_image.getexif() == {}
  assert remote_connection_count == 0  # 混入私网 DNS 结果时
  assert rejected_total_bytes == 20 * 1024 * 1024 + 1
  ```
- [x] **Step 2:** 运行三个新测试文件及 `tests/unit/test_multimodal.py`，确认新增边界 RED。
- [x] **Step 3:** 实现准备、下载和结果验证；纯图片计算在线程执行，取消必须等待有界工作结束后释放文件/运行时资源。
- [x] **Step 4:** 重跑本任务测试及 `tests/agent/test_public_http.py`，证明公网读取边界与原 QQ 路径未退化。
- [x] **Step 5:** 记录格式兼容变化（本阶段拒绝动画）、尺寸及 DNS 连接验证证据。

### Task 4: 媒体文件、元数据与生命周期

**Files:** 新增 `src/agent_bot/media/{files,store,records,resources}.py`、`tests/media/{test_store,test_records,test_resources}.py`。

**Interfaces:**
- `MediaFiles(root: Path)` 只操作解析后的受控根及服务端 ID 文件；临时文件、最终文件和清理路径不包含客户端名称。初始化/读取/删除均拒绝符号链接、Windows 目录联接逃逸；通过根目录独占锁防止多个进程共享该存储，冲突只使媒体未就绪。
- `MediaStore.put(*, owner: MediaOwner, purpose: str, data: bytes, mime_type: str, scope: MediaScope | None=None) -> MediaAssetView`：配额计入临时和正式文件，先占位 writing、原子替换、核对摘要和长度再置 ready；仅 ready 可返回附件。
- `MediaStore.describe(id: str, *, owner: MediaOwner | None=None, admin_only: bool=False) -> MediaAssetView`；`MediaStore.open_content(id: str, *, owner: MediaOwner | None=None, admin_only: bool=False) -> AsyncContextManager[BinaryIO]` 在打开期间固定文件租约；两种权限参数必须恰好选择一个，均未给出时拒绝。通过任务 10 已认证入口的 admin_only 才允许查看不同 admin 会话的历史附件，QQ 附件仍拒绝。
- `MediaStore.bind_inputs(ids: tuple[str,...], scope: MediaScope) -> AsyncContextManager[tuple[ValidatedImage,...]]`：验证入口/会话/input 用途、数量/总量/完整性/未到期，原子绑定 execution。一个上传 ID 只绑定一个执行；同执行幂等，跨执行重用返回 409。GUI 再次发送保留文件时先重新上传，不能静默引用已消费 ID。
- `MediaStore.list_execution(scope: MediaScope) -> tuple[MediaAssetView,...]`；`cleanup(now: datetime)` 和 `reconcile(now: datetime)` 使用注入时钟。过期已知项返回 410，未知/越权返回 404；过期元数据保留，避免失去历史状态。
- `MediaRunStore(AiDatabase)` 提供 `begin(MediaRunStart)`、`finish(MediaRunFinish)`、`get_run(execution_id)`、`list_runs(limit=50,cursor=None,session_id=None,status=None)`；复用现有 keyset 分页边界，终态不能覆盖终态。
- `MediaResources.start()/aclose()` 拥有单个 MediaStore、2 个直接生成名额和清理循环，读取 MediaConfig；start 才做文件/DB 就绪检查，每 60 秒清理，关闭先停止清理再释放根目录锁。缺媒体表或不可写目录仅报告媒体未就绪，不阻止 Java/纯文本启动。此资源属于进程，热刷新不重复创建；启动未就绪后手动安装媒体表或修复目录需要重启服务，界面明确说明，不在每次状态查询中反复初始化资源。

- [x] **Step 1:** 写 `test_cancel_between_replace_and_ready_recovers`、`test_expired_asset_waits_for_reader_lease`、`test_quota_includes_pending_writes`、`test_cross_session_and_qq_asset_are_hidden`、`test_reconcile_does_not_delete_unowned_files` 和分页/CAS 测试。
  ```python
  assert await store.list_execution(scope) == ()  # 未成功发布 ready 的写入
  assert active_reader_can_finish is True
  assert rejected_error.code == 'MEDIA_STORAGE_FULL'
  assert expired_error.status_code == 410
  assert foreign_error.status_code == 404
  ```
- [x] **Step 2:** 运行任务三个新测试文件，确认存储、对账及记录行为缺失造成 RED。
- [x] **Step 3:** 实现事务与文件状态机，取消期间等待已启动文件/DB 工作完成；DB 提交失败保留可对账状态，不错误返还仍占用的文件配额。启动仅清理本服务格式且超过一小时的孤儿/临时文件。
- [x] **Step 4:** 重跑测试，加入替换成功后模拟进程重建、同 ID 摘要被修改、两个并发写竞争最后配额及根锁冲突；不创建根目录外破坏性操作。
- [x] **Step 5:** 记录单进程存储约束、崩溃对账结果和全部路径边界检查。

### Task 5: Python Agent 图片消息与三种供应商协议

**Files:** 修改 `src/agent_bot/agent/{contracts,service,engine}.py`、`src/agent_bot/ai/providers/{agent_chat,agent_responses,agent_ollama}.py`；新增 `tests/media/test_vision_protocols.py`、`tests/media/test_agent_inputs.py`。

**Interfaces:**
- `AgentRequest` 增加内部 `image_urls: tuple[str,...]=()`（QQ）、`attachment_ids: tuple[str,...]=()`（admin），两者互斥且排除 repr/公开序列化；入口 authority 校验来源。管理 DTO 只允许 attachment_ids。
- `TurnMessage.images: tuple[ModelImage,...]=()` 仅 user 角色可用；数据排除序列化，模型 token/字符统计只算原有文本和工具内容，另外统计图片数/字节。
- `AgentService(..., media_store=None, image_downloader=None)` 在已有 8 个活动名额和总预算内取得模型快照、检查 vision_enabled，然后准备图片；只关联最后一条 user 消息。空白 prompt 仅在有图片时转换为既有 `DEFAULT_IMAGE_PROMPT`；无图空白仍拒绝。
- admin 使用任务 4 的 bind_inputs 并持有至整个执行结束，QQ 使用任务 3 下载器。下载/文件准备不能发生在模型能力和授权预检之前。
- 三种 `payload(call, stream)` 映射：Chat 的 text/image_url、Responses 的 input_text/input_image、Ollama 的 images；只发送规范化模型图像。工具往返后该用户图片保持一次，原生 continuation 及工具 call ID 逻辑不变。

- [x] **Step 1:** 为三协议各写“图片输入→工具→最终回复”完整 Mock 往返；写未开启 vision、角色非法、图片被错误计入文本预算及文字路径回归。
  ```python
  assert model_requests[0].image_bytes == model_requests[1].image_bytes
  assert model_requests[1].image_count == 1
  assert download_calls == 0  # 模型没有声明 vision 能力
  assert final.content == '识图完成'
  ```
- [x] **Step 2:** 运行两个新测试文件和 `tests/agent/test_model_protocols.py tests/agent/test_engine.py`，确认图片路径 RED。
- [x] **Step 3:** 实现图片准备及三协议映射，单轮丢失图片、无能力降级为文字均视为错误；给所改函数和协议差异补中文注释。
- [x] **Step 4:** 重跑该范围，验证图片任务后下一条无附件请求不会自动附带上轮图片。
- [x] **Step 5:** 保存脱敏请求结构及测试结论；不把真实图片 Base64 放入账本。

### Task 6: 直接图片及对话音频生成

**Files:** 新增 `src/agent_bot/media/{providers,service}.py`、`tests/media/{test_generation_protocols,test_generation_service}.py`；按需复用 `src/agent_bot/ai/providers/transport.py` 的有界读取。

**Interfaces:**
- `MediaProvider.generate(request: ChatMediaRequest, resolved: ResolvedModel, credential: SecretStr | None) -> ChatMediaResponse` 用独立 ProviderTransport，验证能力/参数后调用固定端点，不接受请求覆盖 base_url。
- gpt-image：`/images/generations`，model/prompt/n=1/output_format，不发送 response_format；legacy-b64：n=1/response_format=b64_json，仅 PNG，不发送 output_format。URL 输出直接拒绝，不下载。
- 对话音频：`/chat/completions`，stream=false、messages、modalities=['text','audio']、audio.voice/format，唯一 choice/assistant 音频，文字优先 content，空时取 transcript；不得通过 `/audio/speech` 朗读用户问题替代。不接受工具调用、截断或空音频终态。
- `MediaGenerationService.generate(request: ChatMediaRequest, owner: MediaOwner) -> MediaGenerationResult`：能力快照、凭据、全局 2 个名额、总预算 min(调用者剩余,120秒)、记录、结果验证和文件保存；active 计数包含准备与持久化，忙时 429，不无限等待。
- 上游读取最多 `4*((max_bytes+2)//3)+65536` 字节并覆盖解压后大小；结果调用任务 3 的校验器。QQ 返回 response 私有字段供原链路发送；管理入口返回 attachments。
- 文件保存失败返回 `MEDIA_SAVE_FAILED`，记录说明已发生上游调用；仅运行记录写入失败且文件已 ready 时保留结果、record_saved=false。可用的媒体文件元数据不能被“尽力保存”略过。

- [x] **Step 1:** 编写协议负载/格式矩阵、唯一媒体项、Base64、超限、取消、磁盘失败及第三个并发请求测试。
  ```python
  assert 'response_format' not in gpt_image_payload
  assert audio_payload['messages'] == conversation_payload
  assert audio_payload['modalities'] == ['text', 'audio']
  assert busy_error.status_code == 429
  assert provider_call_count == 1  # 保存失败也不得重做上游请求
  ```
- [x] **Step 2:** 运行 `tests/media/test_generation_protocols.py tests/media/test_generation_service.py`，确认 RED。
- [x] **Step 3:** 实现两种图片配置及对话音频协议、共享用例和独立媒体记录；命名不掩盖对话音频与固定 TTS 的差异。
- [x] **Step 4:** 重跑测试并验证 Python 生成中 Java Fake 的调用次数为零；无效 PCM 和无法浏览器播放的 PCM 行为分别验证。
- [x] **Step 5:** 记录不支持组合、部分成果错误码及已验证协议；不声称实际模型可用。

### Task 7: QQ 媒体工具与独立管理工具适配器

**Files:** 新增 `src/agent_bot/agent/tools/{local_media,media}.py`、`tests/media/{test_qq_tools,test_admin_tools}.py`；修改 `src/agent_bot/agent/contracts.py`、`src/agent_bot/agent/tools/{policy,chat}.py`、`src/dispatch_chat/integrations/comfy.py`、`src/dispatch_chat/services/workflow_execution.py`。

**Interfaces:**
- 拆分工具集合：`ADMIN_MEDIA_TOOLS` 为列表/绘图/TTS 三项；`QQ_ONLY_TOOLS` 为原四项 Chat 工具及 local_workflow_tasks/cancel；`MEDIA_TOOLS` 为三项共享工具及两项图片任务工具；`CHAT_TOOLS` 为 QQ 可使用集合；`LEGACY_TOOLS` 剩 job/soloncode。admin 策略只移除 QQ_ONLY_TOOLS，不误移除已授权媒体工具。
- `project_local_schema(name: str, schema: dict) -> dict` 仅投影已登记本地工具的 pattern；`validate_local_call(call: ToolCall) -> None` 使用原 WorkflowGenerateArguments/WorkflowTasksArguments/WorkflowCancelArguments/TtsGenerateArguments 完整校验，统一脱敏错误；不改变远端 MCP schema 规则。
- ChatToolBridge 继续每次通过 ChatMcpServer 校验 token 和权限，QQ 副作用、收集器和队列原样复用。不为管理入口制造 ChatToolContext 身份。
- `ComfyClient.upload_input_bytes(data: bytes, *, media_type: str, request_scope: str) -> WorkflowInputImage` 与既有 URL 方法共用验证/上传实现，旧方法签名不变。
- `WorkflowExecutionService.prepare_request(..., image_urls=(), request_scope: str, input_image: DownloadedImage | None=None)` 增加内部已校验输入选项；两种输入来源互斥，保留工作流快照校验、fixed_reference 和严格一张图语义，不能接收未经校验的 Comfy 文件路径。
- `AdminMediaTools.bind(scope: MediaScope, images: tuple[ValidatedImage,...]) -> AdminMediaToolSession`；session 提供 definitions、`validate(call)`、`call(call) -> ToolResult`。使用已装配 workflow_service/tts_client/media_store，按预设与服务端配置授权，一次图片/语音额度先占用再调用；工具定义 source='media'。
- 本任务同时扩展 `ToolDefinition.source` 接受 media，以及 `ToolResult.attachments`，使工具单独可测；任务 8 再实现对应执行事件。provider 只把 ToolResult.content 传回模型，不将 attachments 当作模型返回内容。
- 管理绘图用 `active_image_task` 的请求局部执行器记录真实 prompt_id；取消仅调用已有 `ComfyClient.cancel(prompt_id)`，无法确认时留下 unknown。图片和 TTS 完成后保存 ready 文件，ToolResult 增加 `attachments: tuple[MediaAssetView,...]`，content 仅提供受限摘要。

- [x] **Step 1:** 写 QQ 令牌失效/跨用户任务取消、GUI 拒绝 QQ 工具/任意路径、工作流零图/多图、原参数校验及固定 TTS 结果测试。
  ```python
  assert invalid_workflow_key_side_effects == 0
  assert admin_tools_names.isdisjoint(QQ_ONLY_TOOLS)
  assert uploaded.subfolder == f'dispatch-chat-i2i/{scope.execution_id}'
  assert all(asset.status == 'ready' for asset in result.attachments)
  ```
- [x] **Step 2:** 运行两个新测试及 `tests/unit/test_chat_tools.py tests/unit/test_image_task_tools.py tests/unit/test_comfy_upload.py tests/unit/test_fixed_reference_workflow.py`，确认新接入 RED。
- [x] **Step 3:** 实现本地 schema 投影和纯参数验证、入口适配器、Comfy 字节上传；TTS 使用现有清洗规则和 WAV 校验，复制到媒体存储后不擅自删除其他请求的 TTS 文件。
- [x] **Step 4:** 重跑相关测试，并运行 `tests/agent/test_policy.py tests/agent/test_chat_tools.py tests/agent/test_mcp.py`，证明远端 schema 和 QQ 原规则未放宽。
- [x] **Step 5:** 记录 QQ 已有任务语义、管理媒体授权与工具额度证据。

### Task 8: 整批工具验证、附件事件与失败后的成果

**Files:** 修改 `src/agent_bot/agent/{contracts,engine,service,records}.py`、`src/agent_bot/agent/tools/runtime.py`；新增 `tests/media/test_agent_artifacts.py`；扩展 `tests/agent/test_engine.py`。

**Interfaces:**
- `BoundTools.validate(call: ToolCall) -> None` 对 source=chat/media 的已登记本地工具执行任务 7 的完整原参数校验；`AgentEngine` 在一批工具的任何调用开始前验证整批，不能只在单个 handler 内校验。
- `ToolRuntime.open(snapshot, request, authority, *, media_scope: MediaScope | None=None, images: tuple[ValidatedImage,...]=())` 按入口只绑定一种媒体适配器，仍检查重名/权限交集/MCP 范围；显式 tools=() 不打开工具服务。
- AgentService 根据已绑定输入构造 MediaScope，媒体上下文通过显式参数传递，不放进模型参数/客户端 authority。实例接收的媒体能力允许集来自运行时快照。
- `AgentEvent` 新增 kind='artifact' 和 `attachment: MediaAssetView | None`，要求非空且 ready；每个工具完成后发 tool_result，再按实际生成顺序发 artifact。序号仍由 AgentService 统一递增。
- `AgentCompletionMetadata.attachments` 默认空元组，使同步结果与 done 元数据一致；`RunDetail.attachments` 查询文件存储关联，不新增 Agent 历史表的二进制列。仅 admin 入口向 GUI 返回附件，QQ 结果继续使用网关收集器。
- 工具产物已保存但后续取消/失败时可从运行详情查看；若事件尚未发出，finally 查询已发布资产用于记录，不能把 ready 产物标成不存在。生成确认不明保留 unknown，取消不再触发 handler。

- [x] **Step 1:** 写 `test_later_invalid_local_call_prevents_entire_batch`、`test_ready_artifact_survives_final_model_failure`、`test_cancel_before_artifact_event_keeps_record_attachment`、`test_artifact_sequence_and_done_match`。
  ```python
  assert calls_executed == []  # 第二个调用的 workflow_key 不合法
  assert run.status == 'cancelled'
  assert run.attachments[0].status == 'ready'
  assert [e.sequence for e in events] == sorted({e.sequence for e in events})
  ```
- [x] **Step 2:** 运行 `tests/media/test_agent_artifacts.py tests/agent/test_engine.py tests/agent/test_service.py`，确认 RED。
- [x] **Step 3:** 实现批次验证、附件事件与记录关联，维持第三阶段 start/done/error、草稿重置、终态后禁止事件及取消收尾机制。
- [x] **Step 4:** 重跑该范围，检查事件/日志/RunStep 均不包含 Base64、磁盘路径、上游签名 URL 或令牌。
- [x] **Step 5:** 在账本记录事件顺序和“工具已生成、回复未完成”的明确行为。

### Task 9: QQ 能力路由、模型预检与资源装配

**Files:** 修改 `src/agent_bot/ai/routing.py`、`src/dispatch_chat/services/chat/{contracts,media,service}.py`、`src/dispatch_chat/plugin/{runtime,application}.py`、`src/agent_bot/admin/runtime.py`；新增 `tests/media/test_routing.py`、`tests/media/test_runtime.py`。

**Interfaces:**
- `select_route(request, *, plain_backend='java', agent_backend='java', media_backend='java') -> RouteDecision` 按设计表先判断仍未迁移能力；图片-only 选择媒体后端并在 Python 内强制 tools=()/mcp_services=()；媒体与 Agent 混合时双开关均 Python 才走 Python。
- `RoutingAiClient(..., media_backend='java', media_service=None)`：complete 传入本次 QQ 图片；generate_media 保持外部签名，通过媒体后端调用 MediaGenerationService 并返回其 response。
- `RoutingAiClient.media_route(output_type) -> RouteDecision` 供 `ChatMediaGenerator` 预检使用；`RoutedModelGuard.ensure_enabled(model_id, capability=...)` 支持 Python 媒体模型目录。Agent 已使用预设模型时只由 AgentSnapshot 校验，不先拿 QQ 普通 alias 查 Java。
- `build_runtime_state(..., media_resources: MediaResources | None=None)` 为兼容测试保留可空参数，真实插件传入进程资源。先装配 Comfy/TTS，再创建只引用本运行时客户端的管理工具和媒体服务；不复用上个已关闭运行时的 HTTP 客户端。
- 插件 application 构造 MediaResources 不触发 I/O；on_startup 启动、热刷新共享；on_shutdown 等请求/队列退出后关闭各运行时客户端，再关闭媒体清理任务和数据库池。媒体服务只关闭自己拥有的 ProviderTransport，不重复关闭 Comfy/TTS/DB。
- PluginRuntimeProvider 在 lease 内提供 media_service/media_store，管理端不缓存热刷新前对象；目录配置来自任务 1 的启动配置，运行时媒体开关可热更新。

- [x] **Step 1:** 参数化设计路由表；写混合 media/job/laya、只有图片但 Agent 后端 Java、模型预检一致、刷新期间正在生成和默认缺表启动测试。
  ```python
  assert (decision.backend, java_complete_calls) == ('python', 0)
  assert java_guard_calls == 0  # Python 图片/音频模型预检
  assert refreshed_resources is initial_resources
  assert closed_resources_order[-1] == 'database_pool'
  ```
- [x] **Step 2:** 运行两个新测试及 `tests/ai/test_routing.py tests/agent/test_routing.py tests/unit/test_runtime_reload.py`，确认新路由 RED。
- [x] **Step 3:** 接通路由、预检、旧 Chat 媒体契约、客户端所有权和生命周期；明确中文路由原因，不执行故障回退。
- [x] **Step 4:** 重跑上述范围，覆盖媒体失败、切回 Java、QQ 入队后 Agent 停止不取消图片任务；原 Java 开关路径测试继续通过。
- [x] **Step 5:** 记录所有开关组合、默认启动兼容和关闭顺序。

### Task 10: 管理上传、媒体执行与附件访问 API

**Files:** 新增 `src/agent_bot/admin/{media_routes,media_models,media_requests}.py`、`tests/admin/{test_media_routes,test_media_disconnect}.py`；修改 `src/agent_bot/admin/{http,routes,models,errors,agent_chat,agent_routes}.py`。

**Interfaces:**
- `install_media_routes(router: APIRouter, runtime) -> None` 安装在现有 Bearer 认证路由下。POST `/media/uploads?session_id=...` 接收单文件原始 body，成功 201 返回 MediaAssetView；不使用 multipart 或请求中的文件路径。
- 仅上述准确路径+POST 的 middleware 分支改用计数 receive，不预缓冲；接口通过鉴权后才消费 body。其他管理 body 仍为 64 KiB。上传最多 10 MiB，2 个并发、30 秒接收预算；超出分别 413/429/408，断开撤销占用及临时文件，Content-Length 不能绕过累计检查。
- `MediaGenerationProbeRequest(model, session_id, prompt, messages=[], output_type, image_format=None, voice=None, audio_format=None)`：prompt 最多 2000 字符、history 最多 24 条/12000 字符、禁止 system，模型 alias 不超过 200；session 沿用 ASCII 128 字符规则，服务端添加 admin_ 前缀。不接受 owner、endpoint、路径、凭据、timeout 扩大或任意 max_bytes。
- POST `/media/generations`：IMAGE 把 prompt 转为 ChatMediaRequest.prompt，AUDIO 把 prompt 追加到用户 messages；预算/大小取服务器配置并收紧；返回 MediaGenerationResult 的公开字段。
- GET `/media/runs` 和 `/media/runs/{execution_id}` 仅查询 admin 记录；GET `/media/{id}`、`/media/{id}/content` 仅访问 admin 媒体，未知/QQ 来源 404、过期 410。固定路由先注册；内容使用受限文件租约和正确 MIME/Content-Disposition/nosniff/no-store，不暴露 storage_key。
- `AgentProbeRequest.attachment_ids` 默认空、去重、最多 8；允许仅图无文字，拒绝 URL/bytes/QQ 身份；只授予配置已启用的 ADMIN_MEDIA_TOOLS。RunDetail 添加附件时检查记录 entry。
- `run_until_disconnect(request: Request, operation: Awaitable[T]) -> T` 供上传后计算、同步生成和同步 Agent 使用：请求体已消费后监听 ASGI disconnect，取消并等待 operation 清理；SSE 继续用 ManagedStreamingResponse，在生成器原任务释放租约。
- ServiceStatus.media_backend 改为 java/python，新增 python_media 就绪信息；已迁移媒体从 legacy_capabilities 中移除，留下 video/job/laya/soloncode 等真实边界。

- [x] **Step 1:** 写 auth/422/404/410 测试、未认证不读 body、伪 Content-Length、无长度慢上传、普通 JSON 超过 64 KiB、生成成功及断开测试。
  ```python
  assert (unauthorized.status_code, received_body_calls) == (401, 0)
  assert oversized_json.status_code == 413
  assert 'data' not in generation_response.json()
  assert runtime.active_leases == 0
  assert upstream_submissions == 1  # 断开后不重试
  ```
- [x] **Step 2:** 运行两份新测试及 `tests/admin/test_auth.py tests/admin/test_routes.py tests/admin/test_agent_routes.py tests/admin/test_agent_stream.py`，确认 RED。
- [x] **Step 3:** 实现 DTO/路由、上传中间件例外、同步断开处理、错误文案和状态；content 响应不把管理令牌放进 URL。
- [x] **Step 4:** 重跑相关范围，使用真实 ASGI receive/send 断开测试，不能只调用协程 cancel 模拟浏览器断开。
- [x] **Step 5:** 记录 API 路径、限制及租约释放证据。

### Task 11: 模型能力编辑、上传与直接媒体生成界面

**Files:** 新增 `web/src/{media-types,media-api}.ts`、`web/src/components/{MediaUpload,MediaAttachment,MediaGenerator}.tsx` 及同名测试；修改 `web/src/{ai-types,types}.ts`、`web/src/pages/{Models,Chat,Overview}.tsx`、`web/src/styles.css` 及相关测试。

**Interfaces:**
- MediaApi 扩展 ApiClient，提供 `upload(file: File, sessionId: string, signal: AbortSignal)`、`generate(body: MediaGenerationProbeRequest, signal)`、`asset(id,signal)`、`content(id,signal): Promise<Blob>`、`runs/getRun`；字段与任务 1/10 一致，上传走原始 body，所有调用携带 Authorization，失败复用 ApiFailure。
- `MediaUpload({api,sessionId,maximumCount,disabled,onChange})` 保留浏览器 File 与上传状态；输出 ready 的 MediaAssetView 列表。图片数量/总量提前提示但服务器是最终校验者。失败只重试用户选择的文件；执行用过的 ID 再次发送前重传原 File。
- `MediaAttachment({api,asset})` 通过已认证 fetch 创建 Blob URL，图片用 img、音频用 audio controls；PCM 仅下载并说明。使用原始 File 的本地缩略图不得当作服务端生成成功证明。
- `MediaGenerator({token})` 独立会话、图片/对话音频模式、有效模型选择、格式及音色输入、发送/停止/状态/附件；收到成功 HTTP 结果前只显示正在生成，退出或切模式中止请求。
- Models 的 media 嵌套草稿与已有字段一起提交同一 expected_revision；缺表禁用媒体编辑但普通字段可保存，409 保留输入。Overview 显示真实媒体后端和初始化状态，Chat 增加媒体模式并卸载离开的模式。

- [x] **Step 1:** 写超过数量/总字节、失败重试、凭据头、Blob URL 撤销、过期文件、PCM 下载、CAS 冲突保留能力选择测试。
  ```typescript
  expect(uploadRequest.headers.Authorization).toBe(`Bearer ${token}`);
  expect(revokeObjectURL).toHaveBeenCalledWith(createdUrl);
  expect(screen.getByText('附件已过期')).toBeVisible();
  expect(modelPatch.expected_revision).toBe(3);
  ```
- [x] **Step 2:** 运行三个新组件测试与 `src/pages/Models.test.tsx src/pages/Chat.test.tsx`，确认 RED。
- [x] **Step 3:** 实现 API/组件/模型编辑与模式切换，采用项目现有样式，新增函数和涉及状态变化的逻辑使用中文注释；不重新设计整站。
- [x] **Step 4:** 重跑对应前端测试并执行 `npm --prefix web run typecheck`；检查 Blob 完成时组件已经卸载仍会撤销 URL，不仅覆盖正常卸载。
- [x] **Step 5:** 记录 UI 状态、注释审查与类型检查结果。

### Task 12: Agent 图片输入、附件事件与媒体运行记录界面

**Files:** 修改 `web/src/{agent-types,agent-api,agent-stream}.ts`、`web/src/components/{AgentChat,AgentRunDetail}.tsx`、`web/src/pages/Sessions.tsx`；新增 `web/src/pages/MediaRuns.tsx` 及测试，扩展 `web/src/agent-stream.test.ts`、`web/src/components/AgentChat.test.tsx`、`web/src/components/AgentRunDetail.test.tsx`。

**Interfaces:**
- Agent API 请求增加 attachment_ids；前端历史仍保存纯文字，只在本次消息附图，界面显示“图片仅用于本次提问，继续识图请重新选择”。纯图片发送启用，附件上传中禁止开始。
- streamAgent 接受 artifact 事件，验证 asset ID、purpose=output、status=ready、类型、字节和相同 execution_id/序号；保留 256 KiB 帧与 4 MiB 总流限制、done 后 EOF 检查，不接受事件中图片数据或任意下载地址。
- AgentChat 每轮只重置文字草稿，不清空已产生附件；失败/取消保留产物并显示“工具已生成，回复未完成”，不把该轮加入成功历史。切页/换 Agent/新会话/退出取消上传、执行和预览请求。
- AgentRunDetail 从附件元数据获取受认证内容，过期单项失败不阻止展示其他步骤。MediaRuns 使用任务 10 分页接口，Sessions 增加媒体记录模式；不提供自动重跑。

- [x] **Step 1:** 写 artifact 后 error、artifact 后缺 done、伪附件事件、半途切页、生成中切 Agent、运行记录到期及后台 pending fetch 完成测试。
  ```typescript
  expect(successHistory).toHaveLength(0); // 附件已生成但回复取消
  expect(screen.getByText('工具已生成，回复未完成')).toBeVisible();
  expect(screen.getByRole('img')).toBeVisible();
  expect(abortSignal.aborted).toBe(true);
  ```
- [x] **Step 2:** 运行 Agent 流、对话、详情与 MediaRuns 测试，确认新行为 RED。
- [x] **Step 3:** 接入已有上传/预览组件和事件字段，成功仅在有效 done 与 EOF 后提交，所有其他终止状态保留准确提示。
- [x] **Step 4:** 重跑相应测试及 TypeScript 检查，覆盖同时使用同步和流式执行的相同附件语义。
- [x] **Step 5:** 记录失败与取消后的 UI 行为、资源释放和无自动重传历史图片的结果。

### Task 13: 跨层离线验收与真实浏览器验证

**Files:** 新增 `tests/media/test_end_to_end.py`、`tests/media/test_lifecycle.py`；复用 `tests/admin/test_media_disconnect.py`；本地验收脚本及记录放 `.superpowers/sdd/2026-10-12-agentbot-phase-four-a/`，已检查的截图放 `docs/migration/phase-four-a-*.png`。

**Interfaces:**
- 组装真实 AgentService/Engine、目录/记录/媒体文件服务及 FastAPI，仅数据库、供应商、QQ Bot、Comfy/TTS 外部边界使用替身；QQ 链路使用现有队列和副作用收集逻辑。
- `JavaMustNotBeCalled` 对全部 Java 方法抛错，证明 Python 图片理解、直接生成和本地媒体工具没有隐式依赖；另用 Java spy 验证开关回退。
- 本地 HTTP/ASGI 服务控制“提交成功后断开”“替换文件后 DB 失败”“文字最后一轮失败”“清理到期时读取中”等时序，验证实际提交次数和终态；不人为把队列副作用替换成立即成功。

- [x] **Step 1:** 写 `test_qq_start_delivery_failure_never_submits_image`、`test_stopping_agent_keeps_queued_qq_image`、`test_disconnect_after_generation_keeps_ready_asset`、`test_refresh_waits_for_file_and_database_cleanup` 的端到端断言。
  ```python
  assert comfy_submissions_after_failed_start == 0
  assert queued_image_status_after_agent_stop == 'queued'
  assert generated_media_count == 1
  assert runtime_leases_after_disconnect == 0
  assert files_deleted_while_in_use == []
  ```
- [x] **Step 2:** 运行新增跨层测试，若揭示失败，按根因修复对应任务，保留回归证据；测试最初因真实缺陷变红时不得弱化断言。
- [x] **Step 3:** 启动当前任务专属本地验收服务与浏览器，使用有效本地测试图片/WAV，完成上传识图、Comfy 出图、TTS 播放、直接媒体、停止及历史过期流程；验证未登录和错误输入。
- [x] **Step 4:** 在桌面和 390px 宽度检查无页面横向溢出、按钮可操作、音频可播放、退出取消与 Blob 释放，保存并实际查看截图；区分应用默认上下文错误和浏览器扩展上下文错误。
- [x] **Step 5:** 关闭本任务拥有的进程/端口，写入验收账本；不关闭用户浏览器或其他服务。

### Task 14: 中文注释复核、回退与 0.4.0 交付

**Files:** 修改 `pyproject.toml`、`uv.lock`、`src/agent_bot/__init__.py`、`web/package.json`、`web/package-lock.json`、`tools/check_package.py`、`README.md`、`AGENTS.md`、`docs/migration/source-manifest.json`；新增 `docs/migration/phase-four-a.md`、`tests/media/test_release.py`；更新本设计/计划状态，构建资源只由工具产生。

**Interfaces:**
- wheel 增加新媒体模块、媒体初始化 SQL 和中文注释增量 SQL，原两份 SQL 继续打包；检查禁止包含上传文件、data/media、TTS 文件、.env、日志、数据库数据或本地验收材料。
- README/迁移说明列出四份 SQL 的用途和顺序（首次建库不需再执行旧表补注释脚本）、两个后端开关组合、模型能力、启动媒体目录、保存期/配额、上传单次使用、PCM 下载限制、未迁移能力及回退步骤。
- 用保留的 0.3.0 wheel 隔离环境验证恢复的配置快照去掉新增 `dispatch_chat_media_backend` 后可解析，原密钥/工具/预算不变；启动环境变量目录不属于 MySQL 配置。新增表和文件不自动删除。
- 代码注释审查以本阶段改动清单为边界：新/改 Python 模块、类、函数与 TS 组件/函数有中文说明；注释和实际取消/授权语义一致。SQL 静态结构对比及中文 COMMENT 检查必须通过。

- [x] **Step 1:** 写发布资源/注释 SQL/导入无副作用测试，运行 `tests/media/test_release.py` 看到新资源未纳入的 RED；记录旧 0.3.0 包摘要，避免覆盖。
- [x] **Step 2:** 完成发布资源和版本 0.4.0 更新、配置/迁移文档和注释复核；来源清单只更新 agentBot 改编文件摘要，不改原项目来源摘要。
- [x] **Step 3:** 执行最终验证，逐命令核对退出码；失败修复后只重跑相关范围，再执行最终完整回归。
  ```powershell
  uv lock --check
  uv run ruff check .
  uv run pytest -m "not e2e" -k "not database_onebot_token_reaches_adapter_without_log_disclosure" --allow-hosts=127.0.0.1,::1
  npm --prefix web ci
  npm --prefix web run typecheck
  npm --prefix web test -- --run
  npm --prefix web run build
  uv build
  uv run python tools/check_package.py dist/agent_bot-0.4.0-py3-none-any.whl
  ```
  预期全部退出码 0、Python 分支覆盖率至少 80%、前端构建与包资源检查通过；命令分别执行或显式保存退出码，禁止 PowerShell 后续命令掩盖失败。
- [x] **Step 4:** 在独立临时环境安装最终 wheel，执行两个 CLI help、导入检查、四份 SQL/UI 资源读取及 0.3.0 回退验证；复核旧源码和旧 wheel 摘要，生成新 wheel/sdist SHA256。
- [x] **Step 5:** 将实测通过数、覆盖率、截图、产物链接、未执行真实 MySQL DDL/QQ/付费模型的限制写入迁移记录；完成后再勾选计划、更新设计状态并向用户交付。

## 自审与设计覆盖

| 设计要求 | 落地任务 |
| --- | --- |
| 目标、范围、代码与 SQL 中文注释 | 全局约束、1、14 |
| 模型能力和显式路由 | 2、5、6、9 |
| 图片输入、限额、文件权限与保留期 | 1、3、4、5、10 |
| 三协议图片与直接媒体生成 | 5、6 |
| QQ 工具/任务及管理工具授权 | 7、8、9、13 |
| 取消、部分成果、记录与热刷新 | 4、6、8、9、10、12、13 |
| SQL 初始/升级/缺表/回退 | 1、2、14 |
| GUI 上传/预览/播放/停止/移动端 | 11、12、13 |
| 离线验证、来源保全与安装包 | 13、14 |

接口自审结论：MediaOwner 仅表示入口与会话，MediaScope 增加执行归属；客户端只传 session_id/attachment_ids。ModelImage 与 ValidatedImage 的字节不经过 API/事件序列化。媒体权限通过运行时和预设交集传递，模型能力使用模型 revision，同一请求不会重新解析新配置。上文五项 Review Focus 均有明确测试归属。

本计划细化了设计未命名的内部接口、上传接收并发/超时和单次附件绑定规则，未扩展视频、语音识别、通用任务或代码执行范围。没有产品代码、依赖安装、SQL 执行或发布动作因计划编写而发生。
