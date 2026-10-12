-- dispatch-chat 本地 Comfy 工作流默认配置（MySQL）
-- 本文件幂等补齐字段且不覆盖已有值；执行后本地模式仍为关闭状态。

SELECT GET_LOCK('dispatch_chat_setting', 5);
START TRANSACTION;

INSERT INTO `dispatch_chat_setting` (`config_key`, `config_value`, `description`) VALUES
    ('dispatch_chat_local_workflow_enabled', 'false', '是否使用本地 Comfy 工作流替代云端生图'),
    ('dispatch_chat_local_workflow_base_url', '"http://127.0.0.1:8188"', '本地 ComfyUI 服务根地址'),
    ('dispatch_chat_local_workflow_directory', 'null', '启动期工作流 JSON 导入目录'),
    ('dispatch_chat_local_workflow_default_key', '"sdxl-lightning-4step"', '本地生图默认工作流键'),
    ('dispatch_chat_local_workflow_timeout_seconds', '500', '本地 Comfy 工作流总超时秒数')
ON DUPLICATE KEY UPDATE
    `description` = VALUES(`description`);

UPDATE `dispatch_chat_setting_revision`
SET `revision` = `revision` + 1
WHERE `id` = 1;

COMMIT;
SELECT RELEASE_LOCK('dispatch_chat_setting');
