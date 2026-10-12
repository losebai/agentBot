# agentBot 第二阶段 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking. 沿用已确认的当前聊天顺序执行方式，不委派子代理；本计划已获审阅并实施完成。

**Goal:** 实现可由管理界面配置的 Python 普通 AI 服务，覆盖供应商、模型、同步/流式文本调用和会话记录，并保留未迁移能力的显式 Java 路由。

**Architecture:** 在现有服务内新增 `agent_bot.ai`，通过运行时持有的路由客户端接入 Chat，普通 Python 请求直接调用业务服务。三种供应商协议采用独立适配器，MySQL 新表保存配置与记录；管理 API 和 React 界面继续同源访问。

**Tech Stack:** 沿用 Python >=3.10,<4.0、uv、FastAPI、Pydantic、HTTPX、PyMySQL、NoneBot、Redis；React、TypeScript、Ant Design、Vite；pytest/NoneBug、Ruff、Vitest。不引入 ORM、Agent 框架或新的模型 SDK。

**Spec:** [2026-10-12-agentbot-phase-two-design.md](../specs/2026-10-12-agentbot-phase-two-design.md)，用户于 2026-10-12 回复“继续”确认。

## Global Constraints

- 目标项目 `D:/items/web/agentBot`；旧 `dispatch-sdk`、`bai-web` 只读，不改用户配置、不读取或复制旧密钥。
- 沿用当前聊天顺序实施，不新建聊天、不委派子代理、不创建 Git 仓库、不提交或推送。
- 保留 Python 3.10 语法兼容，开发与验证使用本项目 Python 3.12 环境；发布版本为 0.2.0。
- 配置字段 `dispatch_chat_plain_ai_backend` 取值 `java` 或 `python`，默认 `java`；无模型、协议或后端自动回退，无付费调用自动重试。
- Agent、联网、MCP、Laya、图片/视频、媒体生成和任务仍走明确的 Java 路由；普通 Python 调用必须经过自己的目录校验。
- 仅新增 `agent_bot_ai_provider`、`agent_bot_ai_model`、`agent_bot_ai_session`；不修改 Java 表和第一阶段 `ai_model` 兼容目录，不执行 DROP。
- 管理 API 全部鉴权，token 仅留浏览器内存；供应商凭据只保存 `AGENT_BOT_PROVIDER_` 环境变量引用，不回传原文。
- 内部文本至多 100 条消息、单条 32000 字符、总文本 120000 字符，末条为 user；模型别名至多 200 字符，会话/请求 ID 至多 256 字符。
- 管理对话保留当前输入 2000 字符、历史 12000 字符限制；单页查询至多 100 条。
- 请求总预算至多 300 秒；单流事件至多 256 KiB、累计回复至多 1 MiB；成功和失败终结事件互斥。
- 模型成功但记录失败仍交付回复，明确 `record_saved=false`；取消不进入成功历史。
- 单进程机器人；配置刷新和退出遵守现有运行时租约与数据库池所有权。
- 使用 Fake 数据库、HTTP Mock 和模拟浏览器流，禁止真实 QQ、模型、生产 MySQL/Redis/MCP 调用；保留原 80% 覆盖率门槛。

## Review Focus

- Python 模型别名带斜杠或只与另一别名大小写不同：原样选择上游 ID，不错误拆分或让 MySQL 默认排序规则合并；任务 2、4、7 验证。
- 流中中文被截断在 UTF-8 字节中、最后断流、停止与 done 同时发生：无乱码、不伪造完成、不把取消结果加入历史；任务 3、6、8、10 验证。
- 流正在读取时模型被禁用或配置切换：当前调用用原快照并保有租约，后续调用采用新配置；任务 2、7、8 验证。
- 模型已成功但记录失败、超时或取消时数据库事务已经开始：保留结果，标明未保存，等待有界事务收尾再释放资源；任务 6 验证。
- 原记忆压缩固定设置 `enableThinking=false`：不支持此选项的协议明确拒绝，压缩失败保留旧记忆；不能偷偷丢参数或转 Java；任务 5、7 验证。

## 文件职责与共用类型

| 位置 | 职责 |
| --- | --- |
| `src/agent_bot/ai/contracts.py`、`errors.py` | 普通请求、配置、结果、用量、事件及稳定错误 |
| `src/agent_bot/ai/credentials.py` | 本项目环境凭据引用解析，无变量插值 |
| `src/agent_bot/ai/catalog.py`、`sessions.py` | 异步接口包装现有同步数据库池、事务、目录与记录 |
| `src/agent_bot/ai/providers/{base,transport,parsing}.py` | 适配器接口、有界 HTTP 读取与 SSE/NDJSON 分帧 |
| `src/agent_bot/ai/providers/{responses,chat_completions,ollama}.py` | 三种显式协议的请求映射和结果解析 |
| `src/agent_bot/ai/{service,routing,context}.py` | 预算、记录、能力路由、QQ 来源提示兼容 |
| `src/agent_bot/admin/{ai_routes,errors}.py` | AI 管理路由及共用安全错误映射 |
| `web/src/{ai-types,ai-api,stream}.ts`、`pages/` | 类型化管理操作、流解析与真实功能页面 |
| `sql/agent_bot_ai_init.sql` | 新表幂等初始化及约束 |
| `tests/ai/`、`tests/admin/`、`web/src/*.test.*` | 离线行为、路由、取消和界面验证 |
| `docs/migration/phase-two.md` | 实际迁移矩阵、配置映射、验证及回退证据 |

Pydantic 请求/公开 DTO 采用 `extra='forbid'`、隐藏验证输入、字段长度和有限浮点数验证。`ProviderView`/`ModelView` 带 `revision`；patch 为类型化可省略字段加 `expected_revision`，省略与显式 null 分开。`Page[T]` 使用 Python 3.10 可用的 Generic，包含 `items` 与 `next_cursor`。

`ProviderProtocol = Literal['openai-responses','openai-chat','ollama']`。`ProviderCreate`/`ProviderView` 的业务字段为 name、protocol、base_url、credential_ref、enabled、description，View 再加 id/revision/credential_available；`ProviderUpdate` 为可省略业务字段加 expected_revision。`ModelCreate`/`ModelView` 的业务字段为 alias、upstream_model、provider_id、enabled，View 再加 id/revision；`ModelUpdate` 对应部分字段加 expected_revision。`VersionDelete` 仅含 expected_revision。`ResolvedModel` 为 frozen 的 ModelView 与 ProviderView 快照，不存明文密钥。`CatalogReadiness` 包含 initialized、model_available、provider_available、credential_available、code；不探测上游网络。

`TextRequest` 包含 `model`、`session_id`、`request_id`、`prompt`、`messages: tuple[ChatMessage, ...]`、`options: LlmRequestOptions`、`timeout_seconds`。`prompt` 用于记录；非空 messages 已包含当前输入，不能再次追加。`ProviderCall` 包含该请求、`ResolvedModel`、`credential: SecretStr | None`，秘密字段不进入 repr/公开序列化。`TokenUsage` 三个计数均可为 null。

`ProviderResult` 包含正文、实际模型、完成原因、用量；`ProviderDelta` 为 content 或 done，携带新增正文或最终元数据。`AiCompletion` 再增加执行 ID、请求 ID、耗时、记录保存状态。`AiEvent` 为 start/delta/done/error 的判别联合，错误使用安全 code/message，不传原始异常。

## Task 1: 类型契约、错误与凭据引用

**Files:** 创建 `src/agent_bot/ai/{__init__,contracts,errors,credentials}.py`、`tests/ai/{__init__,conftest,test_contracts,test_credentials}.py`。

**Interfaces:** `CredentialResolver(env_file: Path | None = None, environ: Mapping[str, str] | None = None).resolve(reference: str | None) -> SecretStr | None`，`is_available(reference: str | None) -> bool`。定义上文所有 DTO，以及 `SessionWrite`、`SessionSummary`、`SessionDetail`、`CatalogReadiness`。`AiError(code: str, *, status_code: int)` 只保存安全分类，不拼接上游正文。

- [x] 写失败测试 `test_text_bounds_and_last_user`，断言 101 条、32001 字符、总计 120001 字符或最后为 assistant 拒绝；100 条、32000 字符和总计 120000 字符的合法组合通过。测试无限/NaN 采样参数拒绝，空值保持上游默认。
- [x] 写 `test_credential_scope_and_precedence`：同名系统环境覆盖 `.env`，`.env` 的 `${NAME}` 不插值；非 `AGENT_BOT_PROVIDER_` 引用拒绝，缺失引用为可分类错误，null 为无凭据；序列化与 repr 不出现测试密钥。

  ```python
  def test_credential_scope_and_precedence(tmp_path):
      path = tmp_path / "offline.env"
      path.write_text("AGENT_BOT_PROVIDER_TEST_KEY=file-value\n", encoding="utf-8")
      resolver = CredentialResolver(path, {"AGENT_BOT_PROVIDER_TEST_KEY": "env-value"})
      assert resolver.resolve("AGENT_BOT_PROVIDER_TEST_KEY").get_secret_value() == "env-value"
      assert resolver.resolve(None) is None
  ```
- [x] 运行 `uv run pytest tests/ai/test_contracts.py tests/ai/test_credentials.py --no-cov`，确认预期导入/行为失败。
- [x] 实现上述契约和解析器；ProviderCreate 名称 1..100、协议三选一、HTTP(S) 根地址、enabled 默认 false，ModelCreate 别名及 upstream_model 1..200、provider_id 为正整数、enabled 默认 false。拒绝 URL 用户信息/query/fragment/控制字符以及重复端点路径。
- [x] 重跑相同命令，确认全部通过；记录任务结果，不提交。

## Task 2: MySQL 模型目录与会话仓库

**Files:** 创建 `sql/agent_bot_ai_init.sql`、`src/agent_bot/ai/{catalog,sessions}.py`、`tests/ai/{fake_database,test_catalog,test_sessions}.py`。

**Interfaces:** `AiCatalog(database: DatabaseParams, *, connect: Callable[..., Any])` 提供异步方法：`readiness(model: str | None = None) -> CatalogReadiness`、`list_providers(limit: int = 50, cursor: str | None = None) -> Page[ProviderView]`、`create_provider(body: ProviderCreate) -> ProviderView`、`update_provider(id: int, body: ProviderUpdate) -> ProviderView`、`delete_provider(id: int, expected_revision: int) -> None`、`list_models(limit: int = 50, cursor: str | None = None) -> Page[ModelView]`、`create_model(body: ModelCreate) -> ModelView`、`update_model(id: int, body: ModelUpdate) -> ModelView`、`delete_model(id: int, expected_revision: int) -> None`、`resolve(model: str) -> ResolvedModel`。`AiSessionStore(database: DatabaseParams, *, connect: Callable[..., Any])` 提供异步方法 `save(record: SessionWrite) -> None`、`list_records(*, session_id: str | None, status: str | None, limit: int, cursor: str | None) -> Page[SessionSummary]`、`get_record(execution_id: str) -> SessionDetail`。

目录仓库不读取环境：它返回的 credential_available 由任务 6 的服务通过 `CredentialResolver.is_available` 填入，再由任务 8 输出；仓库的 readiness 仅报告数据库与模型/供应商状态。`AiService.credential_available(reference: str | None) -> bool` 是管理路由填充供应商列表/修改结果的公开边界。

- [x] 写 `test_catalog_cas_and_model_reference`：更新 revision=1 成功到 2，重复 revision=1 返回冲突且数据不变；禁用/缺失供应商阻止 resolve；删除仍有模型引用的供应商拒绝；删除禁用模型后旧记录快照仍可读。

  ```python
  async def test_catalog_cas_and_model_reference(catalog, enabled_provider):
      patch = ProviderUpdate(expected_revision=1, enabled=False)
      assert (await catalog.update_provider(enabled_provider.id, patch)).revision == 2
      with pytest.raises(AiError) as caught:
          await catalog.update_provider(enabled_provider.id, patch)
      assert caught.value.status_code == 409
  ```
- [x] 写 `test_case_sensitive_alias_and_snapshot`：`Qwen/X` 与 `qwen/x` 独立，resolve 返回精确 upstream_model；取得快照后更新模型不改变快照。写 `test_record_paging_and_redaction`：相同时间按执行 ID 分页无重复遗漏，limit=101 拒绝，列表无正文，详情无凭据/完整历史。
- [x] 运行 `uv run pytest tests/ai/test_catalog.py tests/ai/test_sessions.py --no-cov`，确认失败。
- [x] 实现参数化 SQL、行级 CAS 和引用事务。新表用 InnoDB/utf8mb4，模型别名采用二进制比较；供应商/模型 ID 自增、revision 初值 1，会话执行 ID UUID hex 主键，时间使用 UTC。通过池在工作线程读取，写入沿用 `finish_database_write`；禁止业务仓库关闭共享池。缺表/列不匹配明确报未初始化/schema 错误，无自动建表或内存兜底。
- [x] 重跑测试，增加 `test_cancelled_write_finishes_before_pool_release` 和 SQL 脚本无 DROP/不引用旧表断言，确认通过。

## Task 3: 有界 HTTP、SSE 与 NDJSON 解析

**Files:** 创建 `src/agent_bot/ai/providers/{__init__,base,transport,parsing}.py`、`tests/ai/{test_transport,test_parsing}.py`。

**Interfaces:** `TextProvider` Protocol：`validate_options(options: LlmRequestOptions) -> None`、`async complete(call: ProviderCall) -> ProviderResult`、`stream(call: ProviderCall) -> AsyncIterator[ProviderDelta]`、`async aclose() -> None`。`ProviderTransport(transport: httpx.AsyncBaseTransport | None = None)` 持有 HTTPX AsyncClient，提供 `async read_json(url: str, *, payload: JsonValue, headers: Mapping[str, str], timeout_seconds: float, maximum_bytes: int = 1048576) -> JsonValue`、`iter_bytes(url: str, *, payload: JsonValue, headers: Mapping[str, str], timeout_seconds: float) -> AsyncIterator[bytes]` 和 `async aclose() -> None`；两种请求均为 POST，固定不跟重定向、不自动重试。`parse_sse(chunks: AsyncIterable[bytes], *, maximum_event_bytes: int = 262144) -> AsyncIterator[str]` 与 `parse_ndjson(chunks: AsyncIterable[bytes], *, maximum_line_bytes: int = 262144) -> AsyncIterator[str]`。

- [x] 写 `test_utf8_sse_split_and_multiline`，用不同分块位置覆盖中文、CRLF、注释和多行 data，断言文本无乱码且帧完整；NDJSON 允许空行、正确处理最后完整行。

  ```python
  async def test_utf8_sse_split_and_multiline():
      raw = "data: 你好\r\ndata: 世界\r\n\r\n".encode()
      async def chunks():
          for byte in raw:
              yield bytes([byte])
      assert [item async for item in parse_sse(chunks())] == ["你好\n世界"]
  ```
- [x] 写 `test_event_limit_and_body_limit`，断言 262145 字节事件与 1048577 字节响应拒绝；写 `test_http_error_is_safe_and_no_redirect`，断言密钥/上游错误体不进入异常与日志，3xx 不跟随、429 保留分类、取消时 response.aclose 被调用。
- [x] 运行 `uv run pytest tests/ai/test_transport.py tests/ai/test_parsing.py --no-cov`，确认失败。
- [x] 使用增量 UTF-8 解码器和有界帧缓冲实现解析；HTTP read timeout 每次取现有剩余预算，同时由业务总计时限制慢流。同步 JSON 最大 1 MiB，连接和 HTTP 连接池有界；异常只转为稳定 AiError。
- [x] 重跑测试，覆盖非法 UTF-8、未终结帧及取消资源释放，确认通过。

## Task 4: Responses 普通文本适配器

**Files:** 创建 `src/agent_bot/ai/providers/responses.py`、`tests/ai/test_responses.py`。

**Interfaces:** `ResponsesProvider(transport: ProviderTransport)` 实现 TextProvider；根地址后追加 `/responses`，不读 SDK 属性或猜测其他协议。

- [x] 写 `test_responses_payload_and_sync_text`：断言精确 upstream_model、input 的文本角色、stream=false、`store=false`、max_tokens 映射 `max_output_tokens`；从 output 的 assistant/output_text 获取回复，保持空行，usage 缺失为 null。带斜杠模型不重复拼前缀。

  关键断言：`assert payload['model'] == 'Qwen/Qwen3.5-9B'`、`assert payload['store'] is False`、`assert payload['max_output_tokens'] == call.request.options.max_tokens`、`assert result.content == '第一段\n\n第二段'`。
- [x] 写 `test_responses_stream_terminal`：output_text.delta 逐次返回，response.completed 产生唯一终结；failed/incomplete/error、工具输出、仅 reasoning、无完成信号均不能成功。done 中的完整正文不能再次追加已发 delta。
- [x] 运行 `uv run pytest tests/ai/test_responses.py --no-cov`，确认失败。
- [x] 实现同步及流式解析，仅接受正常完成的纯文本结果。支持 max_tokens/temperature/top_p；其余非空选项明确 AI_OPTION_UNSUPPORTED，包括 enable_thinking，不猜测 reasoning effort。上游拒绝参数仍按拒绝分类报告，不自动重发。
- [x] 重跑测试，确认 payload、真正逐段输出、终结与错误脱敏通过。

## Task 5: 兼容 Chat Completions 与 Ollama

**Files:** 创建 `src/agent_bot/ai/providers/{chat_completions,ollama}.py`、`tests/ai/{test_chat_completions,test_ollama}.py`。

**Interfaces:** `ChatCompletionsProvider(transport)`、`OllamaProvider(transport)` 均实现 TextProvider。协议选择只由 ResolvedModel.provider.protocol 决定。

- [x] 写 `test_chat_payload_and_finish`：POST `/chat/completions`，max_tokens 映射 `max_completion_tokens`，stream=false；同步取第一 choice 的 assistant content，流式按 delta 增量处理，finish_reason 和 `[DONE]` 明确完成，保留可选 token 用量。仅工具/思考或空正文拒绝。
- [x] 写 `test_ollama_payload_and_done`：POST `/api/chat`，max_tokens→num_predict、top_p/top_k/min_p/temperature→options、repetition_penalty→repeat_penalty、stop→options.stop、enable_thinking→think；同步显式 stream=false，流以 done=true 完成，统计 prompt_eval_count/eval_count。未知非空选项拒绝。

  关键断言：`assert chat_payload['max_completion_tokens'] == 123`、`assert ollama_payload['options']['num_predict'] == 123`、`assert ollama_payload['think'] is False`；两个测试请求都将 max_tokens 明确设为 123。
- [x] 运行 `uv run pytest tests/ai/test_chat_completions.py tests/ai/test_ollama.py --no-cov`，确认失败。
- [x] 实现两个适配器：Chat Completions 支持 max_tokens/temperature/top_p/presence_penalty/frequency_penalty/stop；Ollama 支持测试列出的字段，不把 presence/frequency penalty 猜成其他参数。两者都禁止工具执行、协议自动探测和重试。
- [x] 重跑测试，增加 `test_unsupported_thinking_is_not_silently_ignored`；记录兼容服务不支持 max_completion_tokens 时需要选择/扩展显式协议，不能运行中偷偷改参数并重发。

## Task 6: 普通对话服务、预算与记录

**Files:** 创建 `src/agent_bot/ai/service.py`、`tests/ai/test_service.py`。

**Interfaces:** `AiService(catalog: AiCatalog, sessions: AiSessionStore, credentials: CredentialResolver, providers: Mapping[ProviderProtocol, TextProvider], *, max_concurrent_requests: int = 8)`：`async complete(request: TextRequest) -> AiCompletion`、`stream(request: TextRequest) -> AsyncIterator[AiEvent]`、`async readiness(model: str | None = None) -> CatalogReadiness`、`async aclose() -> None`。

- [x] 写 `test_complete_records_without_losing_result`：正常结果与 SessionWrite 相同；save 失败仍返回原文且 record_saved=false，日志只有错误类型/请求标识；failed/cancelled 记录状态正确，无完整历史、thinking 和凭据。

  ```python
  async def test_complete_records_without_losing_result(ai_service, text_request):
      ai_service.sessions.save.side_effect = RuntimeError("offline database failure")
      result = await ai_service.complete(text_request)
      assert result.content == "已生成的回复"
      assert result.record_saved is False
  ```

  `ai_service` 测试 fixture 使用真实 AiService、可控仓库 Fake 和返回该正文的 HTTP Mock，不 Mock complete 方法本身。
- [x] 写 `test_deadline_is_total_and_nested_budget_shrinks`：目录慢读与持续小分块共享同一预算，最多 300 秒、更小父级预算不重置。写 `test_stream_cancel_and_terminal_exclusion`：delta 后取消关闭 provider 流、保存 cancelled、无成功 done；正常完成仅一个 done，失败仅一个 error。
- [x] 运行 `uv run pytest tests/ai/test_service.py --no-cov`，确认失败。
- [x] 组装不可变 ProviderCall，按显式协议验证 options，再调用上游；用现有 request_deadline/remaining_timeout 与 asyncio.wait_for 控制 Python 3.10 可用的预算。每运行时最多 8 个活跃普通请求，满额返回可分类忙碌错误，不建立无限排队。正文累计上限 1 MiB，空白/工具/仅思考均拒绝。
- [x] 实现剩余预算内尽力记录、已开始事务的有界收尾及上游关闭；重跑测试，覆盖取消与数据库写并发、记录超时、未初始化、关闭幂等和第 9 个并发请求拒绝。

## Task 7: Chat 能力路由、模型预检与运行时

**Files:** 创建 `src/agent_bot/ai/{routing,context}.py`、`tests/ai/{test_routing,test_context,test_runtime}.py`；修改 `src/dispatch_chat/config.py`、`model_guard.py`、`services/chat/{contracts,service}.py`、`plugin/runtime.py`；更新相关 `tests/unit/{test_model_guard,test_runtime_reload,test_group_context}.py`。

**Interfaces:** 在 routing.py 定义 frozen `AiRouteRequest`，包含有效 agent/web/tools/MCP/Laya 和图片/视频能力；`select_backend(request: AiRouteRequest, plain_backend: Literal['java','python']) -> Literal['java','python']`。`RoutingAiClient(java_client: DispatchAiClient, python_service: AiService, *, plain_backend: Literal['java','python'], default_options: LlmRequestOptions, mcp_services: Sequence[str], laya_enabled: bool)` 保留原 complete/generate_media/decide_auto_speak/pending_jobs/acknowledge_job/aclose 签名与默认值。`RoutedModelGuard(java_guard: ModelGuard, catalog: AiCatalog, *, plain_backend: Literal['java','python'])` 提供 `async ensure_enabled(model_id: str, *, capability: Literal['ordinary-text','java'] = 'java') -> None`；原 ModelGuard 接受同一可选参数，在原实现中仍查 Java 目录。`format_qq_context(messages: Sequence[ChatMessage], context: ChatContext | None) -> tuple[ChatMessage, ...]`。

RoutingAiClient 构造器还明确接收 `timeout_seconds: float`，来自运行时 dispatch_chat_timeout_seconds 并限制到 300；公开只读 python_service 属性供管理边界使用。request_options 为空时使用 default_options，非空时沿用原客户端整组覆盖规则，不擅自改为字段混合。

- [x] 写 `test_plain_python_never_calls_java`、`test_tools_media_and_mcp_remain_java`、`test_python_failure_does_not_fallback`，断言每类只访问对应后端，普通预检读 Python 目录、未迁移请求预检不读 Python 表。覆盖有效 agent=false 时不误附加配置里的 MCP。

  关键断言：`python_service.complete.assert_awaited_once()`、`java_client.complete.assert_not_awaited()`；Python 故障测试在捕获原错误之后继续执行后一条断言。
- [x] 写 `test_context_preserves_qq_identity_guardrail`：Python 当前 user 消息保留 Java ChatContextFormatter 的来源字段与不可信元数据提醒，只处理当前一条，不重复 prompt，非法名称拒绝，构造后仍满足文本边界。写 `test_compression_option_rejection_preserves_memory`：不支持 thinking 开关时不调用任何上游、不改旧记忆、不转 Java。
- [x] 运行 `uv run pytest tests/ai/test_routing.py tests/ai/test_context.py tests/ai/test_runtime.py --no-cov`，确认失败。
- [x] 添加后端配置字段及中文说明；在 ChatService 已确定有效请求能力后通过共用分类执行预检。媒体预检保留 Java；路由客户端也执行最终校验，避免压缩等没有原预检的调用漏检。没有迁移后的 Java-only 类型注解或遗失原方法；不得用无条件 __getattr__ 吞掉边界错误。
- [x] 在 build_runtime_state 创建 Python 模块和路由客户端，旧池唯一所有权不变；Java 默认不查新表。重跑聚焦测试及 `uv run pytest tests/unit/test_model_guard.py tests/unit/test_runtime_reload.py tests/unit/test_client.py tests/unit/test_chat_agent_client.py tests/unit/test_group_context.py --no-cov`，断言热刷新等待 lease、旧客户端只关闭一次、独立进程导入无副作用。

## Task 8: AI 管理 API、就绪状态与流式生命周期

**Files:** 创建 `src/agent_bot/admin/{ai_routes,errors}.py`、`tests/admin/{test_ai_routes,test_chat_stream}.py`；修改 `admin/{models,runtime,chat,routes}.py`、`tests/admin/{conftest,test_routes,test_entrypoint}.py`。

**Interfaces:** `PluginRuntimeProvider.ai_service` 总是解析当前 RuntimeState 客户端的 Python 服务；`ChatProbe.complete(request, request_id) -> ChatResponse` 与 `stream(request, request_id) -> AsyncIterator[AiEvent]`。`install_ai_routes(router: APIRouter, runtime) -> None` 沿用 router 的鉴权依赖。`classify_api_error(error: Exception) -> tuple[int, str, str]` 被 JSON 与 SSE 共用。

路由为 `/api/v1/ai/providers` 和 `/ai/models` 的 GET/POST、`/{id}` PATCH/DELETE（DELETE 用有版本的类型化请求）；`/ai/sessions` GET 与 `/ai/sessions/{execution_id}` GET；`/chat/stream` POST。会话列表 query 包含 session_id/status/limit/cursor。ChatRequest 增加可选 model，仅 Python 可使用临时选择；ChatResponse 保留 content/request_id，增加可选 metadata。

- [x] 写 `test_ai_crud_auth_cas_and_safe_errors`：401 无密钥回显；合法创建/编辑/删除、409 并发冲突、引用冲突、503 未初始化、分页和404详情。输入错误不回显 body；状态只查本地目录/凭据，不访问供应商。

  关键断言：`assert response.status_code == 409`、`assert response.json()['code'] == 'AI_CONFIG_CONFLICT'`、`assert 'offline-provider-secret' not in response.text`；冲突是先更新一次后再提交原 expected_revision 构造。
- [x] 写 `test_chat_stream_asgi_disconnect`：在真实 ASGI send/receive 上驱动逐段响应与 http.disconnect，断言已产生正文后关闭上游、释放租约，且不是 HTTPX ASGITransport 聚合完整流后假定增量成功。Java 模式发流/选 Python 模型在响应开始前拒绝。
- [x] 运行 `uv run pytest tests/admin/test_ai_routes.py tests/admin/test_chat_stream.py --no-cov`，确认失败。
- [x] 添加路由、错误映射和 status 的普通/Agent/媒体后端、python_ai 初始化/可用状态；ready 保留机器人基础含义。管理员同步 Java 无临时模型仍保持旧行为；Python 同步/流式共享系统提示保护。SSE 用类型化事件 model_dump_json，不回传 ProviderCall。生成器持有整个 runtime lease，流开始后所有普通异常转安全 error，CancelledError 继续传播和清理。

  CRUD、状态及记录访问同样在 runtime.lease 内解析当前 AiService；AiService 公开 catalog/sessions 和 credential_available 边界，不能在 install_ai_routes 时捕获会被热刷新关闭的客户端。
- [x] 重跑聚焦测试及 `uv run pytest tests/admin --no-cov`；覆盖 body 64 KiB 限制、Unicode、更新期间流不混用资源、退出清理、旧健康/配置/OneBot/MCP 路由未被新异常处理器接管。

## Task 9: 供应商/模型管理与状态界面

**Files:** 创建 `web/src/{ai-types,ai-api}.ts`、`web/src/pages/{Providers,Models}.tsx` 及对应 `.test.tsx`；修改 `web/src/types.ts`、`web/src/api.ts`、`web/src/App.tsx`、`web/src/pages/Overview.tsx`、`web/src/styles.css`、`web/src/App.test.tsx`。

**Interfaces:** `AiApi(token: string)` 提供与任务 8 DTO 对应的 providers/models CRUD、sessions/getSession；token 仅构造参数与内存字段。错误沿用 ApiFailure；页面公开 props `{token: string}`。ServiceStatus 与 ChatResponse 的前后端字段一致。

- [x] 写 `Providers.test.tsx::conflict_keeps_form_and_secret_reference` 和 `Models.test.tsx::alias_and_upstream_are_distinct`：只提交修改字段、期望版本正确、409 保留编辑、凭据输入只有变量名、禁用模型不能选为普通对话、斜杠上游 ID 原样提交。

  关键断言：`expect(screen.getByLabelText('模型别名')).toHaveValue('local-qwen')`、`expect(screen.getByLabelText('上游模型 ID')).toHaveValue('Qwen/Qwen3.5-9B')`；API Mock 返回 409 后再次断言原编辑值保留。
- [x] 运行 `npm run test -- --run src/pages/Providers.test.tsx src/pages/Models.test.tsx`（工作目录 `web`），确认行为失败。
- [x] 实现列表/分页、新建、编辑、启用/禁用和受约束删除，未初始化明确显示 SQL 初始化说明；删除错误保留列表。供应商协议支持矩阵和根地址提示与后端一致，不伪造连接成功。不增加上游自动同步按钮。
- [x] 加入导航与真实普通后端/能力状态，保留 visited 页面挂载与退出清理；更新阶段文本为第二阶段。实际文件变更只使用精确路径，不创建同名错误后缀文件。
- [x] 运行上述测试、`npm run test -- --run src/App.test.tsx`、`npm run typecheck`，确认通过。

## Task 10: 浏览器流式对话、停止与会话记录

**Files:** 创建 `web/src/{stream,stream.test}.ts`、`web/src/pages/{Sessions,Sessions.test}.tsx`；修改 `web/src/api.ts`、`web/src/pages/{Chat,Chat.test}.tsx`、`web/src/App.tsx`。

**Interfaces:** `readChatEvents(response: Response, signal: AbortSignal) -> AsyncGenerator<AiEvent>`（TypeScript 异步生成器签名）；`ApiClient.streamChat(body: ChatRequest, signal: AbortSignal) -> AsyncGenerator<AiEvent>`；原 chat 方法增可选 model 与 AbortSignal，旧三参数调用保持有效。聊天 UI 单独保存完整成功历史与本次 partial reply。

- [x] 写 `stream.test.ts::split_utf8_and_terminal_validation`：ReadableStream 实际分多块推送，中文/多行 SSE 解析准确，非法/超限/终结缺失报错，收到 done 不重复追加最终正文。
- [x] 写 `Chat.test.tsx::stop_does_not_commit_history`、`logout_aborts_inflight_request`、`record_warning_keeps_answer`：停止保留部分回复、下一次请求不带取消轮次；done 已到后停止不能改变成功状态；退出触发 AbortController 且组件卸载后不写状态；同步和流式保存失败都保留答案。

  关键断言：`expect(nextRequest.messages).toEqual([])`、`expect(signal.aborted).toBe(true)`；分别来自首次取消后发送第二条消息与退出管理场景，不能将所有正常历史也一并清空。
- [x] 运行 `npm run test -- --run src/stream.test.ts src/pages/Chat.test.tsx src/pages/Sessions.test.tsx src/App.test.tsx`，确认预期失败。
- [x] 实现带鉴权 fetch 流、bounded TextDecoder 缓冲和 reader finally 清理；发送请求期间保持选定模型/后端，不静默重发。Python 模式显示模型选择、同步/流式和停止；Java 隐藏临时 Python 模型并明确流不可用。继续按完整轮次裁历史到 12000 字符。
- [x] 实现会话列表过滤/分页/按需详情，列表不请求正文，详情失败可重试读取而不是再调用模型。重跑测试与 `npm run typecheck`，覆盖空列表、取消、异常流和新会话清理。

## Task 11: 回归、迁移说明与 0.2.0 包验证

**Files:** 修改 `pyproject.toml`、`src/agent_bot/__init__.py`、`uv.lock`、`web/{package,package-lock}.json`、`README.md`、`.env.example`、`tools/check_package.py`；创建 `docs/migration/phase-two.md`、`tests/ai/test_release_resources.py`；按实际变化更新 `docs/migration/source-manifest.json` adaptations；保留第一阶段验证作为历史。

**Interfaces:** wheel 名称 `agent_bot-0.2.0-py3-none-any.whl`；原 agent-bot/dispatch-chat 入口不变。SQL 初始化脚本同时以 `agent_bot/resources/agent_bot_ai_init.sql` 包资源发布，来源是受审查的 `sql/agent_bot_ai_init.sql`，由构建 force-include 映射，不依赖旧目录。

- [x] 写 `test_release_sql_and_import_are_independent`：wheel 有新 AI 模块、SQL 和界面；无 .env/.venv/.idea/缓存/旧项目导入；SQL 无 DROP。更新包检查器并在打包前运行测试确认资源检查失败时确实能阻止交付。

  包检查器单元测试使用临时构造的 ZIP，证明缺少资源或夹带秘密会失败，不要求全量 pytest 开始之前已有 0.2.0 产物；实际 wheel 的资源检查和独立安装在下面 uv build 之后执行。

  关键断言：`assert 'agent_bot/ai/service.py' in names`、`assert 'agent_bot/resources/agent_bot_ai_init.sql' in names`、`assert all('.env' not in Path(name).parts for name in names)`；SQL 使用大小写无关的 DROP token 检查，不仅比较文件名。
- [x] 更新版本与锁文件，只更改实际需要的锁数据；README 说明手动 SQL、三协议根地址、环境引用、Python 普通切换、仍在 Java 的能力、内部压缩选项兼容限制、Chat Completions max_completion_tokens 兼容限制、记录正文保留策略。
- [x] 编写回退步骤：备份配置，切 java，等待新配置应用；回退旧二进制前从配置集合移除新字段并保留原 Java 模型/目录，不 DROP 新表。用 Fake Store 和第一阶段字段集合演练恢复读取；不操作真实库。
- [x] 运行以下完整检查，全部取得实际结果后才标记阶段完成：

  ```powershell
  uv lock --check
  uv run ruff check .
  uv run pytest -m 'not e2e' -k 'not database_onebot_token_reaches_adapter_without_log_disclosure' --allow-hosts=127.0.0.1,::1
  ```

  在 `web` 顺序运行 `npm run typecheck`、`npm run test -- --run`、`npm run build`；然后回项目根运行 `uv build` 与 `uv run python tools/check_package.py dist/agent_bot-0.2.0-py3-none-any.whl`。失败先定位、修改并重跑受影响检查，不降低覆盖率门槛。

- [x] 在 `.superpowers/sdd/2026-10-12-agentbot-phase-two/wheel-env` 创建独立安装环境，安装 wheel，检查模块/SQL/JSON/静态资源和两个 CLI --help，确认无需访问旧目录。浏览器检查生产界面和三新增页面，使用临时 QA Fake API；截图及测试报告保存于 `docs/migration/`，不能用 Fake 成功宣称真实联调。停止自己启动的服务/浏览器，作者复核规格、接口和秘密输出，记录实际测试数量、覆盖率、构建、未执行真实联调及限制。

## 顺序与完成记录

任务依赖为 1→2→3→4→5→6→7→8→9→10→11。每个任务先证明测试能失败，再实现并确认通过；现有测试的修改只反映新增能力边界，不删除回归断言。测试辅助仅构造 Fake/Mock，不连接运行环境。

实施前建立 `.superpowers/sdd/2026-10-12-agentbot-phase-two/progress.md`，记录当前任务、通过命令、实际失败与修复及范围调整，逐项勾选计划。没有 Git 时用该记录和迁移清单保留可审阅证据。

本计划的审批范围是第二阶段产品代码与离线验证。真实 QQ、付费模型、生产数据导入、外部部署仍不在本次执行范围。

## 计划自检

- 规格 1–5、11：任务 7、8、11 覆盖生命周期、路由、默认 Java、缺表和回退。
- 规格 6：任务 1、2、8、9 覆盖配置、凭据、约束与管理操作。
- 规格 7–8：任务 1、3–8、10 覆盖边界、协议、预算、流式、错误与取消。
- 规格 9–10：任务 2、6、8–10 覆盖记录、保存失败、临时模型及图形界面。
- 规格 12–13：任务 11 执行全量验证并准确报告后续阶段；不纳入外部 HTTP 兼容服务、原生 Anthropic 或模型自动同步。
- 上述五项 Review Focus 均分配实际测试；类型与方法名称在任务间一致；没有未定任务、未定义业务方法或实现代码正文。

状态：用户确认后已顺序实施完成；实际验证与限制见 docs/migration/phase-two.md。
