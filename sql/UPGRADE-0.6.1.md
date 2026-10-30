# AgentBot 0.6.1 升级说明

适用起点：已正常完成此前升级的下载站 0.6.0 部署。
相较已发布的 0.6.0，本版新增长期互动演化所需结构及旧数据兼容迁移。
迁移清单中两个演化步骤的既有目标版本保持 0.6.0，最终程序版本登记为 0.6.1；
已完成流水不重复执行，不修改历史迁移校验和。

## 升级顺序与数据影响

1. 停止旧实例，备份 `.env`、业务数据库、独立队列库及媒体目录。
   SQLite 备份应包含 WAL 中的数据并保持一致。
2. 安装 0.6.1，保留已有配置、密钥及数据，再手动启动后端。
   程序读取运行配置前按明确清单升级；也可停服后先运行 `agent-bot database upgrade`。
3. 完成既有历史增量后，先执行 `agent-evolution-v1`：对应后端的
   `agent-evolution.sql` / `agent-evolution.sqlite.sql`，新增演化配置、画像及事件表。
4. 再执行 `evolution-independent-v2`：`evolution-independent.sql` /
   `evolution-independent.sqlite.sql`，新增范围、状态及记录表，并按稳定键复制旧演化数据。
   旧表、画像和事件 ID、证据引用保留；不改写角色、凭据、聊天历史或记忆。
   独立 SQLite 队列不执行这些业务库增量，不重跑完整初始化或批量执行历史 SQL。
5. 核对长期互动演化的范围、学习与应用开关、状态和记录，以及原有 Agent 工具授权。
   SQLite 升级失败整体回滚；MySQL DDL 可能保留已完成结构，失败停止启动并保留流水。
6. Comfy 插件新增本机启动入口，需先在启动文件配置 `AGENT_BOT_COMFY_PYTHON`、
   `AGENT_BOT_COMFY_DIRECTORY`、`AGENT_BOT_COMFY_ARGS`，重启后从本机同源管理页面发起。
   页面只发送无参数启动意图，不接受网页命令，不接管或关闭外部 ComfyUI。
   本机 Python 和 ComfyUI 环境需自行准备，安装器不覆盖现有环境。
7. 本版还改进工作流处理；核对 Comfy 连接、原工作流和媒体功能。

新库只执行对应后端的 `agent_bot_init.sql` 或 `agent_bot_init.sqlite.sql`，
其中包含当前完整结构；独立队列使用 `agent_bot_queue_init.sqlite.sql`。
发布包同时提供 MySQL、SQLite 完整初始化、适用增量、升级说明及历史工具。
更早部署先完成原版本所需历史迁移；自动升级仅执行明确登记的适用步骤。
重入、锁和失败处理详见 [自动升级说明](UPGRADE-AUTOMATIC.md)。

回退前停止程序并核对数据库版本兼容性；完整回退使用升级前同一时刻的一致备份。
