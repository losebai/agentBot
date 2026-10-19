-- 用途：归档旧会话、轮次、请求回执与出站表结构，仅供旧数据迁移核对和兼容测试。
-- 顺序：不是当前新库初始化脚本，不应在已完成统一上下文迁移的部署执行。
-- 数据影响：原样保留历史表定义，不包含删除或数据改写。

CREATE TABLE IF NOT EXISTS chat_memory_session (
    -- HMAC 会话标识
    `session_id` TEXT COLLATE NOCASE NOT NULL,
    -- 会话代际标识，重建时重新生成
    `generation_id` TEXT COLLATE NOCASE NOT NULL,
    -- 压缩后的会话摘要
    `memory_summary` TEXT COLLATE NOCASE NOT NULL,
    -- 摘要乐观锁版本
    `memory_revision` INTEGER NOT NULL DEFAULT 0,
    -- 已压缩最大轮次 ID
    `compacted_through_turn_id` INTEGER NOT NULL DEFAULT 0,
    -- 滑动过期时间
    `expires_at` DATETIME NOT NULL,
    -- 创建时间
    `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- 更新时间
    `update_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`session_id`)
);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_chat_memory_session_expires ON chat_memory_session (`expires_at`);
-- 更新业务字段时同步记录更新时间，不覆盖应用显式设置的时间。
CREATE TRIGGER IF NOT EXISTS chat_memory_session_update_time AFTER UPDATE ON chat_memory_session
WHEN NEW.update_time = OLD.update_time BEGIN
    UPDATE chat_memory_session SET update_time=CURRENT_TIMESTAMP WHERE session_id=NEW.session_id;
END;

-- chat 最近会话轮次
CREATE TABLE IF NOT EXISTS chat_memory_turn (
    -- 轮次主键
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- HMAC 会话标识
    `session_id` TEXT COLLATE NOCASE NOT NULL,
    -- 幂等请求标识，旧轮次为空
    `request_id` TEXT NULL,
    -- 用户历史文本
    `user_content` TEXT COLLATE NOCASE NOT NULL,
    -- 已投递助手内容
    `assistant_content` TEXT COLLATE NOCASE NOT NULL,
    -- 创建时间
    `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_chat_memory_turn_session_id ON chat_memory_turn (`session_id`, `id`);
-- 唯一索引保护业务标识。
CREATE UNIQUE INDEX IF NOT EXISTS uk_chat_memory_turn_request ON chat_memory_turn (`session_id`, `request_id`);

-- chat 已提交请求回执
CREATE TABLE IF NOT EXISTS chat_memory_receipt (
    -- HMAC 会话标识
    `session_id` TEXT COLLATE NOCASE NOT NULL,
    -- 幂等请求标识
    `request_id` TEXT NOT NULL,
    -- 独立重试去重截止时间
    `expires_at` DATETIME NOT NULL,
    PRIMARY KEY (`session_id`, `request_id`)
);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_chat_memory_receipt_expires ON chat_memory_receipt (`expires_at`);

-- chat 群聊最近文本

-- 用途：记录全部实际出站消息及平台发送前占位，按会话代际隔离上下文。
-- 顺序：已有 SQLite 部署备份后手动执行，chat_memory_session 必须已包含 generation_id。
-- 数据影响：只新增表和索引，不修改旧记忆或素材，不自动补写历史发送记录。
-- 不建立会话或素材外键：重置会话与清理素材不能删除审计和发送去重回执。
CREATE TABLE IF NOT EXISTS chat_sent_message (
    -- 日志主键，相同完成时间下保持确定顺序。
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 匿名会话标识，禁止跨会话读取。
    session_id TEXT NOT NULL,
    -- 发送任务绑定的原会话代际。
    generation_id TEXT NOT NULL,
    -- 稳定投递身份的 SHA256，重试不重发。
    delivery_id TEXT NOT NULL,
    -- 关联原用户请求，独立通知可为空。
    request_id TEXT NULL,
    -- 平台确认的消息标识，平台未返回时为空。
    message_id TEXT NULL,
    -- 实际发送文字和结构化素材元数据，不内嵌图片原件。
    content TEXT NOT NULL,
    -- 本条消息引用的持久素材标识 JSON 数组。
    material_ids TEXT NOT NULL,
    -- pending 为结果未知占位，sent 为已确认。
    status TEXT NOT NULL,
    -- 占位创建或首次确认完成时间，统一使用 UTC。
    created_at DATETIME NOT NULL
);
-- 并发发送及任务重试只允许一个发送占位。
CREATE UNIQUE INDEX IF NOT EXISTS uk_chat_sent_message_delivery
ON chat_sent_message(delivery_id);
-- 当前会话代际只读取成功投递，按完成时间分页。
CREATE INDEX IF NOT EXISTS idx_chat_sent_message_sessiongen
ON chat_sent_message(session_id,generation_id,status,created_at,id);


