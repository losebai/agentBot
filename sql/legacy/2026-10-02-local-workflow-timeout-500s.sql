-- dispatch-chat 本地 Comfy 工作流总超时调整为 500 秒（MySQL）。
-- 先部署支持 500 秒的新版本，再执行本文件；旧版本校验上限为 300 秒。
-- 仅调整绘图工作流，AI 对话超时不受影响。

SELECT GET_LOCK('dispatch_chat_setting', 5);
START TRANSACTION;

INSERT INTO `dispatch_chat_setting` (`config_key`, `config_value`, `description`) VALUES
    ('dispatch_chat_local_workflow_timeout_seconds', '500', '本地 Comfy 工作流总超时秒数')
ON DUPLICATE KEY UPDATE
    `config_value` = VALUES(`config_value`),
    `description` = VALUES(`description`);

UPDATE `dispatch_chat_setting_revision`
SET `revision` = `revision` + 1
WHERE `id` = 1;

COMMIT;
SELECT RELEASE_LOCK('dispatch_chat_setting');
