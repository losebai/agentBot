-- 统一普通调用、Agent 与旧系统调用记录；先执行本脚本，再运行 records migrate。
-- 仅新增表，不修改或删除原始记录。来源唯一键支持重复执行历史导入。
CREATE TABLE IF NOT EXISTS agent_bot_call_record (
  `id` INT NOT NULL AUTO_INCREMENT COMMENT '统一调用记录主键',
  `session_Id` VARCHAR(255) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '会话标识',
  `content` TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin COMMENT '用户输入正文',
  `replay_context` TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin COMMENT '模型回复正文',
  `create_time` DATETIME DEFAULT NULL COMMENT '调用记录时间，保留源时间',
  `duration` INT DEFAULT NULL COMMENT '调用耗时，单位 ms',
  `user_certificate` VARCHAR(255) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '用户凭据摘要，不保存原始凭据',
  `model` VARCHAR(255) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '实际调用模型',
  `data` JSON DEFAULT NULL COMMENT '响应数据、用量及兼容扩展字段',
  `request_context` JSON DEFAULT NULL COMMENT 'AI请求上下文（不含临时凭据）',
  `request_id` VARCHAR(255) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '请求关联标识',
  `type` VARCHAR(16) COLLATE utf8mb4_bin NOT NULL COMMENT '类型：ordinary、agent、auto_speak、unknown',
  `source` VARCHAR(191) COLLATE utf8mb4_bin NOT NULL COMMENT '来源系统及表的稳定命名空间',
  `source_id` VARCHAR(64) COLLATE utf8mb4_bin NOT NULL COMMENT '来源记录原始主键',
  `execution_id` CHAR(32) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '本地执行标识，保留步骤和附件关联',
  `status` VARCHAR(16) COLLATE utf8mb4_bin NOT NULL COMMENT '执行状态，缺失历史状态为 unknown',
  PRIMARY KEY (`id`),
  KEY idx_call_time (`create_time`, `id`) COMMENT '按调用时间稳定分页',
  UNIQUE KEY uk_call_source (`source`, `source_id`) COMMENT '来源内去重，避免不同系统主键冲突',
  KEY idx_call_type (`type`, `create_time`, `id`) COMMENT '按类型分页',
  KEY idx_call_session (`session_Id`, `create_time`, `id`) COMMENT '按会话分页',
  KEY idx_call_status (`status`, `create_time`, `id`) COMMENT '按状态分页'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='统一 AI 调用记录';
