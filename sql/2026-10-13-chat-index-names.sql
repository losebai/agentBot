-- 用途：修复旧 dispatch 表改名后遗留的索引名称，使记忆和工作流启动校验与初始化 SQL 一致。
-- 执行顺序：选中当前项目数据库后执行，可重复执行；新库直接使用 agent_bot_init.sql。
-- 数据影响：仅在旧索引存在且新名称不存在时原位重命名，不改字段、唯一性或索引顺序，不修改历史记录。
-- 新旧名称同时存在时保留已有索引，避免名称冲突；不通过删除索引修复命名问题。

-- chat_memory_session：沿用原索引的查询或唯一约束用途，只统一名称。
SET @chat_index_sql = IF(
  EXISTS(SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS
         WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_memory_session' AND INDEX_NAME = 'idx_dispatch_chat_memory_session_expires')
  AND NOT EXISTS(SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS
                 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_memory_session' AND INDEX_NAME = 'idx_chat_memory_session_expires'),
  'ALTER TABLE `chat_memory_session` RENAME INDEX `idx_dispatch_chat_memory_session_expires` TO `idx_chat_memory_session_expires`',
  'SELECT 1'
);
PREPARE chat_index_statement FROM @chat_index_sql;
EXECUTE chat_index_statement;
DEALLOCATE PREPARE chat_index_statement;

-- chat_memory_turn：沿用原索引的查询或唯一约束用途，只统一名称。
SET @chat_index_sql = IF(
  EXISTS(SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS
         WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_memory_turn' AND INDEX_NAME = 'idx_dispatch_chat_memory_turn_session_id')
  AND NOT EXISTS(SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS
                 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_memory_turn' AND INDEX_NAME = 'idx_chat_memory_turn_session_id'),
  'ALTER TABLE `chat_memory_turn` RENAME INDEX `idx_dispatch_chat_memory_turn_session_id` TO `idx_chat_memory_turn_session_id`',
  'SELECT 1'
);
PREPARE chat_index_statement FROM @chat_index_sql;
EXECUTE chat_index_statement;
DEALLOCATE PREPARE chat_index_statement;

-- chat_memory_turn：沿用原索引的查询或唯一约束用途，只统一名称。
SET @chat_index_sql = IF(
  EXISTS(SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS
         WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_memory_turn' AND INDEX_NAME = 'uk_dispatch_chat_memory_turn_request')
  AND NOT EXISTS(SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS
                 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_memory_turn' AND INDEX_NAME = 'uk_chat_memory_turn_request'),
  'ALTER TABLE `chat_memory_turn` RENAME INDEX `uk_dispatch_chat_memory_turn_request` TO `uk_chat_memory_turn_request`',
  'SELECT 1'
);
PREPARE chat_index_statement FROM @chat_index_sql;
EXECUTE chat_index_statement;
DEALLOCATE PREPARE chat_index_statement;

-- chat_memory_receipt：沿用原索引的查询或唯一约束用途，只统一名称。
SET @chat_index_sql = IF(
  EXISTS(SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS
         WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_memory_receipt' AND INDEX_NAME = 'idx_dispatch_chat_memory_receipt_expires')
  AND NOT EXISTS(SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS
                 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_memory_receipt' AND INDEX_NAME = 'idx_chat_memory_receipt_expires'),
  'ALTER TABLE `chat_memory_receipt` RENAME INDEX `idx_dispatch_chat_memory_receipt_expires` TO `idx_chat_memory_receipt_expires`',
  'SELECT 1'
);
PREPARE chat_index_statement FROM @chat_index_sql;
EXECUTE chat_index_statement;
DEALLOCATE PREPARE chat_index_statement;

-- chat_group_context：沿用原索引的查询或唯一约束用途，只统一名称。
SET @chat_index_sql = IF(
  EXISTS(SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS
         WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_group_context' AND INDEX_NAME = 'idx_dispatch_chat_group_context_expires')
  AND NOT EXISTS(SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS
                 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_group_context' AND INDEX_NAME = 'idx_chat_group_context_expires'),
  'ALTER TABLE `chat_group_context` RENAME INDEX `idx_dispatch_chat_group_context_expires` TO `idx_chat_group_context_expires`',
  'SELECT 1'
);
PREPARE chat_index_statement FROM @chat_index_sql;
EXECUTE chat_index_statement;
DEALLOCATE PREPARE chat_index_statement;

-- chat_group_member：沿用原索引的查询或唯一约束用途，只统一名称。
SET @chat_index_sql = IF(
  EXISTS(SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS
         WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_group_member' AND INDEX_NAME = 'idx_dispatch_chat_group_member_user')
  AND NOT EXISTS(SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS
                 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_group_member' AND INDEX_NAME = 'idx_chat_group_member_user'),
  'ALTER TABLE `chat_group_member` RENAME INDEX `idx_dispatch_chat_group_member_user` TO `idx_chat_group_member_user`',
  'SELECT 1'
);
PREPARE chat_index_statement FROM @chat_index_sql;
EXECUTE chat_index_statement;
DEALLOCATE PREPARE chat_index_statement;

-- chat_workflow：沿用原索引的查询或唯一约束用途，只统一名称。
SET @chat_index_sql = IF(
  EXISTS(SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS
         WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_workflow' AND INDEX_NAME = 'idx_dispatch_chat_workflow_enabled')
  AND NOT EXISTS(SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS
                 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_workflow' AND INDEX_NAME = 'idx_chat_workflow_enabled'),
  'ALTER TABLE `chat_workflow` RENAME INDEX `idx_dispatch_chat_workflow_enabled` TO `idx_chat_workflow_enabled`',
  'SELECT 1'
);
PREPARE chat_index_statement FROM @chat_index_sql;
EXECUTE chat_index_statement;
DEALLOCATE PREPARE chat_index_statement;

-- chat_workflow：沿用原索引的查询或唯一约束用途，只统一名称。
SET @chat_index_sql = IF(
  EXISTS(SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS
         WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_workflow' AND INDEX_NAME = 'uk_dispatch_chat_workflow_key')
  AND NOT EXISTS(SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS
                 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_workflow' AND INDEX_NAME = 'uk_chat_workflow_key'),
  'ALTER TABLE `chat_workflow` RENAME INDEX `uk_dispatch_chat_workflow_key` TO `uk_chat_workflow_key`',
  'SELECT 1'
);
PREPARE chat_index_statement FROM @chat_index_sql;
EXECUTE chat_index_statement;
DEALLOCATE PREPARE chat_index_statement;

-- chat_workflow：沿用原索引的查询或唯一约束用途，只统一名称。
SET @chat_index_sql = IF(
  EXISTS(SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS
         WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_workflow' AND INDEX_NAME = 'uk_dispatch_chat_workflow_content_sha256')
  AND NOT EXISTS(SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS
                 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_workflow' AND INDEX_NAME = 'uk_chat_workflow_content_sha256'),
  'ALTER TABLE `chat_workflow` RENAME INDEX `uk_dispatch_chat_workflow_content_sha256` TO `uk_chat_workflow_content_sha256`',
  'SELECT 1'
);
PREPARE chat_index_statement FROM @chat_index_sql;
EXECUTE chat_index_statement;
DEALLOCATE PREPARE chat_index_statement;
