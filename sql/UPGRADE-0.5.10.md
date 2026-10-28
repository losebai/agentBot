# AgentBot 0.5.10 升级说明

适用起点：已正常完成此前升级的下载站 0.5.9 部署。
相较已发布的 0.5.9，本版包含 Comfy 插件选择迁移和素材分类、标签关联表增量。
迁移清单中两个步骤的既有目标版本均保持 0.5.9，最终程序版本登记为 0.5.10；
不修改已应用流水，不重放已经完成的步骤。

## 升级顺序与数据影响

1. 停止旧实例，备份 `.env`、业务数据库、独立队列库及媒体目录。
   SQLite 备份应包含 WAL 中的数据并保持一致。
2. 安装 0.5.10，保留已有配置、密钥和数据库，再手动启动后端。
   程序读取配置前按明确清单自动升级；也可停服后先运行 `agent-bot database upgrade`。
3. 在已完成 B站表等历史结构的基础上，先执行 `comfy-plugin-v1`：对应后端的
   `2026-10-28-comfy-plugin.sql` / `2026-10-28-comfy-plugin.sqlite.sql`。
   原本开启本地工作流的部署会在原插件列表补入 `comfy`，并增加配置 revision；
   工作流、默认选择和连接配置保留。新库默认停用 Comfy，后续主动停用不会被再次开启。
4. 再执行 `material-classification-v1`：`material-classification.sql` /
   `material-classification.sqlite.sql`，仅新增 `chat_material_category`、
   `chat_material_tag` 两张关联表、索引和外键，不猜测或回填历史素材分类和标签。
   独立 SQLite 队列不执行这些业务库增量。不要重复初始化或批量执行历史 SQL。
5. 插件配置入口移至“插件管理”：QQ、B站和 Comfy 各自配置，仅运行中的插件开放所属工具。
   核对原插件启用选择及 Agent 工具授权；插件也可通过 `plugin run` 在独立工作目录运行。
   调用记录改用页码分页；本版还修复单条 JSON 回复解析、上游错误分类及 B站凭据日志处理。
6. 核对机器人连接、工作流、素材筛选、调用记录及 B站功能。
   SQLite 升级失败整体回滚；MySQL DDL 可能保留已完成结构，失败停止启动并保留流水。

新库只执行对应后端的 `agent_bot_init.sql` 或 `agent_bot_init.sqlite.sql`，
其中包含当前完整结构；独立队列使用 `agent_bot_queue_init.sqlite.sql`。
发布包同时提供两种业务库的完整初始化、适用增量、升级说明及历史工具。
更早部署先核对原版本历史迁移，自动升级不补做未登记的数据转换。
详细重入、锁和失败处理见 [自动升级说明](UPGRADE-AUTOMATIC.md)。

回退前停止程序并核对数据库版本兼容性；完整回退使用升级前同一时刻的一致备份。
