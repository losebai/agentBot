-- 防止旧后台压缩结果写入重置或过期后重建的同名会话，保留全部旧历史。
SET @dispatch_chat_generation_exists := (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'dispatch_chat_memory_session'
      AND COLUMN_NAME = 'generation_id'
);
SET @dispatch_chat_generation_add_sql := IF(
    @dispatch_chat_generation_exists = 0,
    'ALTER TABLE `dispatch_chat_memory_session` ADD COLUMN `generation_id` CHAR(32) NULL COMMENT ''会话代际标识，重建时重新生成''',
    'SELECT 1'
);
PREPARE dispatch_chat_generation_add_stmt FROM @dispatch_chat_generation_add_sql;
EXECUTE dispatch_chat_generation_add_stmt;
DEALLOCATE PREPARE dispatch_chat_generation_add_stmt;

-- 每行独立生成 UUID；再次执行不会修改已存在的代际标识。
UPDATE `dispatch_chat_memory_session`
SET `generation_id` = REPLACE(UUID(), '-', '')
WHERE `generation_id` IS NULL;

SET @dispatch_chat_generation_nullable := (
    SELECT IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'dispatch_chat_memory_session'
      AND COLUMN_NAME = 'generation_id'
);
SET @dispatch_chat_generation_required_sql := IF(
    @dispatch_chat_generation_nullable = 'YES',
    'ALTER TABLE `dispatch_chat_memory_session` MODIFY COLUMN `generation_id` CHAR(32) NOT NULL COMMENT ''会话代际标识，重建时重新生成''',
    'SELECT 1'
);
PREPARE dispatch_chat_generation_required_stmt FROM @dispatch_chat_generation_required_sql;
EXECUTE dispatch_chat_generation_required_stmt;
DEALLOCATE PREPARE dispatch_chat_generation_required_stmt;
