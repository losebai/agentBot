-- dispatch-chat Redis 对话队列默认配置（MySQL）
-- 本文件幂等补齐字段且不会覆盖已有值；Redis 连接凭据仍只从启动环境读取。

SELECT GET_LOCK('dispatch_chat_setting', 5);
START TRANSACTION;

INSERT INTO `dispatch_chat_setting` (`config_key`, `config_value`, `description`) VALUES
    ('dispatch_chat_queue_max_pending', '1000', 'Redis 对话队列最大待处理任务数'),
    ('dispatch_chat_queue_workers', '4', 'Redis 对话队列文本并发工作数'),
    ('dispatch_chat_queue_media_workers', '1', 'Redis 对话队列媒体并发工作数'),
    ('dispatch_chat_queue_max_attempts', '3', '对话任务最大尝试次数'),
    ('dispatch_chat_queue_retry_initial_seconds', '2', '对话任务首次重试等待秒数'),
    ('dispatch_chat_queue_retry_max_seconds', '30', '对话任务最大重试等待秒数'),
    ('dispatch_chat_queue_job_ttl_seconds', '900', '对话任务最长存活秒数'),
    ('dispatch_chat_queue_result_ttl_seconds', '1800', '已生成待发送结果存活秒数')
ON DUPLICATE KEY UPDATE
    `description` = VALUES(`description`);

UPDATE `dispatch_chat_setting_revision`
SET `revision` = `revision` + 1
WHERE `id` = 1;

COMMIT;
SELECT RELEASE_LOCK('dispatch_chat_setting');
