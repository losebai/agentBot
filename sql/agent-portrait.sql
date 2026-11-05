-- 用途：持久保存角色人物图；在 Agent 基础表建立后执行。
-- 升级起点：已完成基础升级的 0.6.3 数据库；新增独立表，不改写既有角色和密钥。
-- 数据影响：图片随角色保存事务更新，不参与临时媒体过期清理；删除角色时级联删除。
CREATE TABLE IF NOT EXISTS agent_bot_agent_portrait (
  agent_id BIGINT UNSIGNED NOT NULL PRIMARY KEY COMMENT '所属角色主键，一角色一张图，主键保证唯一',
  mime_type VARCHAR(32) NOT NULL COMMENT '经解码校验的 PNG、JPEG 或 WebP 类型',
  image_data MEDIUMBLOB NOT NULL COMMENT '原始图片字节，应用限制最大五 MiB',
  sha256 CHAR(64) NOT NULL COMMENT '内容摘要，用于界面图片版本与完整性核对',
  updated_at DATETIME(6) NOT NULL COMMENT '图片更新 UTC 时间',
  -- 角色删除后同事务移除专属图片，不留下孤立人物图。
  CONSTRAINT fk_agent_portrait_owner FOREIGN KEY (agent_id) REFERENCES agent_bot_agent(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='角色专属持久人物图，不设附件过期时间';
