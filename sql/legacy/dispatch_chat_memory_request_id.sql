-- 已有 dispatch-chat 记忆表的幂等升级：保留全部旧历史，不回填请求编号。
SET @dispatch_chat_request_column_exists := (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'dispatch_chat_memory_turn' AND COLUMN_NAME = 'request_id'
);
SET @dispatch_chat_request_column_sql := IF(
    @dispatch_chat_request_column_exists = 0,
    'ALTER TABLE `dispatch_chat_memory_turn` ADD COLUMN `request_id` VARCHAR(128) COLLATE utf8mb4_bin NULL COMMENT ''幂等请求标识，旧轮次为空''',
    'SELECT 1'
);
PREPARE dispatch_chat_request_column_stmt FROM @dispatch_chat_request_column_sql;
EXECUTE dispatch_chat_request_column_stmt;
DEALLOCATE PREPARE dispatch_chat_request_column_stmt;

SET @dispatch_chat_request_index_exists := (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'dispatch_chat_memory_turn'
      AND INDEX_NAME = 'uk_dispatch_chat_memory_turn_request'
);
SET @dispatch_chat_request_index_sql := IF(
    @dispatch_chat_request_index_exists = 0,
    'ALTER TABLE `dispatch_chat_memory_turn` ADD UNIQUE KEY `uk_dispatch_chat_memory_turn_request` (`session_id`, `request_id`)',
    'SELECT 1'
);
PREPARE dispatch_chat_request_index_stmt FROM @dispatch_chat_request_index_sql;
EXECUTE dispatch_chat_request_index_stmt;
DEALLOCATE PREPARE dispatch_chat_request_index_stmt;

-- 不建立会话外键，清空或过期历史后仍需拦截旧任务重试。
CREATE TABLE IF NOT EXISTS `dispatch_chat_memory_receipt` (
    `session_id` VARCHAR(128) NOT NULL COMMENT 'HMAC 会话标识',
    `request_id` VARCHAR(128) COLLATE utf8mb4_bin NOT NULL COMMENT '幂等请求标识',
    `expires_at` DATETIME(6) NOT NULL COMMENT '独立重试去重截止时间',
    PRIMARY KEY (`session_id`, `request_id`),
    KEY `idx_dispatch_chat_memory_receipt_expires` (`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='dispatch-chat 已提交请求回执';
