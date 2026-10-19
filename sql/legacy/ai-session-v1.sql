-- 用途：保留普通调用旧执行表原结构，供显式迁移和隔离测试核对。
-- 执行顺序：仅旧数据迁移参考；当前新库不应执行，不含删除或改写。
CREATE TABLE IF NOT EXISTS agent_bot_ai_session (
  execution_id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '执行标识，三十二位十六进制',
  session_id VARCHAR(256) COLLATE utf8mb4_bin NOT NULL COMMENT '区分大小写的会话标识',
  request_id VARCHAR(256) NOT NULL COMMENT '调用方请求标识',
  configured_model VARCHAR(200) NOT NULL COMMENT '请求指定的模型别名',
  model VARCHAR(255) NOT NULL COMMENT '实际使用的模型标识',
  status VARCHAR(16) NOT NULL COMMENT '执行状态：运行中、完成、失败或取消',
  duration_ms BIGINT UNSIGNED NOT NULL COMMENT '执行耗时，单位毫秒',
  `usage` JSON NOT NULL COMMENT '令牌用量统计',
  created_at DATETIME(6) NOT NULL COMMENT '创建时间，协调世界时',
  error_code VARCHAR(100) NULL COMMENT '稳定错误代码',
  prompt MEDIUMTEXT NOT NULL COMMENT '用户输入文本',
  content MEDIUMTEXT NOT NULL COMMENT '模型输出文本',
  PRIMARY KEY (execution_id),
  KEY idx_session_created (session_id, created_at, execution_id),
  KEY idx_created (created_at, execution_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='普通文本调用记录';
