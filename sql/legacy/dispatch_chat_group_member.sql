-- dispatch-chat QQ 群成员目录（MySQL）
-- 旧实例可直接执行本文件；CREATE TABLE IF NOT EXISTS 不会清空已有数据。

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
