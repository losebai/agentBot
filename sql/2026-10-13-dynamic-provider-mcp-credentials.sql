-- 用途：将模型供应商和 MCP 凭据改为数据库动态维护。
-- 执行顺序：仅在既有 agent_bot_ai_provider、agent_bot_mcp 表上执行一次，先增加字段，再标记旧引用待录入。
-- 数据影响：不删除或重建表，不读取环境变量；旧引用字段保留，旧鉴权记录需在管理页面重新录入密钥。
-- 新库应使用 agent_bot_init.sql，不重复执行本增量脚本。

ALTER TABLE agent_bot_ai_provider
  ADD COLUMN credential TEXT NULL COMMENT '供应商 API 密钥，仅写入和内部调用，不通过管理接口回显' AFTER base_url,
  ADD COLUMN credential_required TINYINT NOT NULL DEFAULT 0 COMMENT '是否要求鉴权，旧引用待录入时保持为一' AFTER credential;

ALTER TABLE agent_bot_mcp
  ADD COLUMN credential TEXT NULL COMMENT 'MCP Bearer 访问令牌，仅写入和内部调用，不通过管理接口回显' AFTER endpoint,
  ADD COLUMN credential_required TINYINT NOT NULL DEFAULT 0 COMMENT '是否要求鉴权，旧引用待录入时保持为一' AFTER credential;

-- 保留旧记录的鉴权要求，版本递增防止迁移前的编辑草稿覆盖新状态。
UPDATE agent_bot_ai_provider
SET credential_required = 1, revision = revision + 1, updated_at = UTC_TIMESTAMP(6)
WHERE credential_ref IS NOT NULL AND credential_ref <> '';

UPDATE agent_bot_mcp
SET credential_required = 1, revision = revision + 1, updated_at = UTC_TIMESTAMP(6)
WHERE credential_ref IS NOT NULL AND credential_ref <> '';
