-- dispatch-chat 当前群最近聊天上下文默认配置（MySQL）
-- 本文件幂等补齐字段且不会覆盖已有值；执行后默认仍为关闭状态。

SELECT GET_LOCK('dispatch_chat_setting', 5);
START TRANSACTION;

INSERT INTO `dispatch_chat_setting` (`config_key`, `config_value`, `description`) VALUES
    ('dispatch_chat_group_recent_context_enabled', 'false', '是否读取当前群最近普通文本'),
    ('dispatch_chat_group_recent_context_messages', '10', '当前群最近文本消息条数'),
    ('dispatch_chat_group_recent_context_max_chars', '4000', '当前群最近文本最大字符数'),
    ('dispatch_chat_group_recent_context_ttl_seconds', '604800', '当前群最近文本存活时间（秒）')
ON DUPLICATE KEY UPDATE
    `description` = VALUES(`description`);

UPDATE `dispatch_chat_setting_revision`
SET `revision` = `revision` + 1
WHERE `id` = 1;

COMMIT;
SELECT RELEASE_LOCK('dispatch_chat_setting');
