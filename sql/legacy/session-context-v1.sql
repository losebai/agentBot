-- 用途：归档旧会话、轮次、请求回执与出站表结构，仅供旧数据迁移核对和兼容测试。
-- 顺序：不是当前新库初始化脚本，不应在已完成统一上下文迁移的部署执行。
-- 数据影响：原样保留历史表定义，不包含删除或数据改写。

CREATE TABLE IF NOT EXISTS `chat_memory_session` (
        `session_id` VARCHAR(128) NOT NULL COMMENT 'HMAC 会话标识',
        `generation_id` CHAR(32) NOT NULL COMMENT '会话代际标识，重建时重新生成',
        `memory_summary` MEDIUMTEXT NOT NULL COMMENT '压缩后的会话摘要',
        `memory_revision` BIGINT NOT NULL DEFAULT 0 COMMENT '摘要乐观锁版本',
        `compacted_through_turn_id` BIGINT NOT NULL DEFAULT 0 COMMENT '已压缩最大轮次 ID',
        `expires_at` DATETIME(6) NOT NULL COMMENT '滑动过期时间',
        `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
        `update_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
            ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间',
        -- 主键定义记录身份，防止同一业务记录重复创建。
        PRIMARY KEY (`session_id`),
        -- 联合索引支持对应字段过滤和有序读取，避免全表扫描。
        KEY `idx_chat_memory_session_expires` (`expires_at`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='chat 持久会话记忆';

CREATE TABLE IF NOT EXISTS `chat_memory_turn` (
        `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '轮次主键',
        `session_id` VARCHAR(128) NOT NULL COMMENT 'HMAC 会话标识',
        `request_id` VARCHAR(128) COLLATE utf8mb4_bin NULL COMMENT '幂等请求标识，旧轮次为空',
        `user_content` MEDIUMTEXT NOT NULL COMMENT '用户历史文本',
        `assistant_content` MEDIUMTEXT NOT NULL COMMENT '已投递助手内容',
        `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
        -- 主键定义记录身份，防止同一业务记录重复创建。
        PRIMARY KEY (`id`),
        -- 联合索引支持对应字段过滤和有序读取，避免全表扫描。
        KEY `idx_chat_memory_turn_session_id` (`session_id`, `id`),
        -- 唯一索引约束业务标识，支持并发写入时的重复检查。
        UNIQUE KEY `uk_chat_memory_turn_request` (`session_id`, `request_id`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='chat 最近会话轮次';

CREATE TABLE IF NOT EXISTS `chat_memory_receipt` (
        `session_id` VARCHAR(128) NOT NULL COMMENT 'HMAC 会话标识',
        `request_id` VARCHAR(128) COLLATE utf8mb4_bin NOT NULL COMMENT '幂等请求标识',
        `expires_at` DATETIME(6) NOT NULL COMMENT '独立重试去重截止时间',
        -- 主键定义记录身份，防止同一业务记录重复创建。
        PRIMARY KEY (`session_id`, `request_id`),
        -- 联合索引支持对应字段过滤和有序读取，避免全表扫描。
        KEY `idx_chat_memory_receipt_expires` (`expires_at`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='chat 已提交请求回执';


-- 用途：记录全部实际出站消息及平台发送前占位，按会话代际隔离上下文。
-- 顺序：已有 MySQL 部署备份后手动执行，chat_memory_session 必须已包含 generation_id。
-- 数据影响：只新增表和索引，不修改旧记忆或素材，不自动补写历史发送记录。
-- 不建立会话或素材外键：重置会话与清理素材不能删除审计和发送去重回执。
CREATE TABLE IF NOT EXISTS chat_sent_message (
    id BIGINT NOT NULL AUTO_INCREMENT COMMENT '日志主键，相同完成时间下保持确定顺序',
    session_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL COMMENT '匿名会话标识，禁止跨会话读取',
    generation_id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '发送任务绑定的原会话代际',
    delivery_id CHAR(64) COLLATE utf8mb4_bin NOT NULL COMMENT '稳定投递身份的 SHA256，重试不重发',
    request_id VARCHAR(128) COLLATE utf8mb4_bin NULL COMMENT '关联原用户请求，独立通知可为空',
    message_id VARCHAR(128) COLLATE utf8mb4_bin NULL COMMENT '平台确认的消息标识，平台未返回时为空',
    content MEDIUMTEXT NOT NULL COMMENT '实际发送文字和结构化素材元数据，不内嵌图片原件',
    material_ids MEDIUMTEXT NOT NULL COMMENT '本条消息引用的持久素材标识 JSON 数组',
    status VARCHAR(16) COLLATE utf8mb4_bin NOT NULL COMMENT 'pending 为结果未知占位，sent 为已确认',
    created_at DATETIME(6) NOT NULL COMMENT '占位创建或首次确认完成时间，统一使用 UTC',
    -- 自增主键用于完成时间相同的日志稳定排序。
    PRIMARY KEY (id),
    -- 并发发送及任务重试只允许一个发送占位。
    UNIQUE KEY uk_chat_sent_message_delivery (delivery_id),
    -- 当前会话代际只读取成功投递，按完成时间分页。
    KEY idx_chat_sent_message_sessiongen (session_id,generation_id,status,created_at,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='全部 QQ 出站消息及不确定发送占位';


