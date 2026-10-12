-- 图生图兼容迁移：只补齐列，不改工作流数据和启用状态，无外键。
SET @workflow_image_sql = IF(
    EXISTS(SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE()
        AND TABLE_NAME = 'dispatch_chat_workflow' AND COLUMN_NAME = 'workflow_type'),
    'SELECT 1',
    'ALTER TABLE `dispatch_chat_workflow` ADD COLUMN `workflow_type` VARCHAR(16) NOT NULL DEFAULT ''text_to_image'' COMMENT ''工作流类型'''
);
PREPARE workflow_image_statement FROM @workflow_image_sql;
EXECUTE workflow_image_statement;
DEALLOCATE PREPARE workflow_image_statement;

SET @workflow_image_sql = IF(
    EXISTS(SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE()
        AND TABLE_NAME = 'dispatch_chat_workflow' AND COLUMN_NAME = 'input_image_node_id'),
    'SELECT 1',
    'ALTER TABLE `dispatch_chat_workflow` ADD COLUMN `input_image_node_id` VARCHAR(64) NULL COMMENT ''用户原图 LoadImage 节点 ID'''
);
PREPARE workflow_image_statement FROM @workflow_image_sql;
EXECUTE workflow_image_statement;
DEALLOCATE PREPARE workflow_image_statement;

SET @workflow_image_sql = IF(
    EXISTS(SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE()
        AND TABLE_NAME = 'dispatch_chat_workflow' AND COLUMN_NAME = 'positive_input_name'),
    'SELECT 1',
    'ALTER TABLE `dispatch_chat_workflow` ADD COLUMN `positive_input_name` VARCHAR(64) NOT NULL DEFAULT ''text'' COMMENT ''正向或编辑指令字段名'''
);
PREPARE workflow_image_statement FROM @workflow_image_sql;
EXECUTE workflow_image_statement;
DEALLOCATE PREPARE workflow_image_statement;

SET @workflow_image_sql = IF(
    EXISTS(SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE()
        AND TABLE_NAME = 'dispatch_chat_workflow' AND COLUMN_NAME = 'negative_input_name'),
    'SELECT 1',
    'ALTER TABLE `dispatch_chat_workflow` ADD COLUMN `negative_input_name` VARCHAR(64) NOT NULL DEFAULT ''text'' COMMENT ''负向提示词字段名'''
);
PREPARE workflow_image_statement FROM @workflow_image_sql;
EXECUTE workflow_image_statement;
DEALLOCATE PREPARE workflow_image_statement;
