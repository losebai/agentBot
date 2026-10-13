-- 用途：为纯 Python 一次性任务及自动发言决策增加三张表。
-- 顺序：已有 agentBot 数据库由管理员备份后显式执行；新库使用统一初始化脚本。
-- 数据影响：仅创建不存在的表，不复制 Java 待办、不覆盖配置、不删除任何数据；无自动执行。
-- 所有时间为 UTC，任务正文不包含服务凭据；决策记录不保存群聊状态正文。

CREATE TABLE IF NOT EXISTS agent_bot_job_owner (
  owner_key CHAR(64) COLLATE utf8mb4_bin NOT NULL COMMENT '机器人、会话、发起人的稳定摘要，作为创建配额行锁',
  -- 主键串行化同一所有者的创建事务，防止并发突破五十个活动任务的上限。
  PRIMARY KEY (owner_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='一次性任务所有者配额锁';

CREATE TABLE IF NOT EXISTS agent_bot_job (
  id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '服务端随机任务标识',
  owner_key CHAR(64) COLLATE utf8mb4_bin NOT NULL COMMENT '可信所有者摘要',
  deduplication_key CHAR(64) COLLATE utf8mb4_bin NOT NULL COMMENT '所有者与请求组成的幂等摘要，每条请求最多一个任务',
  bot_id VARCHAR(20) COLLATE utf8mb4_bin NOT NULL COMMENT '原机器人 QQ 号',
  session_id VARCHAR(256) COLLATE utf8mb4_bin NOT NULL COMMENT '原会话标识',
  sender_id VARCHAR(20) COLLATE utf8mb4_bin NOT NULL COMMENT '发起人 QQ 号',
  scene VARCHAR(16) NOT NULL COMMENT '原投递场景 group 或 private',
  group_id VARCHAR(20) COLLATE utf8mb4_bin NULL COMMENT '群聊目标，私聊为空',
  model VARCHAR(200) NOT NULL COMMENT '创建时的有效模型别名，到期仍须在目录启用',
  system_prompt MEDIUMTEXT NOT NULL COMMENT '创建时的系统提示快照，不含凭据',
  prompt TEXT NOT NULL COMMENT '一次性任务正文，最多八千字符',
  status VARCHAR(16) NOT NULL COMMENT '状态 PENDING、RUNNING、SUCCEEDED、FAILED、CANCELLED',
  result MEDIUMTEXT NULL COMMENT '成功模型正文，最多六万四千字符',
  error_code VARCHAR(100) NULL COMMENT '稳定脱敏错误码，不保存异常正文',
  run_at DATETIME(6) NOT NULL COMMENT '计划执行时间，UTC',
  created_at DATETIME(6) NOT NULL COMMENT '创建时间，UTC',
  started_at DATETIME(6) NULL COMMENT '获得执行权时间，UTC',
  completed_at DATETIME(6) NULL COMMENT '进入终态时间，UTC',
  delivered_at DATETIME(6) NULL COMMENT '消息队列确认投递完成时间，UTC',
  -- 主键与唯一幂等索引分别约束任务身份和每请求唯一创建。
  PRIMARY KEY (id),
  UNIQUE KEY uk_job_request (deduplication_key),
  -- 所有者索引支持活动配额和最近任务查询。
  KEY idx_job_owner_status (owner_key,status),
  KEY idx_job_owner_created (owner_key,created_at,id),
  -- 调度、陈旧执行恢复和机器人待投递结果分别使用独立复合索引。
  KEY idx_job_due (status,run_at,id),
  KEY idx_job_started (status,started_at),
  KEY idx_job_delivery (bot_id,status,delivered_at,id),
  -- 任务引用稳定所有者，删除任务不会删除其他历史或配额锁。
  CONSTRAINT fk_job_owner FOREIGN KEY (owner_key) REFERENCES agent_bot_job_owner(owner_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Python 一次性文本任务及投递回执';

CREATE TABLE IF NOT EXISTS agent_bot_auto_speak_decision (
  id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '服务端决策记录标识',
  session_id VARCHAR(256) COLLATE utf8mb4_bin NOT NULL COMMENT '自动发言会话标识',
  request_id VARCHAR(256) NOT NULL COMMENT '本次决策请求标识',
  mcp_code VARCHAR(64) NOT NULL COMMENT '已登记的决策服务代码',
  mcp_revision BIGINT UNSIGNED NULL COMMENT '本次决策使用的服务配置版本',
  status VARCHAR(16) NOT NULL COMMENT 'completed、failed 或 cancelled',
  should_speak TINYINT NULL COMMENT '有效结果是否发言，失败为空',
  confidence DOUBLE NULL COMMENT '有效结果置信度，零到一，失败为空',
  duration_ms BIGINT UNSIGNED NOT NULL COMMENT '本次调用耗时，毫秒',
  error_code VARCHAR(100) NULL COMMENT '稳定脱敏错误码，不保存远端原文',
  created_at DATETIME(6) NOT NULL COMMENT '记录时间，UTC',
  -- 主键保证记录身份；时间与会话索引用于有界查询与部署方清理。
  PRIMARY KEY (id),
  KEY idx_auto_decision_created (created_at,id),
  KEY idx_auto_decision_session (session_id,created_at,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Python 自动发言固定选择决策记录';
