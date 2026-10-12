-- dispatch-chat Laya 自动发言触发模式默认配置 (MySQL)
-- 本文件幂等补齐字段且不会覆盖已有值；默认继续使用 interval 模式。

SELECT GET_LOCK('dispatch_chat_setting', 5);
START TRANSACTION;

INSERT INTO `dispatch_chat_setting` (`config_key`, `config_value`, `description`) VALUES
    (
        'dispatch_chat_auto_speak_trigger_mode',
        '"interval"',
        '自动发言触发模式 (interval 或 message)'
    )
ON DUPLICATE KEY UPDATE
    `description` = VALUES(`description`);

UPDATE `dispatch_chat_setting_revision`
SET `revision` = `revision` + 1
WHERE `id` = 1;

COMMIT;
SELECT RELEASE_LOCK('dispatch_chat_setting');
