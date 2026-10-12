-- dispatch-chat 持久聊天记忆（MySQL 8 / InnoDB）
-- 本文件幂等创建会话摘要、最近轮次和群最近文本，不会清空已有数据。

CREATE TABLE IF NOT EXISTS `dispatch_chat_memory_session` (
    `session_id`                 VARCHAR(128) NOT NULL COMMENT 'HMAC 会话标识',
    `generation_id`              CHAR(32) NOT NULL COMMENT '会话代际标识，重建时重新生成',
    `memory_summary`             MEDIUMTEXT   NOT NULL COMMENT '压缩后的会话摘要',
    `memory_revision`            BIGINT       NOT NULL DEFAULT 0 COMMENT '摘要乐观锁版本',
    `compacted_through_turn_id`  BIGINT       NOT NULL DEFAULT 0 COMMENT '已压缩最大轮次 ID',
    `expires_at`                 DATETIME(6)  NOT NULL COMMENT '滑动过期时间',
    `create_time`                DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
    `update_time`                DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间',
    PRIMARY KEY (`session_id`),
    KEY `idx_dispatch_chat_memory_session_expires` (`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='dispatch-chat 持久会话记忆';

CREATE TABLE IF NOT EXISTS `dispatch_chat_memory_turn` (
    `id`                 BIGINT       NOT NULL AUTO_INCREMENT COMMENT '轮次主键',
    `session_id`         VARCHAR(128) NOT NULL COMMENT 'HMAC 会话标识',
    `request_id`         VARCHAR(128) COLLATE utf8mb4_bin NULL COMMENT '幂等请求标识，旧轮次为空',
    `user_content`       MEDIUMTEXT   NOT NULL COMMENT '用户历史文本',
    `assistant_content`  MEDIUMTEXT   NOT NULL COMMENT '已投递助手内容',
    `create_time`        DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
    PRIMARY KEY (`id`),
    KEY `idx_dispatch_chat_memory_turn_session_id` (`session_id`, `id`),
    UNIQUE KEY `uk_dispatch_chat_memory_turn_request` (`session_id`, `request_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='dispatch-chat 最近会话轮次';

-- 回执独立保留 86400 秒，覆盖任务最长重试窗口，不受历史裁剪、过期或 reset 影响。
CREATE TABLE IF NOT EXISTS `dispatch_chat_memory_receipt` (
    `session_id` VARCHAR(128) NOT NULL COMMENT 'HMAC 会话标识',
    `request_id` VARCHAR(128) COLLATE utf8mb4_bin NOT NULL COMMENT '幂等请求标识',
    `expires_at` DATETIME(6) NOT NULL COMMENT '独立重试去重截止时间',
    PRIMARY KEY (`session_id`, `request_id`),
    KEY `idx_dispatch_chat_memory_receipt_expires` (`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='dispatch-chat 已提交请求回执';

-- 旧部署另执行 dispatch_chat_memory_request_id.sql；应用启动也会执行相同原位升级。
-- 会话代际升级另执行 dispatch_chat_memory_generation.sql，防止重置后旧摘要写回。

-- 兼容已部署过旧版本的数据库：幂等移除曾经创建的会话轮次外键。
SET @dispatch_chat_memory_fk_exists := (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND TABLE_NAME = 'dispatch_chat_memory_turn'
      AND CONSTRAINT_NAME = 'fk_dispatch_chat_memory_turn_session'
      AND CONSTRAINT_TYPE = 'FOREIGN KEY'
);
SET @dispatch_chat_memory_drop_fk_sql := IF(
    @dispatch_chat_memory_fk_exists > 0,
    'ALTER TABLE `dispatch_chat_memory_turn` DROP FOREIGN KEY `fk_dispatch_chat_memory_turn_session`',
    'SELECT 1'
);
PREPARE dispatch_chat_memory_drop_fk_stmt FROM @dispatch_chat_memory_drop_fk_sql;
EXECUTE dispatch_chat_memory_drop_fk_stmt;
DEALLOCATE PREPARE dispatch_chat_memory_drop_fk_stmt;

CREATE TABLE IF NOT EXISTS `dispatch_chat_group_context` (
    `scope_id`       VARCHAR(128) NOT NULL COMMENT 'HMAC Bot/群作用域',
    `messages_json`  MEDIUMTEXT   NOT NULL COMMENT '强类型最近群文本 JSON',
    `expires_at`     DATETIME(6)  NOT NULL COMMENT '滑动过期时间',
    `create_time`    DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
    `update_time`    DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间',
    PRIMARY KEY (`scope_id`),
    KEY `idx_dispatch_chat_group_context_expires` (`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='dispatch-chat 群聊最近文本';
