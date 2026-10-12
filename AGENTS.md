# Repository Guidelines

## 项目结构与模块组织

- `src/`：平铺保存统一 agent_bot 包的源码，包含服务入口、应用装配、管理 API、AI/Agent/媒体模块、打包静态资源，以及机器人行为、配置、业务服务、插件及 OneBot、Redis、Java AI、ComfyUI 集成；打包时映射为 agent_bot。
- `web/src/`：React/TypeScript 管理界面，包含页面、组件、API 客户端、样式及同目录测试。
- `tests/`：Python 单元、集成、管理 API 和 AI 测试，以及共享替身与夹具。
- `sql/agent_bot_init.sql`：统一的新库初始化脚本；`sql/legacy/`：原样归档的历史脚本；独立注释增量脚本仅用于既有表。`tools/`：迁移及包检查工具；`docs/`：设计决策与实施计划。

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

新增或修改的 SQL 必须使用中文注释：脚本头说明用途、执行顺序和数据影响，表与字段使用中文 `COMMENT`，索引和外键说明设计目的。为已建表补注释时提供单独的增量脚本，保留原字段类型、默认值、可空性和约束；不得通过重建或删除表补注释。旧项目迁入并作为原始基线保存的历史 SQL 不批量改写。

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

密钥保存在 `.env` 或环境变量中，不得提交凭据。首次导入后，MySQL 是运行配置的权威来源。历史 SQL 必须逐项审查：`sql/legacy/dispatch_chat_setting.sql` 含删除表操作。使用独立数据库和 Redis 键前缀。

媒体文件目录由 `AGENT_BOT_MEDIA_DIRECTORY` 启动配置指定，默认 `data/media`；禁止把上传文件、TTS 输出和验收数据打包。媒体 SQL 仅手动执行；保留 0.3.0 wheel 用于隔离回退验证。
