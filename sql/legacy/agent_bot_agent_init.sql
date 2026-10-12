-- 用途：初始化独立 agentBot 自有表，保留已有数据。

-- 执行顺序：AI 初始化、Agent 初始化、媒体初始化；已建表使用注释增量脚本。

-- 管理员显式执行；建表会申请元数据锁，服务启动不自动执行。

CREATE TABLE IF NOT EXISTS agent_bot_agent_run (
  execution_id CHAR(32) COLLATE utf8mb4_bin NOT NULL PRIMARY KEY COMMENT '执行标识，三十二位十六进制',
  session_id VARCHAR(256) COLLATE utf8mb4_bin NOT NULL COMMENT '区分大小写的会话标识',
  request_id VARCHAR(256) NOT NULL COMMENT '调用方请求标识',
  entry VARCHAR(16) NOT NULL COMMENT '调用入口：管理端或机器人',
  model VARCHAR(255) NOT NULL COMMENT '实际使用的模型标识',
  backend VARCHAR(32) NOT NULL COMMENT '执行后端标识',
  status VARCHAR(16) NOT NULL COMMENT '执行状态：运行中、完成、失败或取消',
  duration_ms BIGINT UNSIGNED NOT NULL COMMENT '执行耗时，单位毫秒',
  usage JSON NOT NULL COMMENT '令牌用量统计',
  created_at DATETIME(6) NOT NULL COMMENT '创建时间，协调世界时',
  error_code VARCHAR(100) NULL COMMENT '稳定错误代码',
  prompt MEDIUMTEXT NOT NULL COMMENT '用户输入文本',
  content MEDIUMTEXT NOT NULL COMMENT '模型输出文本',
  revisions JSON NOT NULL COMMENT '本次执行使用的配置版本快照',
  -- 主键保证唯一，索引支持稳定分页，外键约束关联数据完整性。
  KEY idx_agent_run_created(created_at, execution_id),
  -- 主键保证唯一，索引支持稳定分页，外键约束关联数据完整性。
  KEY idx_agent_run_session(session_id, created_at, execution_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='智能体执行记录';

CREATE TABLE IF NOT EXISTS agent_bot_agent_step (
  execution_id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '执行标识，三十二位十六进制',
  sequence INT NOT NULL COMMENT '执行内递增事件序号',
  turn INT NOT NULL COMMENT '模型调用轮次',
  kind VARCHAR(32) NOT NULL COMMENT '步骤事件类型',
  tool VARCHAR(200) NULL COMMENT '工具名称',
  call_id VARCHAR(256) NULL COMMENT '模型工具调用标识',
  status VARCHAR(16) NOT NULL COMMENT '执行状态：运行中、完成、失败或取消',
  duration_ms BIGINT UNSIGNED NOT NULL COMMENT '执行耗时，单位毫秒',
  summary VARCHAR(1000) NOT NULL COMMENT '脱敏后的步骤摘要',
  -- 主键保证唯一，索引支持稳定分页，外键约束关联数据完整性。
  PRIMARY KEY(execution_id,sequence),
  -- 主键保证唯一，索引支持稳定分页，外键约束关联数据完整性。
  CONSTRAINT fk_agent_step_run FOREIGN KEY(execution_id) REFERENCES agent_bot_agent_run(execution_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='智能体执行步骤';

CREATE TABLE IF NOT EXISTS agent_bot_skill (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
  name VARCHAR(200) NOT NULL COMMENT '显示名称',
  description VARCHAR(2000) NOT NULL COMMENT '中文用途说明',
  category VARCHAR(64) NOT NULL COMMENT '功能分类',
  system_prompt MEDIUMTEXT NOT NULL COMMENT '系统提示词',
  sample_prompt MEDIUMTEXT NOT NULL COMMENT '示例提示词',
  tools JSON NOT NULL COMMENT '允许使用的本地工具列表',
  enabled TINYINT NOT NULL DEFAULT 0 COMMENT '是否启用：零禁用，一启用',
  revision BIGINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '乐观锁版本，每次修改递增',
  created_at DATETIME(6) NOT NULL COMMENT '创建时间，协调世界时',
  updated_at DATETIME(6) NOT NULL COMMENT '更新时间，协调世界时'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='技能目录';

CREATE TABLE IF NOT EXISTS agent_bot_mcp (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
  code VARCHAR(64) COLLATE utf8mb4_bin NOT NULL UNIQUE COMMENT '区分大小写的唯一代码',
  name VARCHAR(200) NOT NULL COMMENT '显示名称',
  description VARCHAR(2000) NOT NULL COMMENT '中文用途说明',
  category VARCHAR(16) NOT NULL COMMENT '功能分类',
  transport VARCHAR(16) NOT NULL COMMENT '远程工具传输方式',
  endpoint VARCHAR(512) NOT NULL COMMENT '远程工具服务端点',
  credential_ref VARCHAR(128) NULL COMMENT '环境凭据引用，不保存密钥',
  allowed_tools JSON NOT NULL COMMENT '允许使用的远程工具列表',
  search_tool VARCHAR(100) NULL COMMENT '搜索工具名称',
  timeout_seconds INT NOT NULL COMMENT '调用超时时间，单位秒',
  required TINYINT NOT NULL DEFAULT 0 COMMENT '是否为执行必需服务',
  enabled TINYINT NOT NULL DEFAULT 0 COMMENT '是否启用：零禁用，一启用',
  revision BIGINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '乐观锁版本，每次修改递增',
  created_at DATETIME(6) NOT NULL COMMENT '创建时间，协调世界时',
  updated_at DATETIME(6) NOT NULL COMMENT '更新时间，协调世界时'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='远程工具服务目录';

CREATE TABLE IF NOT EXISTS agent_bot_agent (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
  code VARCHAR(64) COLLATE utf8mb4_bin NOT NULL UNIQUE COMMENT '区分大小写的唯一代码',
  name VARCHAR(200) NOT NULL COMMENT '显示名称',
  description VARCHAR(2000) NOT NULL COMMENT '中文用途说明',
  system_prompt MEDIUMTEXT NOT NULL COMMENT '系统提示词',
  model_id BIGINT UNSIGNED NOT NULL COMMENT '关联的模型主键',
  tools JSON NOT NULL COMMENT '允许使用的本地工具列表',
  max_steps INT NOT NULL COMMENT '最大工具调用轮次',
  options JSON NOT NULL COMMENT '模型生成参数',
  enabled TINYINT NOT NULL DEFAULT 0 COMMENT '是否启用：零禁用，一启用',
  revision BIGINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '乐观锁版本，每次修改递增',
  created_at DATETIME(6) NOT NULL COMMENT '创建时间，协调世界时',
  updated_at DATETIME(6) NOT NULL COMMENT '更新时间，协调世界时',
  -- 主键保证唯一，索引支持稳定分页，外键约束关联数据完整性。
  CONSTRAINT fk_agent_model FOREIGN KEY(model_id) REFERENCES agent_bot_ai_model(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='智能体配置';

CREATE TABLE IF NOT EXISTS agent_bot_agent_skill (
  agent_id BIGINT UNSIGNED NOT NULL COMMENT '关联的智能体主键',
  skill_id BIGINT UNSIGNED NOT NULL COMMENT '关联的技能主键',
  -- 主键保证唯一，索引支持稳定分页，外键约束关联数据完整性。
  PRIMARY KEY(agent_id,skill_id),
  -- 主键保证唯一，索引支持稳定分页，外键约束关联数据完整性。
  CONSTRAINT fk_agent_skill_agent FOREIGN KEY(agent_id) REFERENCES agent_bot_agent(id) ON DELETE CASCADE,
  -- 主键保证唯一，索引支持稳定分页，外键约束关联数据完整性。
  CONSTRAINT fk_agent_skill_skill FOREIGN KEY(skill_id) REFERENCES agent_bot_skill(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='智能体技能关联';

CREATE TABLE IF NOT EXISTS agent_bot_agent_mcp (
  agent_id BIGINT UNSIGNED NOT NULL COMMENT '关联的智能体主键',
  mcp_id BIGINT UNSIGNED NOT NULL COMMENT '关联的远程工具主键',
  -- 主键保证唯一，索引支持稳定分页，外键约束关联数据完整性。
  PRIMARY KEY(agent_id,mcp_id),
  -- 主键保证唯一，索引支持稳定分页，外键约束关联数据完整性。
  CONSTRAINT fk_agent_mcp_agent FOREIGN KEY(agent_id) REFERENCES agent_bot_agent(id) ON DELETE CASCADE,
  -- 主键保证唯一，索引支持稳定分页，外键约束关联数据完整性。
  CONSTRAINT fk_agent_mcp_mcp FOREIGN KEY(mcp_id) REFERENCES agent_bot_mcp(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='智能体远程工具关联';
