-- 表情包图库增量，适用完整 0.5.5 至 0.5.10 业务库；按登记清单在既有增量后执行。
-- 仅新增独立表与索引，不改写已有配置、凭据或业务数据；可重入，不自动启用插件。
CREATE TABLE IF NOT EXISTS agent_bot_sticker (
    id TEXT PRIMARY KEY, -- 服务端随机32位十六进制标识，不接受客户端路径
    name TEXT NOT NULL, -- 管理员命名
    tags_json TEXT NOT NULL, -- 管理员维护的有序标签JSON数组
    description TEXT NOT NULL, -- 识图或管理员维护的画面说明
    emotion TEXT NOT NULL, -- 主要情绪
    usage_hint TEXT NOT NULL, -- 适用聊天场景
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)), -- 停用后禁止机器人发送
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1), -- 乐观更新修订
    mime_type TEXT NOT NULL, -- 已验证原件容器的媒体类型
    size_bytes INTEGER NOT NULL CHECK (size_bytes > 0), -- 原件字节数
    sha256 TEXT NOT NULL, -- 原件完整性摘要，墓碑仍保留
    active_sha256 TEXT NULL UNIQUE, -- 只对活跃原件去重，删除设NULL允许再次上传
    width INTEGER NOT NULL CHECK (width > 0), -- 原件画布宽
    height INTEGER NOT NULL CHECK (height > 0), -- 原件画布高
    frame_count INTEGER NOT NULL CHECK (frame_count > 0), -- 已解码验证帧数
    animated INTEGER NOT NULL CHECK (animated IN (0,1)), -- 原件是否为GIF动画
    analysis_status TEXT NOT NULL DEFAULT 'pending'
      CHECK (analysis_status IN ('pending','ready','failed')), -- 识图生命周期
    analysis_error TEXT NULL, -- 仅保存安全错误码，不保存供应商正文
    storage_key TEXT NOT NULL UNIQUE, -- 原件服务端私有文件键
    preview_key TEXT NOT NULL UNIQUE, -- 静态PNG预览服务端私有文件键
    preview_size_bytes INTEGER NOT NULL CHECK (preview_size_bytes > 0), -- 预览完整性大小
    preview_sha256 TEXT NOT NULL, -- 预览完整性摘要
    deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0,1)), -- 删除墓碑先阻止访问再清理文件
    created_at DATETIME NOT NULL, -- UTC创建时间
    updated_at DATETIME NOT NULL -- UTC最后修订时间
);
-- 活跃及启用过滤后按创建时间分页，不扫描已删除图库。
CREATE INDEX IF NOT EXISTS ix_sticker_active_created
    ON agent_bot_sticker(deleted, enabled, created_at, id);
