-- 用途：为已有一次性任务增加执行租约与审计；由版本清单在旧任务表之后执行。
-- 数据影响：仅新增表与索引，保留原任务、身份、结果和投递回执；不回填虚构执行记录。
CREATE TABLE IF NOT EXISTS agent_bot_job_attempt (
  id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '随机执行租约标识，拒绝旧执行器迟到提交',
  job_id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '所属持久任务标识',
  status VARCHAR(16) NOT NULL COMMENT '执行状态 RUNNING、SUCCEEDED、FAILED、INTERRUPTED、CANCELLED',
  started_at DATETIME(6) NOT NULL COMMENT '领取时间，UTC',
  lease_expires_at DATETIME(6) NOT NULL COMMENT '执行权过期时间，UTC；晚于模型硬超时',
  completed_at DATETIME(6) NULL COMMENT '执行结束或确认中断时间，UTC',
  error_code VARCHAR(100) NULL COMMENT '稳定脱敏错误码，不记录凭据或供应商异常正文',
  -- 主键使每次执行拥有独立令牌；任务索引支持恢复次数统计与审计查询。
  PRIMARY KEY (id),
  KEY idx_job_attempt_history (job_id,started_at,id),
  -- 审计依附原任务，禁止删除仍有执行历史的任务。
  CONSTRAINT fk_job_attempt_job FOREIGN KEY (job_id) REFERENCES agent_bot_job(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='定时任务执行租约和恢复审计';
