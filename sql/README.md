# SQL 使用说明

新安装只执行 [agent_bot_init.sql](agent_bot_init.sql)。先创建并选中独立 MySQL 数据库，
该文件按依赖顺序创建全部 25 张表，覆盖运行配置、群成员、聊天记忆、标签记忆、
工作流、Java 模型目录、Python AI、Agent、Skill、MCP 和媒体模块。

在仓库根目录执行：

```powershell
mysql -h 127.0.0.1 -u root -p agent_bot --execute="source sql/agent_bot_init.sql"
uv run agent-bot settings import-env
```

脚本不会填充运行配置或模型，也不会覆盖已有数据。Java 模型在 `ai_model` 中由管理员
登记，Python 模型通过管理界面登记；供应商密钥继续通过环境变量引用。
`CREATE TABLE IF NOT EXISTS` 允许重复执行，但不会修正既有表的结构。

已有实例按缺少的功能选择升级脚本，不批量执行目录：

| 场景 | 使用的脚本 |
| --- | --- |
| 只缺 Python AI 表 | `legacy/agent_bot_ai_init.sql` |
| 已有 AI 表，只缺 Agent 表 | `legacy/agent_bot_agent_init.sql` |
| 已有 AI/Agent 表，只缺媒体表 | `legacy/agent_bot_media_init.sql` |
| 既有十张 AI/Agent 表只补中文注释 | [2026-10-12-agentbot-schema-comments.sql](2026-10-12-agentbot-schema-comments.sql) |
| 旧记忆缺请求去重 | `legacy/dispatch_chat_memory_request_id.sql` |
| 旧记忆缺会话代际隔离 | `legacy/dispatch_chat_memory_generation.sql` |
| 更早的工作流、标签记忆或配置版本 | 逐项审查 `legacy/` 中对应历史迁移及其前置条件 |

`legacy/` 保留合并前的 25 份脚本，内容未改写，用于来源核对及指定旧版本升级。
其中 `dispatch_chat_setting.sql` 会删除配置表，部分迁移会覆盖配置或处理历史数据；
这些操作不属于统一安装脚本。历史设计文档和来源清单中的旧 SQL 路径对应本目录的
同名归档文件，不表示需要重新执行它们。

从源码或 editable 安装开发时直接使用仓库 `sql/` 中的脚本。安装 wheel 后可通过
`importlib.resources.files('agent_bot').joinpath('resources/agent_bot_init.sql')` 读取统一脚本。
wheel 兼容保留三份自有分阶段资源与
注释增量资源，服务不会自动执行 SQL。补注释必须使用独立增量脚本，保留类型、
默认值、可空性和约束；执行前按实例升级流程备份，并评估元数据锁影响。
