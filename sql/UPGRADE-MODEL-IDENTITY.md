# 模型身份显式升级

适用起点：已经完成当前 0.5.4 结构升级、模型表仍要求全局别名的业务库。
新数据库直接使用 `agent_bot_init.sql` 或 `agent_bot_init.sqlite.sql`，无需运行增量。
普通服务启动不执行本升级。不要在正在写入的业务库执行迁移。

1. 停止服务，备份实际选中的业务数据库；SQLite 使用备份API或停服后复制完整文件。
2. 先在隔离副本预览并执行、核验，再处理原库。迁移不会创建不存在的SQLite文件。
3. 显式选择目标，默认预览；`--env-file` 沿用启动环境覆盖规则，相对SQLite路径按启动文件目录解析。

```powershell
uv run --no-sync python tools/migrate_model_identity.py --sqlite-path D:/backup/business.db
uv run --no-sync python tools/migrate_model_identity.py --sqlite-path D:/backup/business.db --apply
uv run --no-sync python tools/migrate_model_identity.py --sqlite-path D:/backup/business.db --verify-only

# MySQL或显式启动文件同样先预览，再执行、核验；不要使用未核对的默认启动配置。
uv run --no-sync python tools/migrate_model_identity.py --env-file D:/deployment/startup.env
uv run --no-sync python tools/migrate_model_identity.py --env-file D:/deployment/startup.env --apply
uv run --no-sync python tools/migrate_model_identity.py --env-file D:/deployment/startup.env --verify-only
```

迁移预检先于DDL：同一供应商同一上游模型有多条记录、旧别名占用其他ID的
`model:<十进制正整数ID>` 命名空间、非空模型配置引用不存在或配置版本无效，都会明确拒绝。
冲突报告包含模型记录ID；管理员应先核对备份和原始绑定，工具不会合并、删除记录或选取默认模型。

升级后 `alias` 允许 `NULL`，原非空值与 `uq_model_alias` 唯一索引保留供旧数据兼容。
新业务唯一索引是 `uq_model_provider_upstream(provider_id, upstream_model)`；
MySQL 使用原 `utf8mb4_bin`，SQLite使用BINARY，因此 `Model` 和 `model` 是不同上游标识。
既有ID、Agent外键、媒体能力、供应商密钥、运行历史与记忆数据不变。

只转换 `chat_model`、`chat_vision_model`、`chat_image_model`、`chat_tts_model`、
`chat_memory_embedding_model` 中可明确解析的旧别名，值变为原ID的 `model:<id>`。
空值、空字符串及已经稳定的引用保留。其他配置行不改；有引用变更时，仅在同一事务内
把已有 `chat_setting_revision` 推进一次；全部已升级时重跑不推进版本。

SQLite只重建模型单表，保留完整原DDL中的其他列与约束、原索引与触发器、全部记录和自增高水位；
事务提交前比较完整记录并检查全库外键。失败回滚结构与配置，不重建数据库。
MySQL DDL会隐式提交，后续配置失败可能留下已升级结构；直接重新运行同一CLI补齐，
不会覆盖已升级引用或重复推进配置版本。执行前必须停服，配置版本锁不能阻止模型目录的并发管理写入。

双后端 `2026-10-20-model-identity*.sql` 是独立结构增量，不能代替CLI的引用预检与转换。
优先使用CLI统一升级；如维护流程必须先执行SQL，也应先用CLI在隔离副本预检，执行后用
CLI `--apply` 完成配置升级，再用 `--verify-only` 确认结构、引用及外键。
不要仅执行结构SQL就启动新服务。日期SQL虽被Git忽略，wheel和源码包仍显式收集，缺失会构建失败。
