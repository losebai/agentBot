# SQL 使用说明

新安装只执行 [agent_bot_init.sql](agent_bot_init.sql)。先创建并选中独立 MySQL 数据库，
该文件按依赖顺序创建全部 27 张表，覆盖运行配置、群成员、聊天记忆、标签记忆、
工作流、Python AI、Agent、Skill、MCP、媒体、定时任务和自动发言决策模块。
聊天相关表和配置键使用 `chat_*`，环境变量使用 `CHAT_*`。

在仓库根目录执行：

```powershell
mysql -h 127.0.0.1 -u root -p agent_bot --execute="source sql/agent_bot_init.sql"
uv run agent-bot settings import-env
```

脚本不会填充运行配置或模型，也不会覆盖已有数据。模型通过管理界面登记；
每个供应商及 MCP 的连接地址、密钥均在管理页面直接录入并保存到数据库。旧 `ai_model` 目录已退出运行时，新库不再创建，
已有数据库中的旧表不会被此脚本删除。
`CREATE TABLE IF NOT EXISTS` 允许重复执行，但不会修正既有表的结构。

每次修改数据库表、字段、索引、约束、默认值或注释，都必须同步更新
`agent_bot_init.sql`，使其始终包含当前完整的新库结构。既有库升级另提供独立增量脚本，
并核对升级后的有效结构与初始化脚本一致；保留的历史兼容列应在升级说明中注明。

`legacy/` 仅作为原始 SQL 基线归档，保留原始名称和内容，不用于当前项目初始化。

从源码或 editable 安装开发时直接使用仓库 `sql/` 中的脚本。安装 wheel 后可通过
`importlib.resources.files('agent_bot').joinpath('resources/agent_bot_init.sql')` 读取统一脚本。
wheel 兼容保留三份自有分阶段资源与
注释增量资源，服务不会自动执行 SQL。补注释必须使用独立增量脚本，保留类型、
默认值、可空性和约束；执行前按实例升级流程备份，并评估元数据锁影响。

## 既有库升级动态凭据

已存在供应商与 MCP 表时执行 [2026-10-13-dynamic-provider-mcp-credentials.sql](2026-10-13-dynamic-provider-mcp-credentials.sql) 一次。脚本只增加实际凭据和鉴权要求字段，不重建表、不清空数据。旧 `credential_ref` 列保留作为历史信息，运行时不读取；有旧引用的记录需在页面重新录入密钥。新库使用统一初始化脚本，不重复执行此增量脚本。
