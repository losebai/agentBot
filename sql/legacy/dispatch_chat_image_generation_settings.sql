-- dispatch-chat 自然语言图片生成默认配置（MySQL）
-- 本文件幂等补齐字段且不会覆盖已有值；执行后默认仍为关闭状态。

SELECT GET_LOCK('dispatch_chat_setting', 5);
START TRANSACTION;

INSERT INTO `dispatch_chat_setting` (`config_key`, `config_value`, `description`) VALUES
    ('dispatch_chat_image_generation_enabled', 'false', '是否启用自然语言图片生成'),
    ('dispatch_chat_image_timeout_seconds', '60', '图片生成请求超时秒数'),
    ('dispatch_chat_image_response_format', '"png"', '图片生成输出格式'),
    ('dispatch_chat_image_max_bytes', '10485760', '单张生成图片最大字节数')
ON DUPLICATE KEY UPDATE
    `description` = VALUES(`description`);

UPDATE `dispatch_chat_setting_revision`
SET `revision` = `revision` + 1
WHERE `id` = 1;

COMMIT;
SELECT RELEASE_LOCK('dispatch_chat_setting');
