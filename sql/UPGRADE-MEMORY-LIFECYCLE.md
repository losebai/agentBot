# 长期记忆生命周期升级

本说明从**已完成 0.5.4 数据库迁移**的部署开始，适用于 MySQL 和 SQLite，不指定新的发布版本号。
更早部署先按照 [0.5.4 升级说明](UPGRADE-0.5.4.md) 完成统一会话、调用记录及标签记忆合表；
本工具发现缺少这些起点字段会明确失败。所有命令操作业务库，不能用于 SQLite 持久队列库。

## 新数据库

MySQL 仅执行 `agent_bot_init.sql`，SQLite 仅执行 `agent_bot_init.sqlite.sql`，即可得到完整当前结构。
不要在新库再次执行历史增量。向量缓存在同一个业务数据库，不需要独立向量数据库服务。

## 既有数据库的执行顺序

1. 停止所有连接该业务库的新旧 AgentBot 实例；备份启动文件、业务数据库、独立队列库和媒体目录。
   SQLite 必须保存包含 WAL 数据的一致快照，不能只在运行时复制主文件。保留升级前的程序与 wheel。
2. 安装与本次迁移工具、初始化 SQL 配套的程序，暂不启动；不要修改已有 OneBot Token、会话密钥或数据库后端。
3. 在停服状态先执行下述默认预览，核对选中的数据库、起点结构和记忆数量。
   默认预览不修改字段或数据，也不代替正式迁移后的完整核验。
4. 加 `--apply` 正式升级。工具按所选后端补齐缺少的列、索引、版本、证据、回执和向量缓存表，
   为旧事实建立首版快照，并归档旧摘要。**采用工具路径时不需要先手动执行结构 SQL。**
5. 单独运行 `--verify-only`，核验当前结构和事实版本；结合停服前备份核对 ID、条数、原文、墓碑、素材引用和旧时间值。
   检查预览、正式执行与核验报告中的数据库和计数一致，处理清楚每一个明确错误后再启动服务。
6. 登录管理页面检查当前记忆、版本历史、未知来源与时间说明。配置显式的记忆 Embedding 模型后，
   再验证自然语言召回；不配置时使用明确的标签读取，不会自动调用其他模型。

源码环境的 SQLite 示例（使用业务库绝对路径）：

```powershell
uv run --no-sync python tools/migrate_memory_lifecycle.py --sqlite-path "D:/AgentBot/data/agent_bot.sqlite3"
uv run --no-sync python tools/migrate_memory_lifecycle.py --sqlite-path "D:/AgentBot/data/agent_bot.sqlite3" --apply
uv run --no-sync python tools/migrate_memory_lifecycle.py --sqlite-path "D:/AgentBot/data/agent_bot.sqlite3" --verify-only
```

也可以通过已有启动文件选择 MySQL 或 SQLite；MySQL 启动文件必须明确设置 `CHAT_DB_BACKEND=mysql`。
命令不会修改启动文件，不在命令行参数里填写数据库密码：

```powershell
uv run --no-sync python tools/migrate_memory_lifecycle.py --env-file "D:/AgentBot/.env"
uv run --no-sync python tools/migrate_memory_lifecycle.py --env-file "D:/AgentBot/.env" --apply
uv run --no-sync python tools/migrate_memory_lifecycle.py --env-file "D:/AgentBot/.env" --verify-only
```

`--sqlite-path` 与 `--env-file` 必须二选一。默认仅预览，也可显式传 `--dry-run`；
预览、`--apply`、`--verify-only` 分开执行，不混合这些动作。该工具需要 Python 3.12 与本版依赖，
不能把 Python 文件直接交给桌面 EXE 执行。使用发布 wheel 时，可按 0.5.4 升级说明中的
`uv run --no-project --python 3.12 --with "<配套wheel绝对路径>" python "<工具绝对路径>" ...` 方式运行，
不要混用其他版本的工具、wheel 与 SQL。

若运维流程要求先人工执行 DDL，可选用 `2026-10-20-memory-lifecycle.sql`（MySQL）或
`2026-10-20-memory-lifecycle.sqlite.sql`（SQLite），**结构增量仅执行一次**；
随后仍需工具的默认预览、`--apply` 与 `--verify-only` 完成数据快照和摘要归档，不能只执行 DDL 就启动。
MySQL DDL 有隐式提交，不能认为一次失败会撤销已经完成的所有结构步骤；工具支持停服状态下修复原因后重跑，
但不应重复执行已经执行过的原始 ALTER 脚本。普通服务启动只验证结构，不执行本迁移。

## 数据与行为影响

- 旧事实保留 ID、键、正文、标签、素材引用、原始创建/修改时间和遗忘墓碑。
  首次迁移建立 revision=1；来源标为 `legacy_unknown`，事实时间未知，不能视为本人新近确认。
  原时间的时区不能证明时保留原值并标记 `legacy_unspecified`，不擅自转换为 UTC。
- 旧未遗忘事实继续以较低来源支持度参与授权召回；不会在升级时自动合并“413 网关报错”等重复主题，
  真实历史整理需要管理员逐项核对合并或清理预览后再提交。
- 旧纯文本摘要原样保存到 `legacy_memory_summary`，原默认摘要清空，结构化摘要从空记录开始。
  **旧纯文本摘要停止默认注入模型**，避免把无法确认主体和有效性的旧结论继续当作当前事实。
  因此升级后模型会暂时缺少这部分压缩背景；后续摘要根据真实消息建立，不将旧摘要包装成新证据。
- 重跑已完成的数据迁移不覆盖后续生成的新版本和新摘要。版本、证据、操作回执和向量缓存不占普通事实名额，
  但会增加数据库存储；本轮没有自动删除历史或自动清理缓存任务。
- Embedding 模型别名与供应商 URL、密钥保存在运行配置数据库；模型变更或事实修订后按版本重新生成所需向量。
  热缓存通常只请求查询向量，冷缓存还会分批请求候选正文向量；上游失败明确报错，不自动切换其他模型。
- 遗忘停止长期记忆召回并阻止受关联旧摘要复述，管理员审计历史仍保留。
  遗忘不表示物理擦除所有聊天原文；本次升级也不删除任何旧业务表或媒体原件。

## 核验与回退

迁移报告与 `--verify-only` 通过后，仍应在隔离副本完成一次来源、并发修订、遗忘和混合召回验收，
再操作真实部署。核验失败时保持停服，按明确错误处理，不新建空库或重生成密钥来绕过问题。

回退需要先停服，再恢复升级前程序、启动文件和同一时刻的数据库及媒体备份。
只安装旧程序不会撤销结构变更，也不能安全地让旧程序直接读写已升级数据库。
保留既有 0.3.0 wheel 用于隔离回退验证，不覆盖或删除它；实际部署回退基线以本次升级前的版本和备份为准。
