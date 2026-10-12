-- 仅用于升级已经按 2026-09-22-tagged-memory.sql 建立的旧表。
-- 执行前停止 dispatch-chat 并备份三张记忆表；此脚本执行一次，不用于全新安装。
-- MySQL DDL 会隐式提交，升级期间不能让应用继续读写这三张表。
SET @tagged_memory_original_sql_mode = @@SESSION.sql_mode;
SET SESSION sql_mode = CONCAT_WS(',', NULLIF(@@SESSION.sql_mode, ''), 'STRICT_ALL_TABLES');

ALTER TABLE dispatch_chat_tagged_memory
    ADD COLUMN id_new INT NOT NULL AUTO_INCREMENT COMMENT '记忆自增主键',
    ADD UNIQUE KEY uk_tagged_memory_id_new (id_new),
    ADD COLUMN is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '逻辑删除标记';

ALTER TABLE dispatch_chat_memory_tag
    ADD COLUMN memory_id_new INT NULL COMMENT '关联记忆的新整数主键';

CREATE TABLE IF NOT EXISTS dispatch_chat_memory_tag_orphan_archive (
    memory_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '无主记录的旧记忆 ID',
    tag VARCHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '原标签',
    archive_time DATETIME NOT NULL COMMENT '归档时间',
    PRIMARY KEY (memory_id, tag)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='标签记忆迁移时发现的孤儿关联';

INSERT IGNORE INTO dispatch_chat_memory_tag_orphan_archive (memory_id, tag, archive_time)
SELECT tag.memory_id, tag.tag, NOW()
FROM dispatch_chat_memory_tag AS tag
LEFT JOIN dispatch_chat_tagged_memory AS memory ON memory.id = tag.memory_id
WHERE memory.id IS NULL;

DELETE tag
FROM dispatch_chat_memory_tag AS tag
LEFT JOIN dispatch_chat_tagged_memory AS memory ON memory.id = tag.memory_id
WHERE memory.id IS NULL;

UPDATE dispatch_chat_memory_tag AS tag
JOIN dispatch_chat_tagged_memory AS memory ON memory.id = tag.memory_id
SET tag.memory_id_new = memory.id_new;

ALTER TABLE dispatch_chat_memory_tag
    MODIFY COLUMN memory_id_new INT NOT NULL COMMENT '关联记忆';

ALTER TABLE dispatch_chat_memory_tag
    DROP PRIMARY KEY,
    DROP INDEX idx_memory_tag_lookup,
    CHANGE COLUMN memory_id memory_id_legacy CHAR(64) CHARACTER SET ascii
        COLLATE ascii_bin NOT NULL COMMENT '旧关联记忆',
    CHANGE COLUMN memory_id_new memory_id INT NOT NULL COMMENT '关联记忆',
    ADD PRIMARY KEY (memory_id, tag),
    ADD KEY idx_memory_tag_lookup (tag, memory_id);

ALTER TABLE dispatch_chat_tagged_memory
    DROP PRIMARY KEY,
    DROP INDEX uk_tagged_memory_id_new,
    DROP INDEX idx_tagged_memory_scope_time,
    CHANGE COLUMN id id_legacy CHAR(64) CHARACTER SET ascii
        COLLATE ascii_bin NOT NULL COMMENT '旧作用域与记忆键摘要',
    CHANGE COLUMN id_new id INT NOT NULL AUTO_INCREMENT COMMENT '记忆自增主键',
    ADD PRIMARY KEY (id),
    ADD KEY idx_tagged_memory_scope_time (scope_id, is_deleted, update_time);

ALTER TABLE dispatch_chat_memory_tag DROP COLUMN memory_id_legacy;
ALTER TABLE dispatch_chat_tagged_memory DROP COLUMN id_legacy;
SET SESSION sql_mode = @tagged_memory_original_sql_mode;
