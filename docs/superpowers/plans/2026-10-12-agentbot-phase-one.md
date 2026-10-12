# agentBot 第一阶段 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking. 本计划在当前聊天顺序执行，不委派子代理。

**Goal:** 建立可以独立安装和验证的 Python 机器人项目，迁入完整 Chat 运行能力，提供浏览器管理界面，并通过显式配置的 Java AI 兼容接口测试对话。

**Architecture:** 保留 `dispatch_chat` 的业务、持久化、队列和 NoneBot 插件，由新增 `agent_bot` 包装配入口、运行状态与管理 API。管理路由挂载到同一个 FastAPI 应用，React 界面通过同源 API 操作；第一阶段不替换 Java AI 编排。

**Tech Stack:** Python、uv、NoneBot2/FastAPI、Pydantic、HTTPX、MySQL、Redis；React、TypeScript、Ant Design、Vite；pytest、NoneBug、Ruff。

**Spec:** [2026-10-12-agentbot-migration-design.md](../specs/2026-10-12-agentbot-migration-design.md)

## Global Constraints

- 目标目录为 `D:/items/web/agentBot`；旧项目只读，保留用户本地变更，不提交、不推送。
- 发布包名 `agent-bot`；新入口模块 `agent_bot`；首阶段保留内部 `dispatch_chat` 命名。
- 不通过 `sys.path`、符号链接或相对路径导入同级旧仓库。
- 不复制 `.env`、虚拟环境、IDE、缓存、构建产物、运行时数据或生产凭据。
- 保留 SQL 表名、Redis key、配置字段、会话身份语义；新部署显式配置独立数据库与 Redis key 前缀。
- 管理凭据与 OneBot、AI、MCP 凭据分离，浏览器凭据仅保存于内存。
- Python 兼容范围沿用源项目 `>=3.10,<4.0`，开发与验证优先采用 Python 3.12；新依赖生成独立锁文件。
- 沿用 80% 分支覆盖率门槛；默认测试不访问真实 QQ、模型、MySQL、Redis 或外部 MCP。
- 机器人单实例运行；关闭界面不结束机器人进程。
- 第一阶段明确依赖 Java AI，不增加隐式降级或失败后的跨实现切换。

## Review Focus

- 修改普通配置时不能清空未提交的密钥；任务 2 测试读取脱敏、部分更新和显式清空语义。
- 并发保存不能覆盖新版本；任务 2 测试旧 revision 返回 409 且配置未被写入。
- 已保存配置未热应用或含需重启字段时不能显示为完全生效；任务 3、4 测试数据库版本、运行时版本和字段提示。
- 管理对话测试不能发出 QQ 消息或获得机器人工具会话；任务 3 测试调用仅限普通文本 `chatOne`，不调用 Bot API/MCP。
- 独立数据库缺失模型目录时不能绕过启用检查；任务 1、5 保留 ModelGuard 测试并验证初始化说明、兼容 SQL 与包资源。

## 文件职责

| 位置 | 职责 |
| --- | --- |
| `tools/migrate_chat.py` | 受控复制指定源码、测试、资源、SQL，生成 SHA-256 来源清单，拒绝覆盖不同内容 |
| `docs/migration/source-manifest.json` | 每个迁入文件的来源、目标、摘要和用途，以及明确排除的旧部署测试 |
| `src/dispatch_chat/` | 迁入现有机器人实现；入口集成之外不改变业务行为 |
| `src/agent_bot/__main__.py`、`application.py` | 新 CLI、NoneBot 初始化、管理路由与静态资源装配 |
| `src/agent_bot/admin/config.py`、`auth.py` | 管理启动配置、独立 Bearer 鉴权 |
| `src/agent_bot/admin/models.py`、`settings.py` | 类型化 API 数据、配置字段展示和 revision 更新 |
| `src/agent_bot/admin/runtime.py`、`chat.py`、`routes.py` | 插件运行时边界、普通对话测试、API 路由与错误映射 |
| `web/src/api.ts`、`types.ts` | 类型化同源 API 客户端，不持久化管理凭据 |
| `web/src/App.tsx`、`pages/`、`components/` | 运行概览、配置编辑、文本对话；复用适用的旧 AI 类型与 Markdown 组件 |
| `sql/agent_bot_model_catalog.sql` | 新部署所需的最小模型启用兼容目录，无供应商密钥 |
| `README.md`、`docs/migration/phase-one.md` | 启动、数据初始化、Java 依赖、验证及回退说明 |

## Task 1: 独立 Chat 包与迁移来源

**Files:** 创建 `tools/migrate_chat.py`、`pyproject.toml`、`.gitignore`、`.env.example`、`README.md`、`src/agent_bot/__init__.py`、`docs/migration/source-manifest.json`、`sql/agent_bot_model_catalog.sql`；迁入 `src/dispatch_chat/**`、`tests/**`、`sql/**` 和测试 fixtures。

**Interfaces:** 保留 `dispatch_chat.settings.SettingsStore.load() -> VersionedSettings`、`save(config, *, expected_revision: int) -> VersionedSettings`；保留 `dispatch_chat.services.chat.contracts.ChatClient`。其他任务直接导入新项目内的这些类型。

- [x] 建立明确复制清单：源 `dispatch-chat/src` 的 Python/JSON 资源、测试 Python/fixtures 与 SQL；逐个读取检查。排除 `test_gitlab_ci.py`、`test_deployment_config.py`，在迁移文档说明其依赖旧仓库发布结构；其余机器人行为测试全部迁入。
- [x] 复制脚本使用解析后的绝对源/目标路径和 SHA-256 清单，遇到不一致的已有目标文件立即退出，不读取 `.env`；记录前端实际复用文件的来源。
- [x] 新 `pyproject.toml` 沿用 Chat 的运行依赖范围、Ruff 规则及 pytest 覆盖率门槛，新增 FastAPI/Pydantic 的明确直接依赖和 `agent-bot = agent_bot.__main__:main` 入口，wheel 包含两个 Python 包及运行时资源。
- [x] 新 `.env.example` 使用独立数据库名和 Redis key 前缀，仅含无效占位值；保留原配置导入流程，不自动生成或覆盖真实文件。
- [x] 在独立库准备最小 `ai_model` 兼容表，仅包含 `model_id`、`type`、`enabled`。文档要求运维显式填写与 Java AI 一致的模型标识；不自动注册默认模型，不复制 `api_key`。
- [x] 生成独立 `uv.lock` 并执行 `uv sync --extra dev`。运行迁入的配置、ModelGuard、客户端和包边界聚焦测试：`uv run pytest tests/unit/test_bootstrap.py tests/unit/test_model_guard.py tests/unit/test_client.py tests/unit/test_package_boundaries.py --no-cov`。

## Task 2: 配置管理服务与鉴权

**Files:** 创建 `src/agent_bot/admin/{__init__,config,auth,models,settings}.py`、`tests/admin/test_settings.py`、`tests/admin/test_auth.py`。

**Interfaces:** `AdminConfig` 从 `AGENT_BOT_ADMIN_TOKEN` 读取管理秘密；`SettingsService(store, runtime_provider)` 提供 `read() -> SettingsSnapshot` 与 `update(request: SettingsUpdate) -> SettingsSnapshot`。`SettingsSnapshot` 包含 `database_revision`、`applied_revision`、`restart_required_fields`、字段列表。每个字段包含名称、描述、类型、当前可见值和 `secret_is_set`，秘密值始终为空。

- [x] 先添加失败测试：未认证/错误 Token 拒绝访问；公开响应与异常正文不含测试秘密；读取配置不调用 `runtime_config_to_plain_values` 后直接返回原文。
- [x] 实现恒定时间比较的 Bearer 鉴权；管理 Token 缺失或无效时服务装配明确失败，鉴权日志不记录 Token。
- [x] 添加更新失败测试：只修改温度时原有秘密不变，未知字段拒绝，遮罩文本不能充当新密钥，空 patch 拒绝，旧 revision 拒绝，非法完整配置拒绝且不保存。
- [x] 更新请求使用有字段类型的部分配置模型、`expected_revision >= 1`，只合并明确提供字段；最终通过 `RuntimeConfig` 验证，再调用现有 `SettingsStore.save`。不使用字段默认值覆盖省略项。
- [x] `SettingsService.read` 比较存储快照与当前运行时，以 `RESTART_REQUIRED_FIELDS` 计算待重启字段；版本不同明确显示等待应用，不自行改写配置。
- [x] 运行 `uv run pytest tests/admin/test_settings.py tests/admin/test_auth.py --no-cov`，确认鉴权、保存与脱敏测试通过。

## Task 3: 新入口、运行状态与管理 API

**Files:** 创建 `src/agent_bot/__main__.py`、`application.py`、`admin/{runtime,chat,routes}.py`、`tests/admin/test_routes.py`、`tests/admin/test_entrypoint.py`；只在迁入的 `src/dispatch_chat/plugin/application.py` 增加必要的公开运行时访问边界。

**Interfaces:** `create_application(stored: VersionedSettings, admin: AdminConfig) -> FastAPI` 在现有 NoneBot FastAPI 驱动上装配管理 API；`main(argv: Sequence[str] | None = None) -> int` 支持服务运行及原 `settings`、`workflows` 子命令。迁入插件提供 `get_runtime_state() -> RuntimeState`、`get_applied_settings_revision() -> int`、`acquire_runtime() -> AsyncContextManager[None]` 三个访问边界，新 `PluginRuntimeProvider` 据此提供任务 2 所需状态；连接机器人列表使用 NoneBot 的 `get_bots()`，不向 API 暴露模块全局变量。

- [x] 先测试 CLI 分发、缺失引导配置、插件加载失败和非 FastAPI 驱动；均要求明确失败，不启动替代空服务。
- [x] 新入口复用现有数据库加载、环境清理、NoneBot 初始化、OneBot Adapter 注册、插件加载顺序，随后安装管理路由。原离线子命令仍可用，帮助文字采用 agentBot 名称。
- [x] 新增 `GET /health/live`、受保护的 `GET /api/v1/status`、`GET /api/v1/bots`、`GET /api/v1/settings`、`PATCH /api/v1/settings`、`POST /api/v1/chat`。管理错误统一使用类型化 `ApiError(code, message, request_id)`；422 不回显输入、校验上下文或秘密值，revision 冲突返回 409，依赖不可用返回 503。
- [x] 对话测试请求只包含有界 `prompt`、`session_id` 和类型化历史消息；通过当前运行时租约调用现有 `DispatchAiClient.complete(..., use_agent=False, use_web_search=False, chat_tools=())`，不创建工具会话、不发送 QQ 消息、不允许浏览器指定供应商 URL 或秘密。
- [x] 通过 Fake Store、HTTP Mock 和 Fake Runtime 测试数据库未初始化、AI 超时、错误响应、运行时版本滞后、重启字段及对话成功；禁止真实外部连接。
- [x] 就绪状态检查现有初始化和队列状态，不以“机器人尚未连接”误判 HTTP 服务死亡；显式区分存活、依赖就绪与机器人在线。
- [x] 运行 `uv run pytest tests/admin/test_routes.py tests/admin/test_entrypoint.py --no-cov`，确认生命周期集成未重复初始化或消费。

## Task 4: 浏览器管理界面与静态资源

**Files:** 创建 `web/package.json`、`web/package-lock.json`、`web/tsconfig.json`、`web/vite.config.ts`、`web/index.html`、`web/src/main.tsx`、`web/src/App.tsx`、`web/src/api.ts`、`web/src/types.ts`、`web/src/pages/Overview.tsx`、`web/src/pages/Settings.tsx`、`web/src/pages/Chat.tsx`、`web/src/components/MarkdownRenderer.tsx`、`web/src/styles.css`、`web/src/pages/Settings.test.tsx`、`web/src/pages/Chat.test.tsx`；增加 `tests/admin/test_static.py`。

**Interfaces:** `api.ts` 通过相对 `/api/v1/` 路径调用任务 3 的 API，Authorization 凭据由 React 状态传入；请求与响应类型对应 Python DTO。`npm run build` 输出到 `src/agent_bot/static`，`npm run typecheck` 执行 `tsc --noEmit`。

- [x] 从旧项目选择性复用 Markdown 展示与聊天类型，记录来源；不复用旧项目域名、代理、无关页面或已构建资源。
- [x] 实现管理凭据输入、运行概览、机器人连接列表、配置字段编辑和普通文本聊天。Token 仅在内存，刷新需重新输入；秘密字段显示“已设置”并仅在用户输入新值时发送更新。
- [x] 配置页面显示保存版本、应用版本、需重启字段；冲突后提示重新加载，失败保留用户编辑内容。编辑字段按类型显示并由服务器再次验证，不把秘密遮罩当作字段值。
- [x] 聊天页面展示历史和错误，发送期间防重复提交；请求失败不假装收到回答。第一阶段标明 AI 由已配置的 Java 服务处理。
- [x] 增加静态路由失败测试：`/api` 或 OneBot 未匹配路径不得返回 SPA HTML；缺少前端构建时管理 API 仍正常工作，界面入口明确提示构建要求。
- [x] 实现 `/` 及静态资源访问，API 和 OneBot/MCP 路由优先；开发代理仅连接显式本地服务。生产不启用宽泛跨域权限。
- [x] 使用 Vitest、jsdom 和 React Testing Library 编写行为测试：设置页省略未编辑密钥，409 冲突保留编辑值，保存后显示版本差异和重启提示；聊天页失败时显示错误并恢复提交状态，成功后显示实际回复。Mock API 仅模拟 HTTP 契约，不在页面加入生产 Mock 开关。
- [x] 执行 `npm install`、`npm run typecheck`、`npm test -- --run`、`npm run build`，运行静态路由测试；验证页面登录、读取配置、保存反馈和对话流程，记录实际验证结果。

## Task 5: 完整验证、打包与运行说明

**Files:** 完成 `README.md`、`docs/migration/phase-one.md`、`.env.example`、依赖与包资源配置；创建 `tests/admin/test_packaging.py` 或等效 wheel 检查脚本。

**Interfaces:** 安装产物包含 `agent_bot`、`dispatch_chat`、机器人 JSON 资源与已构建管理页面；服务运行仅依赖新项目和显式外部服务。

- [x] 编写安装和启动步骤、SQL 应用顺序、初始化空配置表、独立管理员 Token、MySQL/Redis 配置、模型兼容目录维护和 Java AI 依赖说明。保留已有离线命令语义，不声称模型供应商/Agent 管理已迁移。
- [x] 执行 `uv run ruff check .`；执行完整默认离线测试 `uv run pytest -m 'not e2e' -k 'not database_onebot_token_reaches_adapter_without_log_disclosure' --disable-socket --allow-unix-socket`，明确排除需要真实 MySQL 且会清表的环境测试；不降低 80% 分支覆盖率门槛。若 Windows 事件循环的本地 socketpair 被 socket 插件阻断，仅调整测试隔离实现，不能开放真实外网连接。
- [x] 执行 `uv build`，检查 wheel 中两个 Python 包、JSON 资源与静态文件；在临时隔离环境安装 wheel，验证导入路径不包含旧源码目录，验证 CLI 帮助且不启动真实机器人。
- [x] 检查源码清单摘要、受控集成改动、秘密排除规则、文档链接与空白；复查源项目 `git status --short`，确认没有本任务引入的修改。
- [x] 在迁移文档记录测试数量、覆盖率、Ruff、前端检查、构建和打包结果，以及真实 QQ/模型/MySQL 联调尚未执行的原因。所有第一阶段验收条件满足后再报告第一阶段完成。

## 执行与审阅

设计与计划已于 2026-10-12 确认，使用 `superpowers:executing-plans` 在当前聊天逐项执行。遇到依赖或行为差异先定位原因，不删减行为测试以绕过失败。不创建额外聊天，不提交、不推送。Windows 事件循环需要本机 TCP socketpair，验证命令使用 `--allow-hosts=127.0.0.1,::1` 替代 blanket socket 禁用，仍阻断机器外连接并排除真实 MySQL 清表测试。AI Python 实现的第二至四阶段使用独立计划，不能把本阶段验收当成全量迁移完成。
