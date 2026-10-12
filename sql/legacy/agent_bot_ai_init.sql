-- 用途：初始化独立 agentBot 自有表，保留已有数据。

-- 执行顺序：AI 初始化、Agent 初始化、媒体初始化；已建表使用注释增量脚本。

-- 管理员显式执行；建表会申请元数据锁，服务启动不自动执行。

CREATE TABLE IF NOT EXISTS agent_bot_ai_provider (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  name VARCHAR(100) COLLATE utf8mb4_bin NOT NULL COMMENT '显示名称',
  protocol VARCHAR(32) NOT NULL COMMENT '供应商接口协议',
  base_url VARCHAR(2048) NOT NULL COMMENT '供应商接口根地址',
  credential_ref VARCHAR(128) NULL COMMENT '环境凭据引用，不保存密钥',
  enabled TINYINT NOT NULL DEFAULT 0 COMMENT '是否启用：零禁用，一启用',
  description VARCHAR(1000) NOT NULL DEFAULT '' COMMENT '中文用途说明',
  revision BIGINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '乐观锁版本，每次修改递增',
  created_at DATETIME(6) NOT NULL COMMENT '创建时间，协调世界时',
  updated_at DATETIME(6) NOT NULL COMMENT '更新时间，协调世界时',
  -- 主键保证唯一，索引支持稳定分页，外键约束关联数据完整性。
  PRIMARY KEY (id),
  -- 主键保证唯一，索引支持稳定分页，外键约束关联数据完整性。
  UNIQUE KEY uq_provider_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模型供应商配置';

CREATE TABLE IF NOT EXISTS agent_bot_ai_model (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  alias VARCHAR(200) COLLATE utf8mb4_bin NOT NULL COMMENT '区分大小写的模型别名',
  upstream_model VARCHAR(200) COLLATE utf8mb4_bin NOT NULL COMMENT '上游实际模型标识',
  provider_id BIGINT UNSIGNED NOT NULL COMMENT '所属供应商主键',
  enabled TINYINT NOT NULL DEFAULT 0 COMMENT '是否启用：零禁用，一启用',
  revision BIGINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '乐观锁版本，每次修改递增',
  created_at DATETIME(6) NOT NULL COMMENT '创建时间，协调世界时',
  updated_at DATETIME(6) NOT NULL COMMENT '更新时间，协调世界时',
  -- 主键保证唯一，索引支持稳定分页，外键约束关联数据完整性。
  PRIMARY KEY (id),
  -- 主键保证唯一，索引支持稳定分页，外键约束关联数据完整性。
  UNIQUE KEY uq_model_alias (alias),
  -- 主键保证唯一，索引支持稳定分页，外键约束关联数据完整性。
  CONSTRAINT fk_agent_bot_model_provider FOREIGN KEY (provider_id) REFERENCES agent_bot_ai_provider (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模型别名目录';

CREATE TABLE IF NOT EXISTS agent_bot_ai_session (
  execution_id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '执行标识，三十二位十六进制',
  session_id VARCHAR(256) COLLATE utf8mb4_bin NOT NULL COMMENT '区分大小写的会话标识',
  request_id VARCHAR(256) NOT NULL COMMENT '调用方请求标识',
  configured_model VARCHAR(200) NOT NULL COMMENT '请求指定的模型别名',
  model VARCHAR(255) NOT NULL COMMENT '实际使用的模型标识',
  status VARCHAR(16) NOT NULL COMMENT '执行状态：运行中、完成、失败或取消',
  duration_ms BIGINT UNSIGNED NOT NULL COMMENT '执行耗时，单位毫秒',
  usage JSON NOT NULL COMMENT '令牌用量统计',
  created_at DATETIME(6) NOT NULL COMMENT '创建时间，协调世界时',
  error_code VARCHAR(100) NULL COMMENT '稳定错误代码',
  prompt MEDIUMTEXT NOT NULL COMMENT '用户输入文本',
  content MEDIUMTEXT NOT NULL COMMENT '模型输出文本',
  -- 主键保证唯一，索引支持稳定分页，外键约束关联数据完整性。
  PRIMARY KEY (execution_id),
  -- 主键保证唯一，索引支持稳定分页，外键约束关联数据完整性。
  KEY idx_session_created (session_id, created_at, execution_id),
  -- 主键保证唯一，索引支持稳定分页，外键约束关联数据完整性。
  KEY idx_created (created_at, execution_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='普通文本调用记录';
