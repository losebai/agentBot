# AgentBot 0.5.5 数据库升级

适用起点为已完成 [0.5.4 数据库迁移](UPGRADE-0.5.4.md) 的 MySQL 或 SQLite 业务库。
0.5.5 增加记忆生命周期及模型身份升级；更早部署先完成相应旧版迁移。
普通启动和一键安装不会自动修改已有数据库结构或转换历史配置。

## 新安装

新数据库仅使用 `agent_bot_init.sql`（MySQL）或 `agent_bot_init.sqlite.sql`（SQLite）。
队列仍使用独立的 `agent_bot_queue_init.sqlite.sql`，不能把业务增量执行到队列库。
全新桌面部署首次启动会完成初始化，无需执行历史增量。

## 从 0.5.4 升级

1. 停止所有连接该业务库的新旧实例，备份启动文件、业务数据库、队列库和媒体目录。
   SQLite 备份须包含 WAL 中尚未合并的数据，不能仅在运行时复制主文件。
2. 安装 0.5.5，但暂不启动。使用同版 SQL 包、工具及 wheel；不要重新初始化已有库，
   不修改现有 OneBot Token、会话密钥或数据库后端。
3. 先按 [模型身份升级](UPGRADE-MODEL-IDENTITY.md) 运行默认预览、`--apply`、`--verify-only`。
   模型保留原 ID、外键和密钥，旧模型引用显式转换为 `model:<id>`。
   同供应商重复上游模型、无效引用等冲突必须先处理；工具不会自动删除或合并模型。
4. 再按 [记忆生命周期升级](UPGRADE-MEMORY-LIFECYCLE.md) 运行默认预览、`--apply`、`--verify-only`。
   工具补齐版本、证据、操作及向量缓存结构，保留原事实和遗忘墓碑，建立旧事实首版快照。
   旧纯文本摘要会归档并停止默认注入，后续摘要由真实消息重新建立。
5. 核对报告、记录数量及原始内容后启动 0.5.5，检查模型绑定、聊天、记忆版本和遗忘状态。
   已单独完成某项迁移的部署先核验；工具支持已升级结构，勿重复执行原始 ALTER 脚本。

两个迁移工具均默认只预览，必须显式指定 `--sqlite-path` 或 `--env-file`。
推荐由工具完成结构和数据升级，**不需要先执行同名结构 SQL**。
SQL 包同时提供两种数据库的独立增量；若维护流程先执行结构 SQL，仍需工具完成配置、
快照和摘要升级并核验，不能仅执行 DDL 就启动服务。

## 桌面版离线工具

两个工具需要 Python 3.12 与 uv，不能直接交给桌面 EXE 执行。
下载同目录的 `agent_bot-0.5.5-py3-none-any.whl` 与 SQL 包并解压后，替换下列绝对路径；
MySQL 或希望沿用启动文件的部署，把 `--sqlite-path` 换成 `--env-file "<启动文件绝对路径>"`。

```powershell
uv run --no-project --python 3.12 --with "<wheel绝对路径>" python "<SQL包解压目录>/tools/migrate_model_identity.py" --sqlite-path "<业务库绝对路径>"
uv run --no-project --python 3.12 --with "<wheel绝对路径>" python "<SQL包解压目录>/tools/migrate_model_identity.py" --sqlite-path "<业务库绝对路径>" --apply
uv run --no-project --python 3.12 --with "<wheel绝对路径>" python "<SQL包解压目录>/tools/migrate_model_identity.py" --sqlite-path "<业务库绝对路径>" --verify-only
uv run --no-project --python 3.12 --with "<wheel绝对路径>" python "<SQL包解压目录>/tools/migrate_memory_lifecycle.py" --sqlite-path "<业务库绝对路径>"
uv run --no-project --python 3.12 --with "<wheel绝对路径>" python "<SQL包解压目录>/tools/migrate_memory_lifecycle.py" --sqlite-path "<业务库绝对路径>" --apply
uv run --no-project --python 3.12 --with "<wheel绝对路径>" python "<SQL包解压目录>/tools/migrate_memory_lifecycle.py" --sqlite-path "<业务库绝对路径>" --verify-only
```

迁移不连接 QQ 或付费模型。语义召回需升级后另行显式配置 Embedding 模型；
模型及供应商凭据仍保存在运行配置数据库，不从环境变量覆盖。

## 回退

MySQL DDL 会隐式提交，迁移失败可能保留部分已完成结构，须按工具错误修复并核验。
回退必须先停服，恢复升级前程序、启动文件和同一时刻的数据库及媒体备份。
仅安装旧程序不能撤销结构与引用变更。旧版本安装器会保留，升级不会自动删除旧业务表。
