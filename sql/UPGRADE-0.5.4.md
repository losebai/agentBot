# AgentBot 0.5.4 数据库升级

本说明适用于已完成 0.5.3 素材迁移的 MySQL 或 SQLite 部署。更早版本先补齐相应升级；
不要批量执行目录中的历史 SQL，尤其不能把 `retire-*` 清理脚本当作初始化脚本。

## 新安装

新数据库仅使用 `agent_bot_init.sql`（MySQL）或 `agent_bot_init.sqlite.sql`（SQLite）。
SQLite 持久队列使用独立的 `agent_bot_queue_init.sqlite.sql`；不能把业务增量执行到队列库。
全新桌面安装首次启动会完成初始化，无需手工执行历史迁移。

## 从 0.5.3 升级

先关闭所有使用该业务库的 AgentBot 服务，备份启动文件、业务数据库、队列库和媒体目录。
SQLite 备份须包含尚未合并的 WAL 数据，建议停服后使用数据库备份工具生成一致快照。
升级过程中不要启动新旧后台，也不要修改 `CHAT_SESSION_SECRET` 或重新初始化已有库。

1. 安装 0.5.4 程序，但暂不启动。按实际数据库类型执行
   `2026-10-19-session-context.sql` 或 `2026-10-19-session-context.sqlite.sql`，
   新增统一会话上下文表，旧会话数据保持原样。
2. 使用本版 `tools/migrate_session_context.py --dry-run` 核对来源数量，
   再去掉 `--dry-run` 导入。工具保留摘要、消息及请求和投递身份，不覆盖已有新上下文。
   该步骤不要求补建旧 `chat_sent_message` 表。
3. 执行 `2026-10-20-merge-ai-sessions.sql` 或对应 `.sqlite.sql`，
   为统一归档新增原始 UTC 时间和索引。这个结构增量只能执行一次；
   然后在部署目录执行 `agent-bot.exe records merge-ai-sessions` 合并历史普通调用。
   合并按来源和执行 ID 去重并校验完整字段，冲突会明确失败。
4. 运行本版 `tools/migrate_tagged_memory.py`，把授权范围、标签、素材引用合入长期记忆表；
   再加 `--verify-only` 核验原文、ID、墓碑、时间、自增序列和全部关联。
   此工具自行完成对应数据库的结构增量，**不要先重复执行**同名 `merge-tagged-memory` SQL。
   SQL 文件同时提供等价手动步骤；工具发现孤儿、空作用域或冲突时须先处理原因。
5. 核对迁移计数与历史数据后启动 0.5.4，检查配置、会话、调用记录和长期记忆。
   普通启动不会自动迁移、删除旧表或修改已有密钥。

源码环境可直接使用 `uv run --no-sync python tools/迁移工具名.py --env-file <既有启动文件>`。
桌面版的两个 Python 工具需要额外的 Python 3.12 与 uv，不能直接交给 EXE 执行。
下载同版 wheel 和 SQL 包并解压后，在部署目录使用以下命令，替换尖括号中的绝对路径：

```powershell
uv run --no-project --python 3.12 --with "<wheel绝对路径>" python "<SQL包解压目录>/tools/migrate_session_context.py" --env-file "<启动文件绝对路径>" --dry-run
uv run --no-project --python 3.12 --with "<wheel绝对路径>" python "<SQL包解压目录>/tools/migrate_session_context.py" --env-file "<启动文件绝对路径>"
uv run --no-project --python 3.12 --with "<wheel绝对路径>" python "<SQL包解压目录>/tools/migrate_tagged_memory.py" --env-file "<启动文件绝对路径>"
uv run --no-project --python 3.12 --with "<wheel绝对路径>" python "<SQL包解压目录>/tools/migrate_tagged_memory.py" --env-file "<启动文件绝对路径>" --verify-only
```

wheel 文件名为 `agent_bot-0.5.4-py3-none-any.whl`，与 SQL 包在同一下载目录。
工具只操作启动文件选定的数据库；路径、后端与维护窗口由执行者核对。

## 旧表清理与回退

升级流程保留旧表。`2026-10-20-retire-legacy-session-context.*`、
`2026-10-20-retire-ai-session.*` 和 `2026-10-20-retire-legacy-tagged-memory.*`
会永久删除对应旧表，仅在所有旧实例停用、迁移逐项核验、原文有完整备份后另行执行。
本版发布和一键安装不会自动执行这些清理脚本。

回退须停止服务，恢复升级前程序、启动文件和一致的数据库及媒体备份。
只安装旧程序不能撤销数据库结构变更，也不会自动转换新版本产生的数据。
