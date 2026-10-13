-- SQLite 统一调用记录增量；先执行本脚本，再运行 records migrate。
-- 只新增表和索引，保留全部旧记录；JSON 使用文本保存并检查合法性。
CREATE TABLE IF NOT EXISTS agent_bot_call_record (
  id INTEGER PRIMARY KEY AUTOINCREMENT, -- 统一记录主键
  session_Id TEXT COLLATE BINARY, -- 会话标识
  content TEXT, -- 用户输入正文
  replay_context TEXT, -- 模型回复正文
  create_time TEXT, -- 调用记录时间，保留源时间
  duration INTEGER, -- 调用耗时，单位 ms
  user_certificate TEXT, -- 用户凭据摘要，不保存原始凭据
  model TEXT, -- 实际调用模型
  data TEXT CHECK (data IS NULL OR json_valid(data)), -- 响应数据、用量及扩展
  request_context TEXT CHECK (request_context IS NULL OR json_valid(request_context)), -- 无临时凭据的上下文
  request_id TEXT COLLATE BINARY, -- 请求关联标识
  type TEXT NOT NULL, -- ordinary、agent、auto_speak、unknown
  source TEXT NOT NULL COLLATE BINARY, -- 来源系统及表命名空间
  source_id TEXT NOT NULL COLLATE BINARY, -- 来源原始主键
  execution_id TEXT, -- 本地执行标识，用于关联步骤与附件
  status TEXT NOT NULL -- 执行状态，历史缺失时为 unknown
);
-- 来源内唯一，重复导入不得覆盖新记录。
CREATE UNIQUE INDEX IF NOT EXISTS uk_call_source ON agent_bot_call_record(source, source_id);
-- 类型、会话、状态分别支持自增主键分页。
CREATE INDEX IF NOT EXISTS idx_call_type ON agent_bot_call_record(type, id);
CREATE INDEX IF NOT EXISTS idx_call_session ON agent_bot_call_record(session_Id, id);
CREATE INDEX IF NOT EXISTS idx_call_status ON agent_bot_call_record(status, id);
