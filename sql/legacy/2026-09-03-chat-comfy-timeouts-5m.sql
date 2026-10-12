-- dispatch-chat 调用 dispatch-ai 与本地 Comfy 工作流的五分钟超时配置（MySQL）
-- 本次变更需要覆盖旧的 90 秒与 120 秒值，避免任一调用层提前中断。

SELECT GET_LOCK('dispatch_chat_setting', 5);
START TRANSACTION;

INSERT INTO `dispatch_chat_setting` (`config_key`, `config_value`, `description`) VALUES
    ('dispatch_chat_timeout_seconds', '300', 'AI 调用超时 (秒)'),
    ('dispatch_chat_local_workflow_timeout_seconds', '300', '本地 Comfy 工作流总超时秒数')
ON DUPLICATE KEY UPDATE
    `config_value` = VALUES(`config_value`),
    `description` = VALUES(`description`);

UPDATE `dispatch_chat_setting_revision`
SET `revision` = `revision` + 1
WHERE `id` = 1;

COMMIT;
SELECT RELEASE_LOCK('dispatch_chat_setting');
