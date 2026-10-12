-- 第四阶段媒体表：在独立 agentBot 库依次执行 AI、Agent 初始化脚本后手动执行本脚本。
-- 仅创建不存在的表，不搬迁旧数据。媒体能力默认禁用，文件字节不存入数据库。
CREATE TABLE IF NOT EXISTS agent_bot_ai_model_media (
  model_id BIGINT UNSIGNED NOT NULL COMMENT '所属模型标识，同模型版本一起更新',
  vision_enabled TINYINT NOT NULL DEFAULT 0 COMMENT '是否明确启用图片理解能力',
  image_api_profile VARCHAR(32) NOT NULL DEFAULT 'none' COMMENT '图片接口类型：none、gpt-image 或 legacy-b64',
  audio_chat_enabled TINYINT NOT NULL DEFAULT 0 COMMENT '是否启用对话音频输出',
  PRIMARY KEY (model_id),
  -- 能力记录随模型删除，避免遗留无归属配置。
  CONSTRAINT fk_media_model FOREIGN KEY (model_id) REFERENCES agent_bot_ai_model(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模型媒体能力配置';

CREATE TABLE IF NOT EXISTS agent_bot_media_run (
  execution_id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '媒体执行唯一标识',
  entry VARCHAR(16) NOT NULL COMMENT '可信入口：admin 或 qq',
  session_id VARCHAR(256) COLLATE utf8mb4_bin NOT NULL COMMENT '所属会话标识',
  request_id VARCHAR(256) NOT NULL COMMENT '调用方请求标识，仅用于关联',
  model VARCHAR(255) NOT NULL COMMENT '本次配置的模型别名',
  backend VARCHAR(32) NOT NULL COMMENT '实际执行后端 python-media',
  output_type VARCHAR(16) NOT NULL COMMENT '媒体输出类型 IMAGE 或 AUDIO',
  status VARCHAR(16) NOT NULL COMMENT '状态 running、completed、failed 或 cancelled',
  duration_ms BIGINT UNSIGNED NOT NULL COMMENT '从准备到结束的耗时，单位毫秒',
  prompt MEDIUMTEXT NOT NULL COMMENT '受限用户请求正文，不含媒体字节',
  content MEDIUMTEXT NOT NULL COMMENT '模型返回文字或音频转写正文',
  error_code VARCHAR(100) NULL COMMENT '可公开的稳定错误码',
  created_at DATETIME(6) NOT NULL COMMENT '创建时间，统一为 UTC',
  PRIMARY KEY (execution_id),
  -- 时间与唯一标识组成稳定分页游标；会话索引支持过滤。
  KEY idx_media_run_created (created_at,execution_id),
  KEY idx_media_run_session (session_id,created_at,execution_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='媒体生成执行记录';

CREATE TABLE IF NOT EXISTS agent_bot_media_asset (
  id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '服务端生成的随机附件标识',
  entry VARCHAR(16) NOT NULL COMMENT '文件所属入口：admin 或 qq',
  session_id VARCHAR(256) COLLATE utf8mb4_bin NOT NULL COMMENT '文件所属会话，绑定输入时校验',
  execution_id CHAR(32) COLLATE utf8mb4_bin NULL COMMENT '可空执行标识，与执行类型成对出现',
  execution_kind VARCHAR(16) NULL COMMENT '执行类型 agent 或 media，未绑定时为空',
  purpose VARCHAR(16) NOT NULL COMMENT '附件用途 input 或 output',
  mime_type VARCHAR(64) NOT NULL COMMENT '校验后的媒体类型',
  size_bytes BIGINT UNSIGNED NOT NULL COMMENT '实际文件字节数，用于完整性及配额核对',
  sha256 CHAR(64) COLLATE utf8mb4_bin NOT NULL COMMENT '实际内容的 SHA256 摘要',
  storage_key CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '受限存储键，等于附件标识而非绝对路径',
  status VARCHAR(16) NOT NULL COMMENT '状态 writing、ready、expired 或 failed',
  created_at DATETIME(6) NOT NULL COMMENT '创建时间，统一为 UTC',
  expires_at DATETIME(6) NOT NULL COMMENT '到期时间，读取租约释放后方可清理文件',
  PRIMARY KEY (id),
  -- 执行关联支持两类运行记录，归属由服务层校验；清理索引避免扫描正文。
  KEY idx_media_asset_execution (execution_kind,execution_id),
  KEY idx_media_asset_expiry (status,expires_at,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='受认证媒体文件元数据';
