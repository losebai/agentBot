CREATE TABLE IF NOT EXISTS `dispatch_chat_workflow` (
    `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键',
    `workflow_key` VARCHAR(128) NOT NULL COMMENT '稳定工作流键',
    `display_name` VARCHAR(256) NOT NULL COMMENT '工作流显示名称',
    `source_file` VARCHAR(512) NOT NULL COMMENT '导入来源文件名',
    `source_format` VARCHAR(16) NOT NULL COMMENT '来源格式：ui 或 api',
    `ui_json` MEDIUMTEXT NULL COMMENT 'ComfyUI 画布工作流 JSON',
    `api_json` MEDIUMTEXT NULL COMMENT '可提交给 ComfyUI 的 API 工作流 JSON',
    `positive_node_id` VARCHAR(64) NULL COMMENT '正向提示词节点 ID',
    `negative_node_id` VARCHAR(64) NULL COMMENT '负向提示词节点 ID',
    `output_node_id` VARCHAR(64) NULL COMMENT '图片输出节点 ID',
    `seed_node_id` VARCHAR(64) NULL COMMENT '随机种子所在节点 ID',
    `workflow_type` VARCHAR(16) NOT NULL DEFAULT 'text_to_image' COMMENT '工作流类型',
    `input_image_node_id` VARCHAR(64) NULL COMMENT '用户原图 LoadImage 节点 ID',
    `positive_input_name` VARCHAR(64) NOT NULL DEFAULT 'text' COMMENT '正向或编辑指令字段名',
    `negative_input_name` VARCHAR(64) NOT NULL DEFAULT 'text' COMMENT '负向提示词字段名',
    `description` VARCHAR(2000) NOT NULL DEFAULT '' COMMENT '供 Agent 选择工作流的用途描述',
    `additional_positive_prompt` VARCHAR(2000) NOT NULL DEFAULT '' COMMENT '生成时固定追加的正向提示词',
    `additional_negative_prompt` VARCHAR(2000) NOT NULL DEFAULT '' COMMENT '生成时固定追加的负向提示词',
    `enabled` TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否允许 Agent 执行',
    `content_sha256` CHAR(64) NOT NULL COMMENT '规范化来源内容 SHA-256',
    `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `update_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_dispatch_chat_workflow_key` (`workflow_key`),
    UNIQUE KEY `uk_dispatch_chat_workflow_content_sha256` (`content_sha256`),
    KEY `idx_dispatch_chat_workflow_enabled` (`enabled`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='本地 Comfy 工作流定义与执行绑定';

-- 为已经执行过旧版本脚本的环境幂等补齐元数据列。
SET @workflow_migration_sql = IF(
    EXISTS(
        SELECT 1
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = 'dispatch_chat_workflow'
            AND COLUMN_NAME = 'description'
    ),
    'SELECT 1',
    'ALTER TABLE `dispatch_chat_workflow` ADD COLUMN `description` VARCHAR(2000) NOT NULL DEFAULT '''' COMMENT ''供 Agent 选择工作流的用途描述'''
);
PREPARE workflow_migration_statement FROM @workflow_migration_sql;
EXECUTE workflow_migration_statement;
DEALLOCATE PREPARE workflow_migration_statement;

SET @workflow_migration_sql = IF(
    EXISTS(
        SELECT 1
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = 'dispatch_chat_workflow'
            AND COLUMN_NAME = 'additional_positive_prompt'
    ),
    'SELECT 1',
    'ALTER TABLE `dispatch_chat_workflow` ADD COLUMN `additional_positive_prompt` VARCHAR(2000) NOT NULL DEFAULT '''' COMMENT ''生成时固定追加的正向提示词'''
);
PREPARE workflow_migration_statement FROM @workflow_migration_sql;
EXECUTE workflow_migration_statement;
DEALLOCATE PREPARE workflow_migration_statement;

SET @workflow_migration_sql = IF(
    EXISTS(
        SELECT 1
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = 'dispatch_chat_workflow'
            AND COLUMN_NAME = 'additional_negative_prompt'
    ),
    'SELECT 1',
    'ALTER TABLE `dispatch_chat_workflow` ADD COLUMN `additional_negative_prompt` VARCHAR(2000) NOT NULL DEFAULT '''' COMMENT ''生成时固定追加的负向提示词'''
);
PREPARE workflow_migration_statement FROM @workflow_migration_sql;
EXECUTE workflow_migration_statement;
DEALLOCATE PREPARE workflow_migration_statement;

-- 统一旧表字段定义和注释，不改变工作流内容。
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
ALTER TABLE `dispatch_chat_workflow`
    MODIFY COLUMN `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键',
    MODIFY COLUMN `workflow_key` VARCHAR(128) NOT NULL COMMENT '稳定工作流键',
    MODIFY COLUMN `display_name` VARCHAR(256) NOT NULL COMMENT '工作流显示名称',
    MODIFY COLUMN `source_file` VARCHAR(512) NOT NULL COMMENT '导入来源文件名',
    MODIFY COLUMN `source_format` VARCHAR(16) NOT NULL COMMENT '来源格式：ui 或 api',
    MODIFY COLUMN `ui_json` MEDIUMTEXT NULL COMMENT 'ComfyUI 画布工作流 JSON',
    MODIFY COLUMN `api_json` MEDIUMTEXT NULL COMMENT '可提交给 ComfyUI 的 API 工作流 JSON',
    MODIFY COLUMN `positive_node_id` VARCHAR(64) NULL COMMENT '正向提示词节点 ID',
    MODIFY COLUMN `negative_node_id` VARCHAR(64) NULL COMMENT '负向提示词节点 ID',
    MODIFY COLUMN `output_node_id` VARCHAR(64) NULL COMMENT '图片输出节点 ID',
    MODIFY COLUMN `seed_node_id` VARCHAR(64) NULL COMMENT '随机种子所在节点 ID',
    MODIFY COLUMN `workflow_type` VARCHAR(16) NOT NULL DEFAULT 'text_to_image' COMMENT '工作流类型',
    MODIFY COLUMN `input_image_node_id` VARCHAR(64) NULL COMMENT '用户原图 LoadImage 节点 ID',
    MODIFY COLUMN `positive_input_name` VARCHAR(64) NOT NULL DEFAULT 'text' COMMENT '正向或编辑指令字段名',
    MODIFY COLUMN `negative_input_name` VARCHAR(64) NOT NULL DEFAULT 'text' COMMENT '负向提示词字段名',
    MODIFY COLUMN `description` VARCHAR(2000) NOT NULL DEFAULT '' COMMENT '供 Agent 选择工作流的用途描述',
    MODIFY COLUMN `additional_positive_prompt` VARCHAR(2000) NOT NULL DEFAULT '' COMMENT '生成时固定追加的正向提示词',
    MODIFY COLUMN `additional_negative_prompt` VARCHAR(2000) NOT NULL DEFAULT '' COMMENT '生成时固定追加的负向提示词',
    MODIFY COLUMN `enabled` TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否允许 Agent 执行',
    MODIFY COLUMN `content_sha256` CHAR(64) NOT NULL COMMENT '规范化来源内容 SHA-256',
    MODIFY COLUMN `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    MODIFY COLUMN `update_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    COMMENT='本地 Comfy 工作流定义与执行绑定';

-- 历史重复内容保留最早 ID；只要任一重复记录被禁用，保留记录也保持禁用。
UPDATE `dispatch_chat_workflow` AS retained
INNER JOIN (
    SELECT `content_sha256`, MIN(`id`) AS `keep_id`,
        MIN(`enabled`) AS `retained_enabled`
    FROM `dispatch_chat_workflow`
    GROUP BY `content_sha256`
    HAVING COUNT(*) > 1
) AS duplicate_group ON retained.`id` = duplicate_group.`keep_id`
SET retained.`enabled` = duplicate_group.`retained_enabled`;

DELETE duplicate_row
FROM `dispatch_chat_workflow` AS duplicate_row
INNER JOIN `dispatch_chat_workflow` AS retained
    ON retained.`content_sha256` = duplicate_row.`content_sha256`
    AND retained.`id` < duplicate_row.`id`;

-- 去重完成后幂等建立内容身份唯一索引。
SET @workflow_migration_sql = IF(
    (
        SELECT 1
        FROM INFORMATION_SCHEMA.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = 'dispatch_chat_workflow'
            AND INDEX_NAME = 'uk_dispatch_chat_workflow_content_sha256'
            AND NON_UNIQUE = 0
            AND SEQ_IN_INDEX = 1
            AND COLUMN_NAME = 'content_sha256'
            AND SUB_PART IS NULL
            AND (
                SELECT COUNT(*)
                FROM INFORMATION_SCHEMA.STATISTICS AS index_columns
                WHERE index_columns.TABLE_SCHEMA = DATABASE()
                    AND index_columns.TABLE_NAME = 'dispatch_chat_workflow'
                    AND index_columns.INDEX_NAME =
                        'uk_dispatch_chat_workflow_content_sha256'
            ) = 1
    ),
    'SELECT 1',
    'ALTER TABLE `dispatch_chat_workflow` ADD UNIQUE KEY `uk_dispatch_chat_workflow_content_sha256` (`content_sha256`)'
);
PREPARE workflow_migration_statement FROM @workflow_migration_sql;
EXECUTE workflow_migration_statement;
DEALLOCATE PREPARE workflow_migration_statement;
