-- dispatch-chat Laya 自动发言判断指令默认配置 (MySQL)
-- 本文件幂等补齐配置且不会覆盖已有判断指令。

SELECT GET_LOCK('dispatch_chat_setting', 5);
START TRANSACTION;

INSERT INTO `dispatch_chat_setting` (`config_key`, `config_value`, `description`) VALUES
    (
        'dispatch_chat_auto_speak_laya_prompt',
        '"结合自动发言会话历史和当前群聊，判断现在是否适合主动发言；仅当发言自然、有明确价值且不会打扰群聊时选择 speak，否则选择 silent。"',
        '自动发言 Laya 判断指令'
    )
ON DUPLICATE KEY UPDATE
    `description` = VALUES(`description`);

UPDATE `dispatch_chat_setting_revision`
SET `revision` = `revision` + 1
WHERE `id` = 1;

COMMIT;
SELECT RELEASE_LOCK('dispatch_chat_setting');
