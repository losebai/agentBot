-- dispatch-chat 本地内容分类模型默认配置（MySQL）
-- 本文件幂等补齐独立模型字段且不会覆盖已有值。

SELECT GET_LOCK('dispatch_chat_setting', 5);
START TRANSACTION;

INSERT INTO `dispatch_chat_setting` (`config_key`, `config_value`, `description`) VALUES
    ('dispatch_chat_vision_model', '"Qwen/Qwen2.5-VL-7B-Instruct"', '图片理解模型名称'),
    ('dispatch_chat_image_model', '"gpt-image-1"', '图片生成模型名称'),
    ('dispatch_chat_tts_model', '"tts-1"', '原生音频生成模型名称')
ON DUPLICATE KEY UPDATE
    `description` = VALUES(`description`);

UPDATE `dispatch_chat_setting_revision`
SET `revision` = `revision` + 1
WHERE `id` = 1;

COMMIT;
SELECT RELEASE_LOCK('dispatch_chat_setting');
