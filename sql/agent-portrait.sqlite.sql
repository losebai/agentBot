-- 用途：持久保存角色人物图；在 Agent 基础表建立后执行。
-- 升级起点：已完成基础升级的 0.6.3 数据库；新增独立表，不改写既有角色和密钥。
-- 数据影响：图片随角色保存事务更新，不参与临时媒体过期清理；删除角色时级联删除。
CREATE TABLE IF NOT EXISTS agent_bot_agent_portrait (
  -- 所属角色主键，一角色一张图；主键保证唯一，外键级联删除专属图片。
  agent_id INTEGER PRIMARY KEY REFERENCES agent_bot_agent(id) ON DELETE CASCADE,
  -- 经解码校验的 PNG、JPEG 或 WebP 类型。
  mime_type TEXT NOT NULL,
  -- 原始图片字节，应用限制最大五 MiB。
  image_data BLOB NOT NULL,
  -- 内容摘要，用于界面图片版本与完整性核对。
  sha256 TEXT NOT NULL,
  -- 图片更新 UTC 时间。
  updated_at DATETIME NOT NULL
);
