-- 用途：为完整 0.6.2 业务库增加持续身份、任务、情节及审批审计；由显式升级清单执行。
-- 顺序：现有任务恢复及调用分类迁移之后；数据影响仅新增表，不启用自主循环、不改身份或密钥。
CREATE TABLE IF NOT EXISTS agent_bot_autonomy_profile (
  -- 稳定身份主键，修改设定不改变此标识
  id CHAR(32) NOT NULL PRIMARY KEY,
  -- 会话密钥派生命名空间，换密钥保留旧记录但不重新认领
  namespace CHAR(64) NOT NULL,
  -- 同一命名空间的可信机器人、会话、用户摘要
  owner_key CHAR(64) NOT NULL,
  -- 管理设定乐观锁版本，不因工作线程更新进度而推进
  revision INTEGER NOT NULL,
  -- 受信绑定、设定、宿主策略、自我状态及预算计数
  document_json TEXT NOT NULL CHECK(json_valid(document_json)),
  -- 下次事件发现与复盘时间，UTC
  next_wake_at DATETIME NOT NULL,
  -- 创建UTC时间
  created_at DATETIME NOT NULL,
  -- 最近状态提交UTC时间
  updated_at DATETIME NOT NULL,
  -- 唯一约束避免重启后创建重复身份
  UNIQUE(namespace,owner_key)
);
-- 索引仅检索当前密钥空间的到期唤醒
CREATE INDEX IF NOT EXISTS idx_autonomy_wake ON agent_bot_autonomy_profile(namespace,next_wake_at,id);
CREATE TABLE IF NOT EXISTS agent_bot_autonomy_task (
  -- 持久任务主键，同时作为汇报幂等标识
  id CHAR(32) NOT NULL PRIMARY KEY,
  -- 所属固定身份，外键阻止身份被随意删除
  profile_id CHAR(32) NOT NULL REFERENCES agent_bot_autonomy_profile(id),
  -- 创建事件或请求的幂等键
  request_id VARCHAR(128) NOT NULL,
  -- 审批和计划变更版本
  revision INTEGER NOT NULL,
  -- pending/running/retry/blocked/approval/succeeded/failed/cancelled
  status VARCHAR(16) NOT NULL,
  -- 目标、计划、已完成步骤、原始身份版本和错误
  document_json TEXT NOT NULL CHECK(json_valid(document_json)),
  -- 当前步骤已领取次数，完成步骤后清零
  attempts INTEGER NOT NULL,
  -- 拒绝旧工作线程迟到提交的随机租约令牌
  lease_token CHAR(32) NULL,
  -- 执行租约结束UTC时间，网络超时必须小于该值
  lease_expires_at DATETIME NULL,
  -- 重试或定时执行UTC时间
  next_run_at DATETIME NOT NULL,
  -- 创建UTC时间
  created_at DATETIME NOT NULL,
  -- 最近提交UTC时间
  updated_at DATETIME NOT NULL,
  -- 同一身份的创建事件仅产生一个任务
  UNIQUE(profile_id,request_id)
);
-- 调度索引支持到期任务及中断租约恢复
CREATE INDEX IF NOT EXISTS idx_autonomy_task_due ON agent_bot_autonomy_task(profile_id,status,next_run_at);
CREATE TABLE IF NOT EXISTS agent_bot_autonomy_episode (
  -- 情节主键，由身份和来源事件确定
  id CHAR(32) NOT NULL PRIMARY KEY,
  -- 可信身份绑定
  profile_id CHAR(32) NOT NULL REFERENCES agent_bot_autonomy_profile(id),
  -- 去重后的来源事件标识
  event_id VARCHAR(128) NOT NULL,
  -- 原始来源、发生时间、摘要和任务意图
  document_json TEXT NOT NULL CHECK(json_valid(document_json)),
  -- 有限保留原情节的到期UTC时间，不影响已确认长期事实
  expires_at DATETIME NOT NULL,
  -- 创建UTC时间
  created_at DATETIME NOT NULL,
  -- 同一来源不会重复归档或发现任务
  UNIQUE(profile_id,event_id)
);
-- 过期索引支持有界情节遗忘，不扫描长期事实正文
CREATE INDEX IF NOT EXISTS idx_autonomy_episode_expiry ON agent_bot_autonomy_episode(profile_id,expires_at);
CREATE TABLE IF NOT EXISTS agent_bot_autonomy_proposal (
  -- 事实提取提案主键
  id CHAR(32) NOT NULL PRIMARY KEY,
  -- 所属身份
  profile_id CHAR(32) NOT NULL REFERENCES agent_bot_autonomy_profile(id),
  -- 来源情节，不删除情节记录以保留证据关联
  episode_id CHAR(32) NOT NULL REFERENCES agent_bot_autonomy_episode(id),
  -- 申请/执行/完成/拒绝/人工核对状态
  status VARCHAR(16) NOT NULL,
  -- 提案审批版本
  revision INTEGER NOT NULL,
  -- 有来源绑定的操作及最终回执；不保存模型凭据
  document_json TEXT NOT NULL CHECK(json_valid(document_json)),
  -- 创建UTC时间
  created_at DATETIME NOT NULL
);
-- 审批列表按身份和状态定位
CREATE INDEX IF NOT EXISTS idx_autonomy_proposal_status ON agent_bot_autonomy_proposal(profile_id,status,created_at);
CREATE TABLE IF NOT EXISTS agent_bot_autonomy_audit (
  -- 审计主键，UTC时间前缀加随机后缀，支持稳定倒序分页
  id CHAR(32) NOT NULL PRIMARY KEY,
  -- 审计所属身份
  profile_id CHAR(32) NOT NULL REFERENCES agent_bot_autonomy_profile(id),
  -- 事件类型，不记录供应商原始异常或密钥
  kind VARCHAR(40) NOT NULL,
  -- 对应任务或提案；空值表示身份级事件
  subject_id CHAR(32) NULL,
  -- 版本快照、状态变化或评估结果
  document_json TEXT NOT NULL CHECK(json_valid(document_json)),
  -- 事件UTC时间
  created_at DATETIME NOT NULL
);
-- 审计支持按身份和时间进行有限分页
CREATE INDEX IF NOT EXISTS idx_autonomy_audit_time ON agent_bot_autonomy_audit(profile_id,created_at,id);
