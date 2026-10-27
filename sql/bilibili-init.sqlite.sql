-- B 站单账号与外部操作记录；0.5.7 既有 SQLite 库由管理员显式执行一次。
-- 在完整初始化之后执行；只新增表和索引，不修改或删除业务数据，不由普通启动执行。
CREATE TABLE IF NOT EXISTS agent_bot_bilibili_account (
    id INTEGER PRIMARY KEY CHECK (id = 1), -- 固定单账号，避免模型选择其他身份
    uid TEXT NOT NULL, -- B 站账号 UID
    name TEXT NOT NULL, -- 最近验证的账号昵称
    cookies TEXT NOT NULL, -- 仅服务端可读的登录凭据，不回填 API
    updated_at INTEGER NOT NULL -- 最近确认登录的 UTC 秒
);
CREATE TABLE IF NOT EXISTS agent_bot_bilibili_operation (
    operation_key TEXT PRIMARY KEY, -- 账号、归属、请求和参数的 SHA256 唯一占位
    uid TEXT NOT NULL, -- 操作时绑定的账号，不随清除登录删除记录
    tool TEXT NOT NULL, -- 产生外部副作用的工具名
    status TEXT NOT NULL CHECK (status IN ('pending', 'completed')), -- 未知结果禁止重发
    result TEXT NULL, -- 成功后的安全 JSON 结果，不含凭据
    created_at INTEGER NOT NULL -- 操作占位的 UTC 秒
);
