-- 本地持久队列初始化：选用 SQLite 队列后，在处理任务之前执行。
-- 可重复执行，仅创建缺失的队列表和索引；不修改业务配置，不清空既有任务。
-- 与主配置数据库独立；图片元数据与入队记录共用事务，保证取消与恢复一致。

-- 待处理任务与重试、投递检查点；命名空间隔离文本和图片队列。
CREATE TABLE IF NOT EXISTS chat_queue_jobs (
    stream_id INTEGER PRIMARY KEY AUTOINCREMENT, -- 单调递增的持久任务顺序，删除后不复用
    namespace TEXT NOT NULL, -- 队列键前缀
    payload TEXT NOT NULL, -- 已验证的任务 JSON，不保存运行凭据
    claimed_at REAL, -- 最近领取时间；空值表示尚未领取
    state TEXT NOT NULL, -- 重试次数、可执行时间及投递位置 JSON
    result TEXT, -- 生成结果 JSON，恢复时复用以避免重复请求模型
    result_expires_at REAL -- 生成结果保留截止时间，空值表示无结果
);
-- 按队列顺序领取及恢复，不扫描其他队列的数据。
CREATE INDEX IF NOT EXISTS chat_queue_jobs_namespace
    ON chat_queue_jobs(namespace, stream_id);

-- 稳定任务标识的入队回执，活动任务持续保留，结束后按保留期过期。
CREATE TABLE IF NOT EXISTS chat_queue_receipts (
    namespace TEXT NOT NULL, -- 所属队列键前缀
    job_id TEXT NOT NULL, -- 应用层稳定任务标识
    digest TEXT NOT NULL, -- 参数摘要，禁止用相同标识替换任务内容
    stream_id INTEGER NOT NULL, -- 原始队列顺序；任务完成后仍用于重复入队响应
    expires_at REAL, -- 回执保留截止时间；空值表示活动任务不可过期
    PRIMARY KEY (namespace, job_id)
);
-- 有界清理已结束的过期回执。
CREATE INDEX IF NOT EXISTS chat_queue_receipts_expiry
    ON chat_queue_receipts(namespace, expires_at);

-- 最终失败或解析损坏的条目，按队列容量保留最近记录。
CREATE TABLE IF NOT EXISTS chat_queue_deadletters (
    id INTEGER PRIMARY KEY AUTOINCREMENT, -- 死信写入顺序
    namespace TEXT NOT NULL, -- 所属队列键前缀
    source_id INTEGER NOT NULL, -- 原始队列顺序
    payload TEXT, -- 合法失败任务的 JSON；损坏任务不复制不可信内容
    reason TEXT NOT NULL, -- 有界错误类型或原因，不保存敏感异常内容
    attempt INTEGER NOT NULL, -- 失败时的执行次数
    failed_at REAL NOT NULL -- 进入死信的时间
);
-- 支持按命名空间裁剪最旧死信。
CREATE INDEX IF NOT EXISTS chat_queue_deadletters_namespace
    ON chat_queue_deadletters(namespace, id);

-- 图片执行元数据；取消意图独立持久化，活动记录不自动过期。
CREATE TABLE IF NOT EXISTS chat_image_tasks (
    namespace TEXT NOT NULL, -- 图片队列键前缀
    task_id TEXT NOT NULL, -- 图片任务稳定标识
    owner TEXT NOT NULL, -- 机器人、会话和用户归属的规范 JSON
    created_at REAL NOT NULL, -- 创建时间，用于最近任务排序
    payload TEXT NOT NULL, -- 完整执行、投递、取消和上游标识 JSON
    expires_at REAL, -- 终态保留截止时间，活动任务为空
    PRIMARY KEY (namespace, task_id)
);
-- 精确按归属列出最近任务，防止跨用户泄露。
CREATE INDEX IF NOT EXISTS chat_image_tasks_owner
    ON chat_image_tasks(namespace, owner, created_at DESC);
-- 清理过期终态，避免本地任务元数据无限增长。
CREATE INDEX IF NOT EXISTS chat_image_tasks_expiry
    ON chat_image_tasks(namespace, expires_at);

-- 工具操作幂等记录，保留已提交副作用及其结果，防止任务重试重复提交。
CREATE TABLE IF NOT EXISTS chat_tool_operations (
    namespace TEXT NOT NULL, -- 服务键前缀
    job_id TEXT NOT NULL, -- 所属对话任务标识
    tool TEXT NOT NULL, -- 工具名称
    payload TEXT NOT NULL, -- 操作参数摘要与状态 JSON
    expires_at REAL NOT NULL, -- 操作记录保留截止时间
    PRIMARY KEY (namespace, job_id, tool)
);
-- 按命名空间清理已过期的幂等记录。
CREATE INDEX IF NOT EXISTS chat_tool_operations_expiry
    ON chat_tool_operations(namespace, expires_at);
