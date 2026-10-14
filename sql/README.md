# SQL 使用说明

新安装按 `CHAT_DB_BACKEND` 选择 [MySQL 脚本](agent_bot_init.sql) 或 [SQLite 脚本](agent_bot_init.sqlite.sql)。MySQL 先创建独立数据库，SQLite 文件由显式初始化命令创建，
该文件按依赖顺序创建完整业务表，覆盖运行配置、群成员、聊天记忆、标签记忆、共享知识、
工作流、Python AI、Agent、Skill、MCP、媒体、定时任务和自动发言决策模块。
聊天相关表和配置键使用 `chat_*`，环境变量使用 `CHAT_*`。

在仓库根目录执行：

```powershell
uv run agent-bot database init
uv run agent-bot settings init
```

脚本不会填充运行配置或模型，也不会覆盖已有数据。模型通过管理界面登记；
每个供应商及 MCP 的连接地址、密钥均在管理页面直接录入并保存到数据库。旧 `ai_model` 目录已退出运行时，新库不再创建，
已有数据库中的旧表不会被此脚本删除。
`CREATE TABLE IF NOT EXISTS` 允许重复执行，但不会修正既有表的结构。

每次修改数据库表、字段、索引、约束、默认值或注释，都必须同步更新
`agent_bot_init.sql` 和 `agent_bot_init.sqlite.sql`，使两个后端始终包含当前完整的新库结构。既有库升级另提供独立增量脚本，
并核对升级后的有效结构与初始化脚本一致；保留的历史兼容列应在升级说明中注明。

`legacy/` 仅作为原始 SQL 基线归档，保留原始名称和内容，不用于当前项目初始化。

从源码或 editable 安装开发时直接使用仓库 `sql/` 中的脚本。安装 wheel 后可通过
`importlib.resources.files('agent_bot').joinpath('resources/agent_bot_init.sql')` 读取统一脚本。
wheel 兼容保留三份自有分阶段资源与
注释增量资源，服务不会自动执行 SQL。补注释必须使用独立增量脚本，保留类型、
默认值、可空性和约束；执行前按实例升级流程备份，并评估元数据锁影响。

## 既有库升级动态凭据

已存在供应商与 MCP 表时执行 [2026-10-13-dynamic-provider-mcp-credentials.sql](2026-10-13-dynamic-provider-mcp-credentials.sql) 一次。脚本只增加实际凭据和鉴权要求字段，不重建表、不清空数据。旧 `credential_ref` 列保留作为历史信息，运行时不读取；有旧引用的记录需在页面重新录入密钥。新库使用统一初始化脚本，不重复执行此增量脚本。

## 旧表索引名称升级

旧 `dispatch_chat_*` 表改名为 `chat_*` 后，MySQL 会保留原索引名。执行 [2026-10-13-chat-index-names.sql](2026-10-13-chat-index-names.sql) 将记忆、群成员和工作流的索引原位改为当前名称。脚本可重复执行，新旧索引同时存在时不制造名称冲突，不改变字段、唯一性和历史记录。记忆初始化也兼容已确认的旧索引名称；缺少其他必需结构时仍明确报错。

SQLite 脚本使用中文行注释说明表、字段、索引和外键，替代 SQLite 不支持的 `COMMENT` 子句。JSON 字段通过 `json_valid` 约束，外键在每个连接启用；业务初始化使用事务，重复执行不改写已有数据。MySQL 增量 SQL 不可在 SQLite 执行。

## 标签知识库升级

既有 MySQL 部署显式执行 [2026-10-14-tags-knowledge-memory.sql](2026-10-14-tags-knowledge-memory.sql)，SQLite 部署执行 [2026-10-14-tags-knowledge-memory.sqlite.sql](2026-10-14-tags-knowledge-memory.sqlite.sql)。增量只新增知识正文与标签关联表，可重复执行，不删除或重建既有配置、记忆和密钥。新安装使用对应完整初始化脚本；普通启动不会自动升级，未启用知识工具的聊天不依赖这两张表。

标签记忆从本版本起只允许新增、查看和逻辑删除。同一作用域已有键会返回冲突，包括已删除键，不再覆盖正文、替换标签或恢复旧记录；新增事实需使用新键。此行为变化不修改既有记忆字段、唯一约束或历史内容。
