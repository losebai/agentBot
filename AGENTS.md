# Repository Guidelines

## 项目结构与模块组织

- `src/`：平铺保存统一 agent_bot 包的源码，包含服务入口、应用装配、管理 API、AI/Agent/媒体模块、打包静态资源，以及机器人行为、配置、业务服务、插件及 OneBot、Redis、模型供应商、ComfyUI 集成；打包时映射为 agent_bot。
- `web/src/`：React/TypeScript 管理界面，包含页面、组件、API 客户端、样式及同目录测试。
- `tests/`：Python 单元、集成、管理 API 和 AI 测试，以及共享替身与夹具。
- `sql/agent_bot_init.sql` 与 `sql/agent_bot_init.sqlite.sql`：MySQL、SQLite 的新库初始化脚本；`sql/legacy/`：原样归档的历史脚本；独立注释增量脚本仅用于既有表。`tools/`：迁移及包检查工具；`docs/`：设计决策与实施计划。

## 构建、测试与开发命令

使用 Python 3.12、uv 和 Node.js 22。以下命令均在仓库根目录执行：

- `uv sync --extra dev --python 3.12`：安装 Python 开发依赖。
- `npm --prefix web ci`：按锁文件安装前端依赖。
- `uv run agent-bot`：启动服务，默认地址为 `127.0.0.1:8080`。
- `npm --prefix web run dev`：启动 Vite，API 请求代理到后端。
- `uv run ruff check .`：检查 Python 代码与导入顺序。
- `npm --prefix web run typecheck`：执行 TypeScript 严格类型检查。
- `npm --prefix web test -- --run`：运行一次前端测试。
- `npm --prefix web run build`：生成 `src/static/`；不要手动修改生成资源。
- `uv build`：前端构建完成后生成 wheel 和源码包。
- `uv run python tools/check_package.py dist/agent_bot-0.4.0-py3-none-any.whl`：检查 wheel；版本变化时调整文件名。

## 编码风格与命名规范

Python 使用四空格缩进和类型注解；函数、模块采用 `snake_case`，类采用 `PascalCase`。Ruff 按 Python 3.10 兼容性检查，行长上限为 100 字符。TypeScript 使用两空格缩进、单引号、分号及 `PascalCase` 组件文件名。遵循相邻代码风格；目前未配置前端格式化或 lint 工具。

新增或修改的代码需要注释：Python 的模块、类和函数使用中文文档字符串说明职责；TypeScript 的组件、函数及复杂分支补充中文注释。重点解释权限边界、协议差异、状态变化、取消和资源释放的原因，避免只复述语句。修改已有函数时同步修正过时注释，无关代码不做批量格式化。

新增或修改的 SQL 必须使用中文注释（SQLite 不支持 COMMENT，使用中文行注释保留字段与索引说明）：脚本头说明用途、执行顺序和数据影响，表与字段使用中文 `COMMENT`，索引和外键说明设计目的。为已建表补注释时提供单独的增量脚本，保留原字段类型、默认值、可空性和约束；不得通过重建或删除表补注释。旧项目迁入并作为原始基线保存的历史 SQL 不批量改写。

每次涉及数据库表、字段、索引、约束、默认值或注释的修改，都必须同步更新 `sql/agent_bot_init.sql`，保证新数据库仅执行该脚本即可获得当前完整结构。既有数据库的升级同时提供独立增量脚本；核对增量升级后的有效结构与初始化脚本一致，历史兼容列应明确说明。

## 实现原则

优先采用直接、明确的实现。仅在需求或已确认的兼容场景要求时增加兜底、重试、默认值和备用路径；不为假设性问题添加防御分支。失败应返回清晰错误，避免吞异常、静默降级或自动切换模型、协议及后端。复用已有校验，避免重复检查；保留必要的权限校验、数据约束和资源释放。

## 测试规范

Python 使用 pytest、pytest-asyncio、NoneBug 和 MySQL/Redis 替身，覆盖率包含分支，门槛为 80%。测试文件命名为 `test_*.py`。前端使用 Vitest、jsdom 和 Testing Library，测试命名为 `*.test.tsx`。

验证按改动范围选择最少且有效的检查，合并相关修改后集中执行。文档改动只核对内容与格式；局部代码改动优先运行相关测试和检查。涉及公共接口、跨模块行为或发布时再扩大验证范围。定向 Python 测试可使用 `--no-cov`，完整覆盖率验证仍保留 80% 门槛。

同一代码状态下，每项检查通过后不重复执行；仅在后续修改影响结果、检查失败或存在未解决问题时重跑。前端构建用于验证构建或更新静态资源；wheel 构建与包检查用于验证打包或发布，安排在相关修改完成后统一执行。日常开发不默认运行全部构建和测试，不新增仅复述实现的测试。

需要完整验证时，运行离线测试集：

```powershell
uv run pytest -m "not e2e" -k "not database_onebot_token_reaches_adapter_without_log_disclosure" --allow-hosts=127.0.0.1,::1
```

被排除的数据库测试会清空真实配置数据。日常测试使用 Fake/Mock，不连接 QQ、不调用付费模型、不访问生产服务。

## 提交与拉取请求规范

当前目录没有 Git 元数据，无法核实历史提交惯例。建议提交标题用简短动词描述变更。PR 应说明行为变化、关联问题及验证命令和结果；界面改动附截图，SQL 或配置改动说明影响。

## 安全与配置

桌面壳允许通过原生“服务”菜单编辑启动文件中的 `PORT`，端口须验证范围与占用，
保存后重启本窗口的后端，保留其他配置和密钥。OneBot Token 在网页“机器人配置 → QQ”编辑，
保存到启动文件后不切换当前连接，由用户手动使用已有“服务 → 重启后端”生效。
管理 HTTP API 允许保存严格授权、页面提供且启动文件可写的静态字段，保存不切换当前连接、
监听地址或数据库后端，由用户手动重启生效；环境变量覆盖的字段只读，修改文件不能覆盖启动环境。
网页不持有进程控制能力，不新增 Token 原生菜单。

静态配置（数据库后端及连接、Redis、管理员凭据、NoneBot/OneBot 启动字段、会话密钥、目录及固定资源参数）保留在 `.env` 或环境变量中；管理 API 仅允许写入已明确授权的静态字段，修改后手动重启。静态修改使用 `startup_revision` 校验启动文件版本，不能写入运行配置数据库，不回填密钥，不写回未编辑的陈旧值；静态密钥留空保留，可空密钥需显式清除为 null，提交失败须回滚文件与数据库修改。`ONEBOT_V11_ACCESS_TOKEN` 和 `CHAT_SESSION_SECRET` 须为至少 32 字符的不同随机值。会话密钥修改并重启后使用新的 HMAC 会话和标签记忆身份命名空间，旧历史与记忆数据保留，供应商/MCP 密钥不受影响。运行配置及模型供应商、MCP 的 URL、密钥通过管理页面保存到所选 MySQL 或 SQLite，不从环境变量读取或覆盖。首次初始化后，所选数据库是运行配置的权威来源。`CHAT_DB_BACKEND` 未指定时默认 SQLite，MySQL 部署必须显式指定 `mysql`；SQLite 文件由 `CHAT_DB_SQLITE_PATH` 指定。首次无配置且无已有数据时允许自动生成独立随机密钥、默认配置与 SQLite 数据库；OneBot Token 与会话密钥均写入 `.env`。已有部署不得重建数据库或覆盖密钥。队列默认 `CHAT_QUEUE_BACKEND=sqlite`，由 `CHAT_QUEUE_SQLITE_PATH` 指定本地库；Redis 仅在显式选择 `redis` 时使用，失败不得自动切换后端。不得提交凭据或数据库文件。历史 SQL 必须逐项审查：`sql/legacy/dispatch_chat_setting.sql` 含删除表操作。使用独立数据库和 Redis 键前缀。

媒体文件目录由 `AGENT_BOT_MEDIA_DIRECTORY` 启动配置指定，默认 `data/media`；禁止把上传文件、TTS 输出和验收数据打包。媒体 SQL 随首次全新本地配置初始化，或通过明确初始化命令及手动执行；已有部署普通启动不执行升级；保留 0.3.0 wheel 用于隔离回退验证。
