-- 用途：为完整 0.6.2 业务库增加持续身份、任务、情节及审批审计；由显式升级清单执行。
-- 顺序：现有任务恢复及调用分类迁移之后；仅新增表，不改旧数据、身份、密钥或启用状态。
CREATE TABLE IF NOT EXISTS agent_bot_autonomy_profile (
  id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '固定身份主键，修订设定不改变标识',
  namespace CHAR(64) COLLATE utf8mb4_bin NOT NULL COMMENT '会话密钥派生命名空间',
  owner_key CHAR(64) COLLATE utf8mb4_bin NOT NULL COMMENT '可信机器人、会话与用户摘要',
  revision INT NOT NULL COMMENT '管理设定及审批乐观锁版本',
  document_json JSON NOT NULL COMMENT '身份、受信绑定、策略、运行状态及预算计数',
  next_wake_at DATETIME(6) NOT NULL COMMENT '下次发现和复盘UTC时间',
  created_at DATETIME(6) NOT NULL COMMENT '创建UTC时间',
  updated_at DATETIME(6) NOT NULL COMMENT '最近状态提交UTC时间',
  PRIMARY KEY(id),
  -- 唯一约束防止重启后重复认领；索引只唤醒到期身份。
  UNIQUE KEY uq_autonomy_owner(namespace,owner_key),
  KEY idx_autonomy_wake(namespace,next_wake_at,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='持续身份与宿主策略和动态状态';
CREATE TABLE IF NOT EXISTS agent_bot_autonomy_task (
  id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '任务及汇报幂等标识',
  profile_id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '所属持续身份',
  request_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL COMMENT '原始创建事件幂等键',
  revision INT NOT NULL COMMENT '审批及计划修订版本',
  status VARCHAR(16) NOT NULL COMMENT 'pending/running/retry/blocked/approval/succeeded/failed/cancelled',
  document_json JSON NOT NULL COMMENT '目标、计划、步骤检查点和身份快照',
  attempts INT NOT NULL COMMENT '当前步骤领取次数，完成后清零',
  lease_token CHAR(32) COLLATE utf8mb4_bin NULL COMMENT '随机执行令牌，拒绝迟到提交',
  lease_expires_at DATETIME(6) NULL COMMENT '执行权到期UTC时间，晚于网络超时',
  next_run_at DATETIME(6) NOT NULL COMMENT '重试或定时执行UTC时间',
  created_at DATETIME(6) NOT NULL COMMENT '创建UTC时间',
  updated_at DATETIME(6) NOT NULL COMMENT '最近提交UTC时间',
  PRIMARY KEY(id),
  -- 请求唯一键防重复发现；调度索引支持过期租约恢复。
  UNIQUE KEY uq_autonomy_task_request(profile_id,request_id),
  KEY idx_autonomy_task_due(profile_id,status,next_run_at),
  -- 保留任务所有权，禁止删除仍有任务的身份。
  CONSTRAINT fk_autonomy_task_profile FOREIGN KEY(profile_id) REFERENCES agent_bot_autonomy_profile(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='可恢复的自主任务和逐步执行检查点';
CREATE TABLE IF NOT EXISTS agent_bot_autonomy_episode (
  id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '身份及来源事件派生的情节标识',
  profile_id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '可信固定身份',
  event_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL COMMENT '来源事件标识',
  document_json JSON NOT NULL COMMENT '来源、原文、摘要及用户任务意图',
  expires_at DATETIME(6) NOT NULL COMMENT '原情节到期UTC时间，不影响已确认事实',
  created_at DATETIME(6) NOT NULL COMMENT '创建UTC时间',
  PRIMARY KEY(id),
  -- 来源唯一约束防止重复归档；到期索引支持有限遗忘。
  UNIQUE KEY uq_autonomy_episode_event(profile_id,event_id),
  KEY idx_autonomy_episode_expiry(profile_id,expires_at),
  CONSTRAINT fk_autonomy_episode_profile FOREIGN KEY(profile_id) REFERENCES agent_bot_autonomy_profile(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='有来源和保留期限的情节记忆';
CREATE TABLE IF NOT EXISTS agent_bot_autonomy_proposal (
  id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '事实提取提案标识',
  profile_id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '所属身份',
  episode_id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '可追踪来源情节',
  status VARCHAR(16) NOT NULL COMMENT 'pending/applying/applied/rejected/review',
  revision INT NOT NULL COMMENT '审批所见版本',
  document_json JSON NOT NULL COMMENT '操作参数、证据绑定和执行回执',
  created_at DATETIME(6) NOT NULL COMMENT '创建UTC时间',
  PRIMARY KEY(id),
  -- 状态索引支持管理员查询待核对提案。
  KEY idx_autonomy_proposal_status(profile_id,status,created_at),
  -- 来源记录保留墓碑以维持审计关系。
  CONSTRAINT fk_autonomy_proposal_profile FOREIGN KEY(profile_id) REFERENCES agent_bot_autonomy_profile(id),
  CONSTRAINT fk_autonomy_proposal_episode FOREIGN KEY(episode_id) REFERENCES agent_bot_autonomy_episode(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='有版本审批和来源验证的长期事实提案';
CREATE TABLE IF NOT EXISTS agent_bot_autonomy_audit (
  id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT 'UTC时间前缀加随机后缀，支持稳定倒序分页',
  profile_id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '所属身份',
  kind VARCHAR(40) NOT NULL COMMENT '受控事件类型',
  subject_id CHAR(32) COLLATE utf8mb4_bin NULL COMMENT '对应任务或提案标识',
  document_json JSON NOT NULL COMMENT '身份版本、状态变化或评估结果',
  created_at DATETIME(6) NOT NULL COMMENT '事件UTC时间',
  PRIMARY KEY(id),
  -- 时间索引支持有界审计分页。
  KEY idx_autonomy_audit_time(profile_id,created_at,id),
  CONSTRAINT fk_autonomy_audit_profile FOREIGN KEY(profile_id) REFERENCES agent_bot_autonomy_profile(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='自主行为与身份修订不可变审计';
