# agentBot Phase Three Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 本项目沿用当前会话直接执行；不创建子代理、不初始化 Git、不提交或推送。

**Goal:** 在 Python 服务中完成 Agent、Skill、MCP、受限工具调用与 GUI 执行观察，交付可离线验证和独立安装的 0.3.0。

**Architecture:** 新增 `agent_bot.agent` 执行模块，复用第二阶段模型目录、数据库池和管理鉴权。模型工具消息有独立类型，Chat 工具复用请求令牌授权，通用 MCP 使用锁定 SDK；统一执行内核同时服务同步、流式和 QQ。

**Tech Stack:** Python 3.12 开发、Python 3.10 语法兼容；FastAPI/Pydantic 2/MySQL；现有 HTTPX 0.28.1 模型客户端；锁定 MCP 2.3.0 的 HTTPX2 2.13.1 客户端；React/TypeScript/Vite、pytest/Vitest。

**Spec:** [已确认设计](../specs/2026-10-12-agentbot-phase-three-design.md)。该文档是行为和范围的依据。

状态：用户已确认并实施，14 项任务已完成；实际验证、实现调整及边界见第三阶段迁移记录和执行账本。

## Global Constraints

- 仅修改 `D:/items/web/agentBot`；旧 `dispatch-sdk` 与 `bai-web` 只读，保留原始摘要和用户修改。
- 不初始化 Git、不创建分支/提交/PR、不使用子代理；每任务结束保存验证记录到 `.superpowers/sdd/2026-10-12-agentbot-phase-three/progress.md`，替代技能中的提交步骤。
- 不连接真实 QQ、付费模型、生产数据库或外部 MCP；普通测试使用 Fake/Mock，仅协议测试允许本机 loopback。
- Agent 后端默认 java，与普通文本后端独立；第四阶段能力显式走 Java，不做故障回退。
- 最大 8 个活动 Agent、3 个通用 MCP 服务、20 个远端工具；工具轮次默认 3，可选 1–5，每轮最多 8 次工具调用、累计最多 40 次；额外一次禁用工具收尾，总预算不超过 300 秒。
- MCP 初始化最多 10 秒，单次配置 1–30 秒，发现最多 10 页；参数/schema 各最多 64 KiB，schema 深度 16；文本结果最多 32,000 字符、正文 1 MiB、事件帧 256 KiB。
- 输入最多 100 条、单条 32,000、合计 120,000 字符；角色 32,000、单 Skill 16,000、组装系统提示词 65,536 字符。
- `web_search` 查询 300 字符、5 个结果、上下文 10,000 字符；`read_url` 最多 3 次跳转、解压后 1 MiB、输出 32,000 字符。
- 工具令牌、凭据、模型内部推理和原始敏感工具正文不进入事件/记录/日志；有副作用的调用不自动重试。
- Python 完整回归保持 80% 分支覆盖率门槛；继续排除已有真实数据库破坏性测试，不降低门槛或删除原有断言。

## Review Focus

1. 混合请求同时携带 memory 与绘图/Laya：模型预检、实际路由和界面原因必须一致；任务 10 增加 `test_mixed_capabilities_keep_java_model_guard`。
2. 模型先产生文字再产生工具调用：文字只能是当前轮草稿，不能变成最终 QQ 回复或下一轮成功历史；任务 4/9/13 增加 `test_text_before_tool_call_is_not_final`。
3. MCP 的 SSE 消息 endpoint 与初始地址跨源、或发现循环游标：不得泄漏认证或无限读取；任务 5 增加 `test_sse_foreign_endpoint_never_receives_token` 与 `test_discovery_repeated_cursor_stops`。
4. 浏览器在工具已完成、结果尚未记录时断开：取消不能重做副作用，已完成状态和未知状态须区分；任务 8/9/11 增加 `test_disconnect_after_tool_side_effect_does_not_retry`。
5. GUI 目录正在编辑时对象被禁用、重命名或删除：在途请求使用原快照，新请求明确失败，CAS 冲突保留编辑内容；任务 2/3/12 增加对应快照和编辑冲突测试。

## 文件结构与依赖顺序

| 文件/目录 | 职责 | 任务 |
| --- | --- | --- |
| `src/agent_bot/agent/contracts.py`, `errors.py` | 目录、请求、消息、事件、记录类型与安全错误 | 1 |
| `agent/catalog.py`, `catalog_rows.py` | 目录 CRUD、CAS、关联与一致快照 | 2 |
| `agent/prompting.py`, `tools/policy.py`, `tools/schema.py` | 提示词、权限交集、schema 与参数校验 | 3 |
| `ai/providers/agent_*.py` | 三种协议的工具调用与流式解析 | 4 |
| `agent/mcp/client.py`, `http.py`, `discovery.py`, `credentials.py` | SDK 连接、网络边界、发现、环境凭据 | 5 |
| `agent/tools/chat.py` | 复用 Chat 授权执行 | 6 |
| `agent/tools/search.py`, `read_url.py`, `public_http.py` | 搜索、公开网页、安全连接 | 7 |
| `agent/records.py` | Run/Step 保存、过滤与分页 | 8 |
| `agent/engine.py`, `service.py`, `tools/runtime.py` | 执行循环、准备、资源所有权 | 9 |
| `ai/routing.py`, Chat 配置及运行时 | QQ 路由、预检、配置刷新 | 10 |
| `admin/agent_routes.py`, `agent_chat.py` | 鉴权 API、SSE 与记录 | 11 |
| `web/src/agent-types.ts`, `agent-api.ts`, 目录页面 | 类型化目录管理 | 12 |
| `web/src/agent-stream.ts`, Agent 对话与记录组件 | 执行步骤、停止与记录详情 | 13 |
| SQL、构建、迁移文档与包检查 | 发布、来源验证与回退 | 14 |

所有 `agent/…` 路径均位于 `src/agent_bot/` 下。新增 package 的 `__init__.py` 随拥有它的任务创建，禁止导入时启动插件、网络连接或数据库写入。任务 4–8 依赖任务 1–3，任务 9 合并其边界；这里是一个闭环功能的依赖顺序，不拆成互不兼容的独立产品。

## 共用验证约定

聚焦 Python 命令使用 `uv run pytest <本任务文件> --no-cov --allow-hosts=127.0.0.1,::1 -q`，预期最终退出码 0；首次 RED 必须因目标行为缺失失败，不接受夹具错误作为功能验证。最后任务运行完整覆盖率。

每任务的日志重定向到本计划的 `.superpowers/sdd` 目录并读取尾部；无新变化不重复全量测试。下列代码块为测试中的核心断言，不是可直接运行的完整测试模块；夹具的对象与方法在 Interfaces 或该步骤中定义。

### Task 1: 固定目录、工具消息与执行事件契约

**Files:** 新增 `src/agent_bot/agent/{__init__,contracts,errors}.py`；新增 `tests/agent/{__init__,test_contracts}.py`。

**Interfaces:**
- 复用 `AiModel`、`Page[T]`、`ResolvedModel`、`TokenUsage`、`LlmRequestOptions`，禁止另外建立宽松的管理 DTO 基类。
- 产生 `AgentCreate/Update/View`、`SkillCreate/Update/View`、`McpCreate/Update/View`。code 使用 `[a-z][a-z0-9_-]{0,63}`；显示名称 1–200，描述 0–2000；集合不允许重复；全部新对象 enabled=false。Update 含 expected_revision，区别未提供字段与显式 null。
- `AgentCreate`：code/name/description/system_prompt/model_id/skill_ids/tools/mcp_ids/max_steps/options/enabled。max_steps=3。`SkillCreate`：name/description/category/system_prompt/sample_prompt/tools/enabled；示例最多 32,000 字符。
- `McpCreate`：code/name/description/category/transport/endpoint/credential_ref/allowed_tools/search_tool/timeout_seconds/required/enabled；timeout_seconds=10、required=false；endpoint 最多 512 字符，search 分类必须指定 search_tool。
- `AgentRequest`：agent_code 可空、model 可空、prompt、messages、session_id、request_id、tools 可空 tuple、mcp_services 可空 tuple、max_steps 可空、options、timeout_seconds。管理员 DTO 只暴露任务 11 指定字段，不直接接受此内部类型。
- `ExecutionAuthority`：entry=admin|qq、allowed_tools、allowed_mcp_services、可选 `chat_token: SecretStr`（exclude/repr=false），由服务端创建；模型或 API JSON 不得直接构造它。
- `ToolDefinition(name, description, input_schema, source)`、`ToolCall(call_id, name, arguments)`、`ToolResult(call_id, name, content, is_error)`；source=chat|builtin|mcp。模型只见工具名称和参数 schema。
- `TurnMessage(role, text, tool_calls, tool_result)` 严格校验角色对应字段；`AgentModelCall(resolved, credential, messages, tools, options, timeout_seconds, final_only)`；`ModelTurn(content, calls, model, usage, continuation)`；continuation 是适配器持有的短期、不序列化上下文。
- `ModelEvent(kind=text_delta|turn_end, delta, turn)`：仅 turn_end 提供完整 ModelTurn；`AgentEvent(kind, execution_id, sequence, turn, delta, tool, code, message, metadata)`。kind 固定为设计中的 9 类；metadata 对 done 包含 `AgentCompletion` 的无正文元数据。
- `AgentCompletion` 含 content、execution_id、request_id、model、usage、duration_ms、record_saved、backend=python-agent；`AgentReadiness` 含 initialized/agent_available/model_available/code，不访问远端。

- [x] **Step 1:** 编写 `test_contract_limits_and_secret_exclusion`、`test_tool_messages_require_matching_role`、`test_request_cannot_expand_limits`、`test_patch_omitted_differs_from_null`。断言：
  ```python
  assert AgentCreate(**valid_agent).max_steps == 3
  assert AgentCreate(**valid_agent).enabled is False
  assert "chat_token" not in authority.model_dump()
  assert "max_steps" not in AgentUpdate(expected_revision=1).model_fields_set
  ```
  参数化拒绝 max_steps=0/6、timeout=301/NaN、重复工具、超大 schema、带 userinfo 的 MCP URL、非法凭据引用。
- [x] **Step 2:** 运行 `tests/agent/test_contracts.py`，预期因新契约不存在而 FAIL。
- [x] **Step 3:** 实现上述类型及 `AgentError(AiError)`；code 统一 `AGENT_` 前缀，继承既有 status_code 安全错误机制，不在异常消息拼接入参。
- [x] **Step 4:** 同时运行新契约及 `tests/ai/test_contracts.py`，预期 PASS；保存任务记录。

### Task 2: 目录数据库、关联约束与一致快照

**Files:** 新增 `src/agent_bot/agent/{catalog,catalog_rows}.py`、`sql/agent_bot_agent_init.sql`、`tests/agent/{conftest,fake_database,test_catalog}.py`；修改 `src/agent_bot/ai/catalog.py` 的模型引用删除边界。

**Interfaces:**
- `AgentCatalog(AiDatabase)`：`async create_agent(data)->AgentView`、`update_agent(id, patch)->AgentView`、`delete_agent(id, expected_revision)->None`、`get_agent(id)->AgentView`、`list_agents(*, limit=50,cursor=None)->Page[AgentView]`；Skill/MCP 同样命名为 create/update/delete/get/list_skill(s)、mcp(s)。
- `async snapshot(request: AgentRequest)->AgentSnapshot`，一次数据库事务返回选定 Agent、关联启用 Skill/MCP、ResolvedModel、revision；不打开 MCP 或解析密钥。无 agent_code 时 model 必填，使用空 Skill 和请求选择的 MCP。
- `async readiness(agent_code: str|None)->AgentReadiness` 检查所需列存在；缺表/列返回初始化错误，不能只执行 SELECT *。
- `AgentSnapshot` 为任务 1 新增的冻结类型，含 agent 可空、skills、mcps、resolved、revision 映射；事务内读取避免目录关系混用。`AgentCatalog.model_in_use(connection, model_id)->bool` 的模型删除检查只在新表存在时查询；数据库 FK 是最终约束。
- `tests/agent/fake_database.py` 继承/组合第二阶段 DB-API double，建立真实 SQLite 关系与列，使用参数化 SQL；不伪造 repository 返回值。

- [x] **Step 1:** 编写 `test_crud_cas_and_references`、`test_snapshot_survives_later_edits`、`test_missing_tables_leave_plain_catalog_usable`，核心断言：
  ```python
  assert updated.revision == original.revision + 1
  assert old_snapshot.resolved.provider.base_url == "https://original.invalid/v1"
  assert stale_update.value.status_code == 409
  assert referenced_model_delete.value.status_code == 409
  ```
  覆盖并发修改关系、显式空列表清空关联、大小写 code、禁用/缺失关联、分页 limit=0/101、游标注入和配置日期。
- [x] **Step 2:** 运行 `tests/agent/test_catalog.py`，预期新目录缺失导致 FAIL。
- [x] **Step 3:** 建立设计的 7 张表（Run/Step 列按任务 8），utf8mb4/InnoDB、UTC 时间、code 二进制比较、唯一关联、FK/revision；同一事务更新关系。禁止 DROP 和自动执行 SQL。
- [x] **Step 4:** 运行 `tests/agent/test_catalog.py tests/ai/test_catalog.py`，预期 PASS；记录 SQLite 未验证真实 MySQL 锁/DDL 的边界。

### Task 3: 提示词、权限交集与严格 schema 校验

**Files:** 新增 `src/agent_bot/agent/prompting.py`、`tools/{__init__,policy,schema}.py`；新增 `tests/agent/test_policy.py`；修改 `pyproject.toml`、`uv.lock`。

**Interfaces:**
- `ToolPolicy.select(*, configured: tuple[str,...], requested: tuple[str,...]|None, authority: ExecutionAuthority, available: tuple[ToolDefinition,...])->tuple[ToolDefinition,...]`；返回稳定排序的交集，显式请求越权工具报错，requested=() 得到空集合。无预设时 configured 使用服务端为入口批准的集合。
- `ToolSchema.validate_definition(tool)->None`、`validate_arguments(tool, arguments)->dict[str, object]`：直接依赖已锁定的 jsonschema 4.26.0（声明 `>=4.26,<5`），固定 Draft 2020-12；只接受 object 根、允许的结构/数值/数组/字符串约束与局部 `$defs`/`$ref`，拒绝远程引用、循环引用、pattern/patternProperties 和未实现的 vocabulary，不做隐式类型转换。
- `AgentPromptBuilder.build(role: str, skills: tuple[SkillView,...], tools: tuple[ToolDefinition,...], *, final_only: bool=False)->str`；只注入已关联且启用 Skill，依赖缺失失败；工具输出永远不是 system 内容。

- [x] **Step 1:** 编写 `test_skill_dependencies_never_grant_tools`、`test_empty_selection_disables_tools`、`test_schema_rejects_remote_ref_without_network`、`test_prompt_size_boundaries`：
  ```python
  assert selected == ()  # requested=()
  assert "memory" not in {tool.name for tool in selected_without_memory_permission}
  assert network_calls == []  # 远程 $ref 在加载前拒绝
  assert final_prompt.endswith(final_answer_instruction)
  ```
  检查 64 KiB/depth=16 边界、超深 arguments、未知字段约束、角色换行规范化、Skill 相同名字按 ID 稳定排序。
- [x] **Step 2:** 运行 `tests/agent/test_policy.py`，预期 FAIL。
- [x] **Step 3:** 实现权限与提示词，不照搬 Java 全量启用 Skill 注入；添加 jsonschema 直接依赖并更新锁文件，仅改变必要依赖声明。
- [x] **Step 4:** 运行 `tests/agent/test_policy.py` 与 `uv lock --check`，预期 PASS；记录受支持 schema 范围供 MCP 页面错误说明使用。

### Task 4: 三种模型协议的工具消息和真实流式输出

**Files:** 新增 `src/agent_bot/ai/providers/{agent_base,agent_responses,agent_chat,agent_ollama}.py`；修改必要的共享解析/传输边界；新增 `tests/agent/test_model_protocols.py`。

**Interfaces:**
- `AgentModelProvider`：`validate_options(options)->None`、`async complete_turn(call: AgentModelCall)->ModelTurn`、`stream_turn(call: AgentModelCall)->AsyncIterator[ModelEvent]`、`async aclose()->None`。
- 三个实现构造器接收已有 `ProviderTransport`，复用 SSE/NDJSON 有界解析器和参数映射；不改普通 TextProvider 返回结构。AgentModelCall 不通过 TextRequest 校验器，因为工具消息不满足“末条 user”。
- Responses 保留原生 function_call/call_id、function_call_output 关联；兼容协议保留 assistant tool_calls 和 tool 消息；Ollama 保留工具名称和索引，在协议无 call ID 时生成执行内唯一 ID。Responses 必需的供应商续传数据只在内存保留，不显示/持久化推理。
- 每轮流只产生 text_delta 与完整 turn_end；参数片段不能触发工具。正文未形成正常终态时不能返回成功。

- [x] **Step 1:** 对三个协议参数化 `test_model_tool_roundtrip`、`test_text_before_tool_call_is_not_final`、`test_fragmented_arguments_and_duplicate_ids`、`test_final_only_never_accepts_tool_calls`、`test_stream_cancel_closes_transport`。MockTransport 验证第二次请求含第一次调用 ID 和结果：
  ```python
  assert turn.calls[0].name == "web_search"
  assert final.content == "已查询到结果"
  assert len(sent_requests) == 2
  assert closed is True
  ```
  用真实协议 JSON/SSE/NDJSON fixture，覆盖多工具交错碎片、UTF-8 拆分、usage 缺失、EOF、incomplete、超大帧和不支持选项。
- [x] **Step 2:** 运行 `tests/agent/test_model_protocols.py`，预期 FAIL；fixture 必须在实现前对照官方协议资料核实，保存核对链接与日期。
- [x] **Step 3:** 实现三个适配器；不得为了匹配 fixture 猜字段或重发换协议。final_only 明确移除工具并按协议禁止工具调用；协议不支持选项时报错。
- [x] **Step 4:** 运行 `tests/agent/test_model_protocols.py tests/ai/test_providers.py tests/ai/test_parsing.py`，预期全部 PASS，普通聊天保持兼容。

### Task 5: MCP 连接、发现与凭据边界

**Files:** 新增 `src/agent_bot/agent/mcp/{__init__,client,http,discovery,credentials}.py`、`tests/agent/{mcp_server,test_mcp}.py`；修改 `pyproject.toml`、`uv.lock` 以直接声明实际引用的 httpx2。

**Interfaces:**
- `McpCredentials.resolve(reference: str|None)->SecretStr|None`，只读本项目 `.env` 或环境，环境优先；前缀 `AGENT_BOT_MCP_`，值拒绝 CR/LF/NUL，无引用允许无认证。
- `McpConnector.open(servers: tuple[McpView,...], *, remaining: Callable[[],float])->AsyncContextManager[McpConnection]`；连接和退出发生在同一任务。
- `McpConnection.tools: tuple[ToolDefinition,...]`、`warnings: tuple[ServiceWarning,...]`、`async call(call: ToolCall)->ToolResult`。ServiceWarning 只有 code/service_code，不含 endpoint 或凭据。
- HTTP 边界使用锁定 `mcp.client.session.ClientSession`、`streamable_http_client(http_client=...)`、`sse_client(httpx_client_factory=...)`，SDK 2.3.0 使用 httpx2，不能注入 httpx.AsyncClient。为 SDK JSON 响应增加 1 MiB 上限，SSE frame=256 KiB，HTTP 客户端 trust_env=false。
- 发现根据 SDK `list_tools(params=PaginatedRequestParams(cursor=...))` 遍历，校验本次 20 工具/10 页上限与重复游标；工具白名单按配置筛选，`*` 只在管理员显式配置时生效。

- [x] **Step 1:** 编写两种传输的 `test_real_local_initialize_list_and_call`、`test_sse_foreign_endpoint_never_receives_token`、`test_discovery_repeated_cursor_stops`、`test_required_and_optional_connection_failure`、`test_cancel_closes_sessions_in_owner_task`：
  ```python
  assert result.content == "local result"
  assert foreign_requests == []
  assert warning.code == "AGENT_MCP_UNAVAILABLE"
  assert server.active_sessions == 0
  ```
  `mcp_server.py` 用当前 SDK 的本地测试服务提供握手、分页、超限和挂起模式；额外覆盖 401 不跳过、schema 不跳过、同名冲突、deadline 不跳过及错误正文脱敏。
- [x] **Step 2:** 运行 `tests/agent/test_mcp.py`，预期 FAIL；测试服务仅随机 loopback 端口，结束后关闭。
- [x] **Step 3:** 实现客户端和关闭边界，使用独立 HTTPX2 工厂校验所有出站请求 origin；拒绝跨源 endpoint/redirect，认证仅发往登记 origin。required=false 仅隔离连接拒绝/网络超时，不能吞协议、鉴权和配置错误。
- [x] **Step 4:** 运行 `tests/agent/test_mcp.py tests/unit/test_mcp_server.py`，预期 PASS，确认 Chat 自有 MCP 服务回归无变化。

### Task 6: 本地 Chat 工具适配与请求授权

**Files:** 新增 `src/agent_bot/agent/tools/chat.py`、`tests/agent/test_chat_tools.py`；必要时从 `src/dispatch_chat/integrations/mcp.py` 抽取共享业务调用到 `src/dispatch_chat/services/tool_execution.py`。

**Interfaces:**
- `ChatToolBridge(server: ChatMcpServer)`：`async list_tools(authority: ExecutionAuthority)->tuple[ToolDefinition,...]`、`async call(call: ToolCall, authority: ExecutionAuthority)->ToolResult`。使用现有 list_tools/call_tool 内部边界，保留 memory 首屏目录、deadline 和 registry 验证。
- 唯一允许迁入集合：memory/naraka_query/naraka_search/mention_member。bridge 再次检查 source 与 authority 后读取令牌；GUI entry=admin 不允许 Chat 工具。

- [x] **Step 1:** 编写 `test_gui_cannot_forge_qq_scope`、`test_revoked_token_prevents_second_call`、`test_memory_scope_and_mention_collector_preserved`：
  ```python
  assert bot.sent_messages == []  # mention 只登记，不直接发消息
  assert unauthorized_memory_writes == []
  assert second_call_error.code == "AGENT_TOOL_UNAUTHORIZED"
  ```
  通过真实 ToolSessionStore 和 ChatToolRegistry 加 Fake 业务服务测试，不 mock 掉授权验证；拒绝跨用户/群、超时和第四阶段工具。
- [x] **Step 2:** 运行 `tests/agent/test_chat_tools.py`，预期 FAIL。
- [x] **Step 3:** 实现进程内 bridge，复用现有错误类型和业务实现；不读取任意 bot/group/user 参数，不请求自身 MCP HTTP 地址。
- [x] **Step 4:** 运行 `tests/agent/test_chat_tools.py tests/unit/test_mcp_server.py tests/integration/test_chat_mcp_plugin.py`，预期 PASS；确认令牌在结束后撤销。

### Task 7: 搜索与公开网页工具

**Files:** 新增 `src/agent_bot/agent/tools/{search,read_url,public_http}.py`、`tests/agent/{test_search,test_public_http}.py`；直接声明所用 httpcore 1.x 依赖。

**Interfaces:**
- `SearchTool(connector: McpConnector, server: McpView)`：`definition: ToolDefinition`、`async call(call: ToolCall)->ToolResult`。保持名称 web_search 和旧 query/numResults/searchMode/contextMaxCharacters 参数；searchMode=auto|request|playwright，contextMaxCharacters=1000–10000。输出 query/results，结果项 title/url/snippet；在调用后端时才映射 limit 或 num_results，不改模型看到的工具参数。
- `ReadUrlTool(reader: PublicPageReader)`：同一 definition/call 约定，参数 url/offset/maxCharacters 与旧实现一致，offset=0–1048576、maxCharacters 默认 10000 且 1–20000；比通用 32,000 字符结果上限更严格。`PublicPageReader.read(url: str, *, remaining: Callable[[],float])->PageText` 返回原文、最终公开 URL 和截断信息；仅处理 text/plain、text/html，不执行脚本。工具输出保持 url/title/contentType/content/links/offset/nextOffset/totalCharacters/hasMore/truncated；links 中只保留合法 HTTP(S) URL，最多 20 个，文字最多 200 字符。
- `PublicNetworkBackend(httpcore.AsyncNetworkBackend)` 注入 DNS resolver 与真正 network backend；connect_tcp 先解析全部地址并拒绝任一非 global IP，向验证后的数值 IP 建连并核验 peer address。保留原 hostname 的 Host/SNI/TLS 证书校验；禁止 Unix socket、代理和环境代理，不在 DNS 检查后用 hostname 再次解析连接。
- HTTPX AsyncBaseTransport 桥接专用 httpcore.AsyncConnectionPool；跳转手动最多 3 次，每次重新校验。文本/HTML 解析只提取有界正文，输出和解压上限在实际读流中实施。

- [x] **Step 1:** 编写 `test_search_modes_and_result_limits`、`test_search_mcp_not_exposed_as_general`、`test_dns_rebinding_cannot_change_connected_ip`、`test_redirect_to_private_address_rejected`、`test_decompressed_size_limit`：
  ```python
  assert len(results) <= 5
  assert sum(len(item.snippet) for item in results) <= 10000
  assert connected_host == validated_numeric_ip
  assert private_connections == []
  ```
  覆盖 IPv4-mapped IPv6、混合公网/内网 DNS、userinfo、无效 URL、超多跳转、非文本、空结果、搜索错误；原参数与返回字段逐项比对 Java 测试。
- [x] **Step 2:** 运行 `tests/agent/test_search.py tests/agent/test_public_http.py`，预期 FAIL。
- [x] **Step 3:** 实现受限工具、连接后 peer 核验及流量上限；缺少搜索配置明确不可用，不改用模型自身猜测结果或备用搜索商。
- [x] **Step 4:** 同命令预期 PASS；确认全部 DNS/HTTP 替身无外网，日志无完整网页或认证头。

### Task 8: Run/Step 存储与故障记录

**Files:** 新增 `src/agent_bot/agent/records.py`、`tests/agent/test_records.py`；补齐任务 2 SQL 中 Run/Step 索引与约束。

**Interfaces:**
- `AgentRunStore(AiDatabase)`：`async begin(run: RunStart)->None`、`append_step(step: StepWrite)->None`、`finish(run: RunFinish)->None`、`list_runs(*,limit=50,cursor=None,session_id=None,status=None)->Page[RunSummary]`、`get_run(execution_id)->RunDetail`、`list_steps(execution_id,*,limit=50,cursor=None)->Page[StepView]`。
- RunStart/Finish/Summary/Detail、StepWrite/View 定义在 contracts.py；运行 execution_id=UUID hex，step 唯一键(run_id,sequence)，finish 只允许 running→terminal 一次。目录删除不级联清除历史记录；step 关联 run。
- status=running/completed/failed/cancelled；step status=started/completed/failed/unknown，取消时不能把未确认的远端副作用写成未执行。

- [x] **Step 1:** 编写 `test_run_terminal_transition_and_step_order`、`test_record_list_excludes_bodies_and_secrets`、`test_failed_save_does_not_replay_work`、`test_keyset_pages_same_timestamp`：
  ```python
  assert [step.sequence for step in detail_steps] == [1, 2, 3]
  assert "prompt" not in summary.model_dump()
  assert "arguments" not in persisted_step
  assert duplicate_finish_error.status_code == 409
  ```
  覆盖缺表、非法/跨 run 游标、重启残留 running、不含秘密的安全摘要。
- [x] **Step 2:** 运行 `tests/agent/test_records.py`，预期 FAIL。
- [x] **Step 3:** 实现参数化 SQL、状态 CAS、分页与字段脱敏；数据库操作复用现有 write 完成边界，记录失败由服务层报告，不伪造存储成功。
- [x] **Step 4:** 运行 `tests/agent/test_records.py tests/ai/test_sessions.py`，预期 PASS。

### Task 9: 统一执行循环、预算、取消和服务装配

**Files:** 新增 `src/agent_bot/agent/{engine,service}.py`、`tools/runtime.py`、`tests/agent/{test_engine,test_service}.py`。

**Interfaces:**
- `ToolRuntime.open(snapshot: AgentSnapshot, request: AgentRequest, authority: ExecutionAuthority)->AsyncContextManager[BoundTools]`；BoundTools.definitions 和 `async call(ToolCall)->ToolResult`，组合任务 3、5–7；所有连接在此 context 生命周期内。
- `AgentEngine.run(prepared: PreparedRun, tools: BoundTools)->AsyncIterator[AgentEvent]`，依赖注入 AgentModelProvider，不查数据库、不解析配置；校验当前全部 calls 后才开始该批第一项副作用。
- `AgentService(catalog, records, model_credentials, providers, tool_runtime)`：`async complete(request, authority)->AgentCompletion`、`stream(request,authority)->AsyncIterator[AgentEvent]`、`async readiness(agent_code=None)->AgentReadiness`、`async aclose()->None`。
- PreparedRun 持有 execution_id、快照、提示词、authority、预算和模型调用参数；只在执行内存存在。complete 消费同一 stream/engine，若 error 终止转换为 AgentError，不重复请求模型。
- text_delta 带 turn，表示当前轮生成草稿；model_start 清空上一轮草稿。若该轮最后产生工具调用则不将草稿并入 final content/history；只有无调用且合法终态时 done 确认最终文字。最终无工具收尾直接转发真实供应商 delta，不用定时拆分。

- [x] **Step 1:** 编写 `test_rounds_1_3_5_then_final_only`、`test_text_before_tool_call_is_not_final`、`test_authorization_failure_precedes_all_side_effects`、`test_disconnect_after_tool_side_effect_does_not_retry`、`test_busy_ninth_call_rejected`：
  ```python
  assert model_calls == max_steps + 1
  assert final_call.final_only and final_call.tools == ()
  assert tool_invocations == ["once"]
  assert active_runs == 0
  assert record_saved is False  # 成功结果的记录失败场景
  ```
  加入总 deadline 跨连接/模型/工具递减、每轮 8/9 与总 40 上限、重复 ID、超大上下文、模型异常/工具业务失败、真正取消、退出所有连接、部分记录失败等测试。
- [x] **Step 2:** 运行 `tests/agent/test_engine.py tests/agent/test_service.py`，预期 FAIL。
- [x] **Step 3:** 实现准备→名额→预算→连接→循环→终态→清理；并发名额与截止时间覆盖准备和记录阶段，不只包围模型调用。生成器 aclosing，取消原样传播，结束不会派生无主后台任务。
- [x] **Step 4:** 运行全部 `tests/agent`，预期 PASS；读取资源清理测试结果，记录错误保存属于尽力而为的实际边界。

### Task 10: QQ 路由、配置、模型预检与运行时生命周期

**Files:** 修改 `src/agent_bot/ai/routing.py`、`src/dispatch_chat/{config,model_guard}.py`、`services/chat/{contracts,service}.py`、`plugin/{runtime,application}.py`、`src/agent_bot/admin/runtime.py`、`.env.example`；新增 `tests/agent/test_routing.py`；修改受影响原有配置/Chat 测试。

**Interfaces:**
- 新增三个运行配置：`dispatch_chat_agent_backend: Literal['java','python']='java'`、`dispatch_chat_python_agent_code: str|None=None`、`dispatch_chat_python_search_mcp_code: str|None=None`；复用已有 agent_max_steps/web_search_max_steps/mcp_services，不重复增加轮次字段。
- `AiRouteRequest` 增加具体 chat_tools 和 mcp_services；`select_route(request, *, plain_backend, agent_backend)->RouteDecision`，含 backend=java|python、capability=ordinary-text|agent|legacy、reason；旧 select_backend 可保留薄包装供旧调用方。
- RoutingAiClient 保持 complete 的外部参数兼容，新增 agent_service、agent_backend、agent_code；QQ authority 从实际 tool session 和当前服务端配置构造。绑定 Agent 时以预设模型为默认；无预设使用现有 Chat 选定模型。
- ChatService 的 Java/普通预检继续执行；Python Agent 预检由 AgentService 在同一配置快照中完成，禁止先用 Java ai_model 检查 Python alias，也不在预检与执行间重新选择 Agent 默认模型。
- `build_runtime_state(..., chat_tool_bridge: ChatToolBridge|None=None)` 用延迟解析当前 Chat MCP 业务边界的方式注入，避免 application 模块初始化顺序循环。PluginRuntimeProvider 新增 agent_service 属性，每次在 lease 下访问当前运行时。

- [x] **Step 1:** 编写 `test_mixed_capabilities_keep_java_model_guard`、`test_python_agent_uses_selected_model_snapshot`、`test_defaults_keep_java_without_agent_tables`、`test_python_failure_never_falls_back`、`test_runtime_refresh_waits_for_active_agent`：
  ```python
  assert decision.backend == "java"  # memory + local_workflow_generate_image
  assert java_requests == []  # Python Agent 模型失败
  assert resolved_model == agent_snapshot.resolved.model.alias
  assert close_count == 1
  ```
  覆盖 Laya 隐式服务、图片/视频、tools 为空、agent 开关、web-only、无 MCP mount、令牌寿命、运行时字段描述及配置热刷新。
- [x] **Step 2:** 运行新 routing 和旧 `tests/ai/test_routing.py`，预期新增场景 FAIL。
- [x] **Step 3:** 实现路由和装配；检查 `ChatService` 同步设置 use_agent 的所有分支，不依赖 client 默认值改变行为。所有新客户端只关闭一次，共享数据库池不由 AgentService 关闭。
- [x] **Step 4:** 运行 `tests/agent/test_routing.py tests/ai/test_routing.py tests/unit/chat/test_chat_agent_service.py tests/integration/test_chat_mcp_plugin.py` 及受影响配置测试，预期 PASS；不得只修改测试字段数量掩盖行为差异。

### Task 11: 管理 API、流式断开与执行记录查询

**Files:** 新增 `src/agent_bot/admin/{agent_routes,agent_chat}.py`、`tests/admin/{test_agent_routes,test_agent_stream}.py`；修改 `admin/{routes,models,errors}.py`。

**Interfaces:**
- `install_agent_routes(router: APIRouter, runtime)->None` 挂在现有授权 router 下，数据库/服务访问均持有运行时 lease。
- 目录 REST paths：`/agents`、`/skills`、`/mcp-servers`，GET 列表/`/{id}`详情、POST 创建、PATCH/DELETE `/{id}`；DELETE 使用既有 VersionDelete body。
- `AgentProbeRequest` 只允许 agent_code、prompt、messages、session_id、request_id（可选）、max_steps（只能收紧）、timeout_seconds（只能收紧）；不允许 model/工具令牌/群用户字段或 system 消息。内部生成 authority，不从 JSON 反序列化 ExecutionAuthority。
- `/agent-runs` POST 同步和 GET 记录；`/agent-runs/stream` POST SSE；`/agent-runs/{execution_id}` GET 详情；`/agent-runs/{execution_id}/steps` GET 分页。先注册静态 stream 路径再注册动态详情。
- ServiceStatus 新增 agent_backend、configured_agent、python_agent、legacy_capabilities；本地 readiness 不发 MCP/模型请求。AgentChatProbe 在 lease 内固定配置并调用 AgentService。

- [x] **Step 1:** 编写 `test_agent_crud_requires_admin_and_cas`、`test_agent_api_rejects_forged_qq_authority`、`test_sync_and_stream_share_engine`、`test_disconnect_after_tool_side_effect_does_not_retry`：
  ```python
  assert unauthenticated.status_code == 401
  assert forged_scope.status_code == 422
  assert event_kinds[-1] == "done"
  assert runtime.active_leases == 0
  assert side_effect_count == 1
  ```
  用实际 ASGI receive/send 断开场景，断言 cancelled/unknown 记录、无 done、provider/MCP 关闭、生成器在原上下文释放；另测 SSE 开始后的 error、初始化前的 409/422/503 和鉴权前不读秘密配置。
- [x] **Step 2:** 运行新 admin 测试，预期 FAIL。
- [x] **Step 3:** 实现端点、SSE 序号和安全错误映射，复用 ManagedStreamingResponse；UUID execution ID 与请求关联 ID 分离，禁止自动重试。
- [x] **Step 4:** 运行 `tests/admin`，预期新旧管理 API 均 PASS。

### Task 12: Agent、Skill、MCP 目录管理界面

**Files:** 新增 `web/src/{agent-types,agent-api}.ts`、`web/src/pages/{Agents,Skills,McpServers}.tsx` 及对应测试；修改 `web/src/App.tsx`、`web/src/styles.css`。

**Interfaces:**
- TS 对象字段与后端一致；`AgentApi extends ApiClient` 提供 list/get/create/update/deleteAgents/Skills/McpServers 的类型化方法，不使用 any 或在组件里手写 fetch。
- 表单显示模型、关联 Skill/MCP、轮次、允许工具；MCP 可选 required/transport/category，credential_ref 只填变量名，实际 key 不可编辑。
- 更新仅提交 dirty 字段和 expected_revision，null 与 [] 显式清除；分页不悄悄丢掉已选但未在当前页的关联。禁用/删除引用失败呈现具体安全错误。

- [x] **Step 1:** 新增 `Agents.test.tsx`、`Skills.test.tsx`、`McpServers.test.tsx`；覆盖关联分页、CAS 冲突保留、required 切换、空白名单不能保存和 secrets 不显示：
  ```ts
  expect(screen.getByLabelText('系统提示词')).toHaveValue('尚未保存的修改');
  expect(updateBody).toEqual({ expected_revision: 1, tools: [] });
  expect(screen.queryByText('actual-secret')).not.toBeInTheDocument();
  ```
- [x] **Step 2:** `npm --prefix web test -- --run src/pages/Agents.test.tsx src/pages/Skills.test.tsx src/pages/McpServers.test.tsx`，预期 FAIL。
- [x] **Step 3:** 实现页面与导航；使用已有 Ant Design/原生表单风格。移动端导航可换行且文字完整，保持键盘标签、加载/错误/空状态。
- [x] **Step 4:** 同测试命令及 `npm --prefix web run typecheck`，预期 PASS；不手改打包静态文件。

### Task 13: Agent 对话步骤、取消和记录详情

**Files:** 新增 `web/src/{agent-stream,agent-stream.test}.ts`、`components/{AgentChat,AgentRunDetail}.tsx`、相应 `.test.tsx`；修改 `pages/{Chat,Sessions,Overview}.tsx` 和必要 API 类型。

**Interfaces:**
- `streamAgent(response: Response, signal?: AbortSignal): AsyncGenerator<AgentEvent>`：帧上限 256 KiB，严格 kind/sequence/execution_id 校验、终态互斥、EOF 不能假成功；finally cancel/release reader。
- AgentApi 增加 `completeAgent(request, signal)`、`streamAgent(request, signal)`、`listRuns(filters)`、`getRun(id)`、`listSteps(id,cursor)`；AbortSignal 贯穿网络读取。
- AgentChat 独立管理当前 turn 草稿、工具步骤与成功历史；model_start 切换轮次，tool_start 表示前轮草稿是中间内容，done 才提交最终轮成功正文。切换普通/Agent 模式先停止旧请求，不混入另一种模式的历史。
- AgentRunDetail 分页加载步骤，只展示允许摘要；状态 running 不转换成成功，record_saved=false 给出记录失败提示。

- [x] **Step 1:** 编写 `test_text_before_tool_call_is_not_final`、`test_stop_and_logout_abort_request`、`test_done_then_eof_keeps_success`、`test_missing_or_duplicate_terminal_fails`、`test_record_detail_does_not_reissue_agent`：
  ```ts
  expect(successfulHistory).toEqual([]); // 工具前的草稿不能提交
  expect(signal.aborted).toBe(true);
  expect(agentPostCount).toBe(1); // 查看记录不能重新执行
  ```
  覆盖 UTF-8 分片、取消后保留可见部分正文但不提交历史、不同 execution ID 污染、切换页面/退出、record_saved=false。
- [x] **Step 2:** 执行上述新前端测试文件，预期 FAIL。
- [x] **Step 3:** 实现模式、步骤、记录和概览后端显示，保留普通对话原交互和 12,000 字符成功历史限制；不展示原始模型推理或工具参数。
- [x] **Step 4:** `npm --prefix web test -- --run` 与 `npm --prefix web run typecheck`，预期全部 PASS。

### Task 14: 完整回归、浏览器、发布和回退验证

**Files:** 修改 `pyproject.toml`、`uv.lock`、`web/package.json`、`web/package-lock.json`、`README.md`、`AGENTS.md` 旧包命令、`tools/check_package.py`、`docs/migration/source-manifest.json`；新增 `docs/migration/phase-three.md`、`tests/agent/test_release.py`；构建生成 `src/agent_bot/static` 与 dist。

**Interfaces:** 版本 0.3.0；wheel force-include `sql/agent_bot_agent_init.sql` → `agent_bot/resources/agent_bot_agent_init.sql`。回退到 0.2.0 前移除任务 10 新增的 3 个字段，保留原 agent_max_steps/mcp_services 配置并恢复 Java 可用模型/code。

- [x] **Step 1:** 编写发布资源与配置回退测试：检查三个新字段经 SettingsStore CAS 设置/移除、旧字段/凭据保留、SQL 无 DROP、资源可通过 importlib.resources 获取。先用未更新包检查器验证新资源检查 FAIL，再实现版本/打包检查变化。
- [x] **Step 2:** 完成作者复核：按 Review Focus 检查实际代码、契约来源与边界；有问题用可复现测试修正，不声称有独立子代理审查。运行 `uv run ruff check .`、`uv lock --check`，预期退出码 0。
- [x] **Step 3:** 运行完整 Python：`uv run pytest -m "not e2e" -k "not database_onebot_token_reaches_adapter_without_log_disclosure" --allow-hosts=127.0.0.1,::1`。预期测试通过、1 个已知破坏性测试排除、覆盖率至少 80%；失败后修复对应范围再最终回归。
- [x] **Step 4:** 执行标准 `npm --prefix web ci`、`npm --prefix web run typecheck`、`npm --prefix web test -- --run`、`npm --prefix web run build`，预期通过。用真实管理 API 配合隔离 DB/模型/MCP，在桌面和手机验证三个目录页、Agent 工具往返、流式停止、记录及权限错误；保存截图与控制台结果，不连接外部服务。
- [x] **Step 5:** `uv build`、`uv run python tools/check_package.py dist/agent_bot-0.3.0-py3-none-any.whl`，预期资源检查通过；在独立环境安装 wheel，验证模块无启动副作用、两条 CLI --help、静态界面和 SQL 均存在。用实际 0.2.0 wheel 接受移除新字段后的 Fake SettingsStore 配置完成回退演练。
- [x] **Step 6:** 验证源项目原始摘要保持一致，迁入适配摘要单独更新；完成 README 启用步骤、目录行为差异、混合能力路由、schema 限制、真实联调边界和回退记录。停止本轮自建本地 QA 进程，核对端口关闭；记录最终包 SHA-256 和实际测试数量后交付，不部署、不提交。

## 计划自检与规格覆盖

| 规格章节 | 实施任务 |
| --- | --- |
| 1–4 目标、源码语义、方案、路由 | 1、3、9、10、14 |
| 5 数据与模块 | 1、2、8、10 |
| 6 模型与执行循环 | 1、3、4、9 |
| 7 权限与 Chat 复用 | 3、6、9、10、11 |
| 8 MCP、搜索、网页读取 | 3、5、7、9 |
| 9 管理 API、记录、GUI | 8、11、12、13 |
| 10 验证与交付 | 每任务 RED/GREEN、14 |
| 11 回退与兼容性 | 10、14 |

共享接口采用本计划的完整命名，不将 model ID、alias、agent code 混用。MCP 与模型 HTTP 客户端由各自拥有者关闭，数据库池所有权保留原运行时。准备/模型/工具/记录使用同一总预算，只有清理可以使用独立的短暂有界关闭预算（最多 5 秒），该预算不允许再执行工具。

本计划不扩大到第四阶段、生产数据迁移或部署。已确认在当前会话由主代理实施；用户审阅此计划后使用 executing-plans，逐任务推进，无需重新选择执行方式。
