-- agentBot new database initialization; does not drop existing tables.
-- dispatch-chat 配置表（MySQL）
-- 键值表设计：每个配置项一行，所有运行配置以明文存储；版本号单独管理。
-- 表结构必须与本文件一致；dispatch-chat 启动时会校验列结构并拒绝不兼容的表。

-- 配置键值表
-- config_value 存放 JSON 编码后的标量/数组/对象（字符串、数字、bool、list、dict）。
CREATE TABLE IF NOT EXISTS `dispatch_chat_setting` (
    `id`            BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    `config_key`    VARCHAR(128) NOT NULL                COMMENT '配置键名（RuntimeConfig 字段名）',
    `config_value`  MEDIUMTEXT                           COMMENT '配置值（JSON 编码）',
    `description`   VARCHAR(256)                         COMMENT '配置项中文说明',
    `create_time`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `update_time`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_config_key` (`config_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='dispatch-chat 配置键值表';

-- 配置版本号表（单行）
-- 用于整份配置快照的乐观锁；initialize 写入 revision=1，每次 save 自增。
CREATE TABLE IF NOT EXISTS `dispatch_chat_setting_revision` (
    `id`           BIGINT   NOT NULL COMMENT '固定为 1，保证单行',
    `revision`     BIGINT   NOT NULL COMMENT '当前配置快照版本号，从 1 起',
    `update_time`  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`id`),
    CONSTRAINT `chk_dispatch_chat_setting_revision_singleton` CHECK (`id` = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='dispatch-chat 配置版本号（单行）';

-- QQ 群成员目录（新安装一并创建；已有安装也可单独执行 dispatch_chat_group_member.sql）
CREATE TABLE IF NOT EXISTS `dispatch_chat_group_member` (
    `bot_id`          VARCHAR(20)  NOT NULL COMMENT '机器人 QQ 号',
    `group_id`        VARCHAR(20)  NOT NULL COMMENT 'QQ群号',
    `user_id`         VARCHAR(20)  NOT NULL COMMENT '群成员 QQ 号',
    `group_name`      VARCHAR(128)          COMMENT '群名称',
    `nickname`        VARCHAR(128)          COMMENT 'QQ 昵称',
    `card`            VARCHAR(128)          COMMENT '群名片',
    `member_role`     VARCHAR(16)  NOT NULL DEFAULT 'member' COMMENT 'owner/admin/member',
    `join_time`       BIGINT UNSIGNED        COMMENT '入群时间（Unix 秒）',
    `last_sent_time`  BIGINT UNSIGNED        COMMENT '最后发言时间（Unix 秒）',
    `create_time`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `update_time`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`bot_id`, `group_id`, `user_id`),
    KEY `idx_dispatch_chat_group_member_user` (`bot_id`, `user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='NapCat OneBot QQ群成员目录';

-- 持久聊天记忆（旧安装也可单独执行 dispatch_chat_memory.sql）
CREATE TABLE IF NOT EXISTS `dispatch_chat_memory_session` (
    `session_id`                 VARCHAR(128) NOT NULL COMMENT 'HMAC 会话标识',
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
    `user_content`       MEDIUMTEXT   NOT NULL COMMENT '用户历史文本',
    `assistant_content`  MEDIUMTEXT   NOT NULL COMMENT '已投递助手内容',
    `create_time`        DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
    PRIMARY KEY (`id`),
    KEY `idx_dispatch_chat_memory_turn_session_id` (`session_id`, `id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='dispatch-chat 最近会话轮次';

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
