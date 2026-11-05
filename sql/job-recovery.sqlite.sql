-- 用途：为已有一次性任务增加执行租约与审计；由版本清单在旧任务表之后执行。
-- 数据影响：仅新增表与索引，保留原任务、身份、结果和投递回执；不回填虚构执行记录。
CREATE TABLE IF NOT EXISTS agent_bot_job_attempt (
    -- 随机执行租约标识，拒绝旧执行器迟到提交
    id TEXT NOT NULL PRIMARY KEY,
    -- 所属持久任务标识
    job_id TEXT NOT NULL,
    -- 执行状态 RUNNING、SUCCEEDED、FAILED、INTERRUPTED、CANCELLED
    status TEXT NOT NULL,
    -- 领取时间，UTC
    started_at DATETIME NOT NULL,
    -- 执行权过期时间，UTC；晚于模型硬超时
    lease_expires_at DATETIME NOT NULL,
    -- 执行结束或确认中断时间，UTC
    completed_at DATETIME NULL,
    -- 稳定脱敏错误码，不记录凭据或供应商异常正文
    error_code TEXT NULL,
    -- 审计依附原任务，禁止删除仍有执行历史的任务
    CONSTRAINT fk_job_attempt_job FOREIGN KEY (job_id) REFERENCES agent_bot_job(id)
);
-- 任务索引支持恢复次数统计与审计查询。
CREATE INDEX IF NOT EXISTS idx_job_attempt_history
    ON agent_bot_job_attempt (job_id,started_at,id);
