-- 聊天调用链路资源配置；只补齐默认值，保留已有配置值。
-- GET_LOCK 必须返回 1 后才继续执行事务。
SELECT GET_LOCK('dispatch_chat_setting', 5);
START TRANSACTION;

INSERT INTO `dispatch_chat_setting` (`config_key`, `config_value`, `description`) VALUES
    ('dispatch_chat_memory_compression_queue_capacity', '64', '后台记忆压缩待处理会话上限'),
    ('dispatch_chat_db_pool_size', '8', 'MySQL 连接池最大连接数，修改后重启'),
    ('dispatch_chat_db_timeout_seconds', '10', 'MySQL 连接、读写和池等待超时秒数，修改后重启'),
    ('dispatch_chat_model_cache_ttl_seconds', '5', '模型启用校验成功缓存秒数，0 表示关闭'),
    ('dispatch_chat_queue_media_workers', '1', 'Redis 对话队列媒体并发工作数，修改后重启')
ON DUPLICATE KEY UPDATE `description` = VALUES(`description`);

UPDATE `dispatch_chat_setting_revision`
SET `revision` = `revision` + 1
WHERE `id` = 1;

COMMIT;
SELECT RELEASE_LOCK('dispatch_chat_setting');
