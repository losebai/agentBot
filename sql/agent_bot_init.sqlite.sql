-- 用途：显式初始化 SQLite 的全部 27 张业务表。
-- 执行顺序：由 agent-bot database init 在启用外键的事务中执行，再运行 settings init。
-- 数据影响：仅创建缺失结构，不清空数据、不写入配置或密钥。
-- SQLite 不支持 COMMENT，以下中文注释保留表、字段、索引与外键设计目的。

-- chat 配置键值表
CREATE TABLE IF NOT EXISTS chat_setting (
    -- 主键
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 配置键名（RuntimeConfig 字段名）
    `config_key` TEXT COLLATE NOCASE NOT NULL,
    -- 配置值（JSON 编码）
    `config_value` TEXT COLLATE NOCASE,
    -- 配置项中文说明
    `description` TEXT COLLATE NOCASE,
    -- 创建时间
    `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- 更新时间
    `update_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- 唯一索引保护业务标识。
CREATE UNIQUE INDEX IF NOT EXISTS uk_config_key ON chat_setting (`config_key`);
-- 更新业务字段时同步记录更新时间，不覆盖应用显式设置的时间。
CREATE TRIGGER IF NOT EXISTS chat_setting_update_time AFTER UPDATE ON chat_setting
WHEN NEW.update_time = OLD.update_time BEGIN
    UPDATE chat_setting SET update_time=CURRENT_TIMESTAMP WHERE id=NEW.id;
END;

-- chat 配置版本号（单行）
CREATE TABLE IF NOT EXISTS chat_setting_revision (
    -- 固定为 1，保证单行
    `id` INTEGER NOT NULL,
    -- 当前配置快照版本号，从 1 起
    `revision` INTEGER NOT NULL,
    -- 更新时间
    `update_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    CONSTRAINT `chk_chat_setting_revision_singleton` CHECK (`id` = 1)
);
-- 更新业务字段时同步记录更新时间，不覆盖应用显式设置的时间。
CREATE TRIGGER IF NOT EXISTS chat_setting_revision_update_time AFTER UPDATE ON chat_setting_revision
WHEN NEW.update_time = OLD.update_time BEGIN
    UPDATE chat_setting_revision SET update_time=CURRENT_TIMESTAMP WHERE id=NEW.id;
END;

-- NapCat OneBot QQ群成员目录
CREATE TABLE IF NOT EXISTS chat_group_member (
    -- 机器人 QQ 号
    `bot_id` TEXT COLLATE NOCASE NOT NULL,
    -- QQ群号
    `group_id` TEXT COLLATE NOCASE NOT NULL,
    -- 群成员 QQ 号
    `user_id` TEXT COLLATE NOCASE NOT NULL,
    -- 群名称
    `group_name` TEXT COLLATE NOCASE,
    -- QQ 昵称
    `nickname` TEXT COLLATE NOCASE,
    -- 群名片
    `card` TEXT COLLATE NOCASE,
    -- owner/admin/member
    `member_role` TEXT COLLATE NOCASE NOT NULL DEFAULT 'member',
    -- 入群时间（Unix 秒）
    `join_time` INTEGER CHECK (join_time >= 0),
    -- 最后发言时间（Unix 秒）
    `last_sent_time` INTEGER CHECK (last_sent_time >= 0),
    -- 创建时间
    `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- 更新时间
    `update_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`bot_id`, `group_id`, `user_id`)
);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_chat_group_member_user ON chat_group_member (`bot_id`, `user_id`);
-- 更新业务字段时同步记录更新时间，不覆盖应用显式设置的时间。
CREATE TRIGGER IF NOT EXISTS chat_group_member_update_time AFTER UPDATE ON chat_group_member
WHEN NEW.update_time = OLD.update_time BEGIN
    UPDATE chat_group_member SET update_time=CURRENT_TIMESTAMP WHERE bot_id=NEW.bot_id AND group_id=NEW.group_id AND user_id=NEW.user_id;
END;

-- chat 持久会话记忆
CREATE TABLE IF NOT EXISTS chat_memory_session (
    -- HMAC 会话标识
    `session_id` TEXT COLLATE NOCASE NOT NULL,
    -- 会话代际标识，重建时重新生成
    `generation_id` TEXT COLLATE NOCASE NOT NULL,
    -- 压缩后的会话摘要
    `memory_summary` TEXT COLLATE NOCASE NOT NULL,
    -- 摘要乐观锁版本
    `memory_revision` INTEGER NOT NULL DEFAULT 0,
    -- 已压缩最大轮次 ID
    `compacted_through_turn_id` INTEGER NOT NULL DEFAULT 0,
    -- 滑动过期时间
    `expires_at` DATETIME NOT NULL,
    -- 创建时间
    `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- 更新时间
    `update_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`session_id`)
);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_chat_memory_session_expires ON chat_memory_session (`expires_at`);
-- 更新业务字段时同步记录更新时间，不覆盖应用显式设置的时间。
CREATE TRIGGER IF NOT EXISTS chat_memory_session_update_time AFTER UPDATE ON chat_memory_session
WHEN NEW.update_time = OLD.update_time BEGIN
    UPDATE chat_memory_session SET update_time=CURRENT_TIMESTAMP WHERE session_id=NEW.session_id;
END;

-- chat 最近会话轮次
CREATE TABLE IF NOT EXISTS chat_memory_turn (
    -- 轮次主键
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- HMAC 会话标识
    `session_id` TEXT COLLATE NOCASE NOT NULL,
    -- 幂等请求标识，旧轮次为空
    `request_id` TEXT NULL,
    -- 用户历史文本
    `user_content` TEXT COLLATE NOCASE NOT NULL,
    -- 已投递助手内容
    `assistant_content` TEXT COLLATE NOCASE NOT NULL,
    -- 创建时间
    `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_chat_memory_turn_session_id ON chat_memory_turn (`session_id`, `id`);
-- 唯一索引保护业务标识。
CREATE UNIQUE INDEX IF NOT EXISTS uk_chat_memory_turn_request ON chat_memory_turn (`session_id`, `request_id`);

-- chat 已提交请求回执
CREATE TABLE IF NOT EXISTS chat_memory_receipt (
    -- HMAC 会话标识
    `session_id` TEXT COLLATE NOCASE NOT NULL,
    -- 幂等请求标识
    `request_id` TEXT NOT NULL,
    -- 独立重试去重截止时间
    `expires_at` DATETIME NOT NULL,
    PRIMARY KEY (`session_id`, `request_id`)
);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_chat_memory_receipt_expires ON chat_memory_receipt (`expires_at`);

-- chat 群聊最近文本
CREATE TABLE IF NOT EXISTS chat_group_context (
    -- HMAC Bot/群作用域
    `scope_id` TEXT COLLATE NOCASE NOT NULL,
    -- 强类型最近群文本 JSON
    `messages_json` TEXT COLLATE NOCASE NOT NULL,
    -- 滑动过期时间
    `expires_at` DATETIME NOT NULL,
    -- 创建时间
    `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- 更新时间
    `update_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`scope_id`)
);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_chat_group_context_expires ON chat_group_context (`expires_at`);
-- 更新业务字段时同步记录更新时间，不覆盖应用显式设置的时间。
CREATE TRIGGER IF NOT EXISTS chat_group_context_update_time AFTER UPDATE ON chat_group_context
WHEN NEW.update_time = OLD.update_time BEGIN
    UPDATE chat_group_context SET update_time=CURRENT_TIMESTAMP WHERE scope_id=NEW.scope_id;
END;

-- 标签记忆作用域与写锁
CREATE TABLE IF NOT EXISTS chat_memory_scope (
    -- 匿名作用域键
    scope_id TEXT NOT NULL,
    -- 记忆作用域：全局、会话、用户或群组
    scope TEXT COLLATE NOCASE NOT NULL,
    PRIMARY KEY (scope_id)
);

-- LLM 自主保存的标签记忆
CREATE TABLE IF NOT EXISTS chat_tagged_memory (
    -- 记忆自增主键
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 关联作用域
    scope_id TEXT NOT NULL,
    -- 稳定事实键
    memory_key TEXT NOT NULL,
    -- 已确认的事实
    content TEXT COLLATE NOCASE NOT NULL,
    -- 逻辑删除标记
    is_deleted INTEGER NOT NULL DEFAULT 0,
    -- 创建时间
    create_time DATETIME NOT NULL,
    -- 更新时间
    update_time DATETIME NOT NULL
);
-- 唯一索引保护业务标识。
CREATE UNIQUE INDEX IF NOT EXISTS uk_tagged_memory_scope_key ON chat_tagged_memory (scope_id, memory_key);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_tagged_memory_scope_time ON chat_tagged_memory (scope_id, is_deleted, update_time);

-- 记忆与标签多对多关联
CREATE TABLE IF NOT EXISTS chat_memory_tag (
    -- 关联记忆
    memory_id INTEGER NOT NULL,
    -- 规范化主题标签
    tag TEXT NOT NULL,
    PRIMARY KEY (memory_id, tag)
);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_memory_tag_lookup ON chat_memory_tag (tag, memory_id);

-- 本地 Comfy 工作流定义与执行绑定
CREATE TABLE IF NOT EXISTS chat_workflow (
    -- 主键
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 稳定工作流键
    `workflow_key` TEXT COLLATE NOCASE NOT NULL,
    -- 工作流显示名称
    `display_name` TEXT COLLATE NOCASE NOT NULL,
    -- 导入来源文件名
    `source_file` TEXT COLLATE NOCASE NOT NULL,
    -- 来源格式：ui 或 api
    `source_format` TEXT COLLATE NOCASE NOT NULL,
    -- ComfyUI 画布工作流 JSON
    `ui_json` TEXT COLLATE NOCASE NULL,
    -- 可提交给 ComfyUI 的 API 工作流 JSON
    `api_json` TEXT COLLATE NOCASE NULL,
    -- 正向提示词节点 ID
    `positive_node_id` TEXT COLLATE NOCASE NULL,
    -- 负向提示词节点 ID
    `negative_node_id` TEXT COLLATE NOCASE NULL,
    -- 图片输出节点 ID
    `output_node_id` TEXT COLLATE NOCASE NULL,
    -- 随机种子所在节点 ID
    `seed_node_id` TEXT COLLATE NOCASE NULL,
    -- 工作流类型
    `workflow_type` TEXT COLLATE NOCASE NOT NULL DEFAULT 'text_to_image',
    -- 用户原图 LoadImage 节点 ID
    `input_image_node_id` TEXT COLLATE NOCASE NULL,
    -- 正向或编辑指令字段名
    `positive_input_name` TEXT COLLATE NOCASE NOT NULL DEFAULT 'text',
    -- 负向提示词字段名
    `negative_input_name` TEXT COLLATE NOCASE NOT NULL DEFAULT 'text',
    -- 供 Agent 选择工作流的用途描述
    `description` TEXT COLLATE NOCASE NOT NULL DEFAULT '',
    -- 生成时固定追加的正向提示词
    `additional_positive_prompt` TEXT COLLATE NOCASE NOT NULL DEFAULT '',
    -- 生成时固定追加的负向提示词
    `additional_negative_prompt` TEXT COLLATE NOCASE NOT NULL DEFAULT '',
    -- 是否允许 Agent 执行
    `enabled` INTEGER NOT NULL DEFAULT 1,
    -- 规范化来源内容 SHA-256
    `content_sha256` TEXT COLLATE NOCASE NOT NULL,
    -- 创建时间
    `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- 更新时间
    `update_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- 唯一索引保护业务标识。
CREATE UNIQUE INDEX IF NOT EXISTS uk_chat_workflow_key ON chat_workflow (`workflow_key`);
-- 唯一索引保护业务标识。
CREATE UNIQUE INDEX IF NOT EXISTS uk_chat_workflow_content_sha256 ON chat_workflow (`content_sha256`);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_chat_workflow_enabled ON chat_workflow (`enabled`);
-- 更新业务字段时同步记录更新时间，不覆盖应用显式设置的时间。
CREATE TRIGGER IF NOT EXISTS chat_workflow_update_time AFTER UPDATE ON chat_workflow
WHEN NEW.update_time = OLD.update_time BEGIN
    UPDATE chat_workflow SET update_time=CURRENT_TIMESTAMP WHERE id=NEW.id;
END;

-- 模型供应商配置
CREATE TABLE IF NOT EXISTS agent_bot_ai_provider (
    -- 自增主键
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK (id >= 0),
    -- 显示名称
    name TEXT NOT NULL,
    -- 供应商接口协议
    protocol TEXT COLLATE NOCASE NOT NULL,
    -- 供应商接口根地址
    base_url TEXT COLLATE NOCASE NOT NULL,
    -- 供应商或 MCP 实际凭据，仅写入和内部调用，不通过管理接口回显
    credential TEXT COLLATE NOCASE NULL,
    -- 是否要求鉴权：零无需凭据，一必须配置凭据
    credential_required INTEGER NOT NULL DEFAULT 0,
    -- 是否启用：零禁用，一启用
    enabled INTEGER NOT NULL DEFAULT 0,
    -- 中文用途说明
    description TEXT COLLATE NOCASE NOT NULL DEFAULT '',
    -- 乐观锁版本，每次修改递增
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 0),
    -- 创建时间，协调世界时
    created_at DATETIME NOT NULL,
    -- 更新时间，协调世界时
    updated_at DATETIME NOT NULL
);
-- 唯一索引保护业务标识。
CREATE UNIQUE INDEX IF NOT EXISTS uq_provider_name ON agent_bot_ai_provider (name);

-- 模型别名目录
CREATE TABLE IF NOT EXISTS agent_bot_ai_model (
    -- 自增主键
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK (id >= 0),
    -- 区分大小写的模型别名
    alias TEXT NOT NULL,
    -- 上游实际模型标识
    upstream_model TEXT NOT NULL,
    -- 所属供应商主键
    provider_id INTEGER NOT NULL CHECK (provider_id >= 0),
    -- 是否启用：零禁用，一启用
    enabled INTEGER NOT NULL DEFAULT 0,
    -- 乐观锁版本，每次修改递增
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 0),
    -- 创建时间，协调世界时
    created_at DATETIME NOT NULL,
    -- 更新时间，协调世界时
    updated_at DATETIME NOT NULL,
    -- 外键保护引用完整性，保留级联删除规则。
    CONSTRAINT fk_agent_bot_model_provider FOREIGN KEY (provider_id) REFERENCES agent_bot_ai_provider (id)
);
-- 唯一索引保护业务标识。
CREATE UNIQUE INDEX IF NOT EXISTS uq_model_alias ON agent_bot_ai_model (alias);

-- 普通文本调用记录
CREATE TABLE IF NOT EXISTS agent_bot_ai_session (
    -- 执行标识，三十二位十六进制
    execution_id TEXT NOT NULL,
    -- 区分大小写的会话标识
    session_id TEXT NOT NULL,
    -- 调用方请求标识
    request_id TEXT COLLATE NOCASE NOT NULL,
    -- 请求指定的模型别名
    configured_model TEXT COLLATE NOCASE NOT NULL,
    -- 实际使用的模型标识
    model TEXT COLLATE NOCASE NOT NULL,
    -- 执行状态：运行中、完成、失败或取消
    status TEXT COLLATE NOCASE NOT NULL,
    -- 执行耗时，单位毫秒
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    -- 令牌用量统计
    `usage` TEXT COLLATE NOCASE NOT NULL CHECK (json_valid(usage)),
    -- 创建时间，协调世界时
    created_at DATETIME NOT NULL,
    -- 稳定错误代码
    error_code TEXT COLLATE NOCASE NULL,
    -- 用户输入文本
    prompt TEXT COLLATE NOCASE NOT NULL,
    -- 模型输出文本
    content TEXT COLLATE NOCASE NOT NULL,
    PRIMARY KEY (execution_id)
);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_session_created ON agent_bot_ai_session (session_id, created_at, execution_id);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_created ON agent_bot_ai_session (created_at, execution_id);

-- 智能体执行记录
CREATE TABLE IF NOT EXISTS agent_bot_agent_run (
    -- 执行标识，三十二位十六进制
    execution_id TEXT NOT NULL PRIMARY KEY,
    -- 区分大小写的会话标识
    session_id TEXT NOT NULL,
    -- 调用方请求标识
    request_id TEXT COLLATE NOCASE NOT NULL,
    -- 调用入口：管理端或机器人
    entry TEXT COLLATE NOCASE NOT NULL,
    -- 实际使用的模型标识
    model TEXT COLLATE NOCASE NOT NULL,
    -- 执行后端标识
    backend TEXT COLLATE NOCASE NOT NULL,
    -- 执行状态：运行中、完成、失败或取消
    `status` TEXT COLLATE NOCASE NOT NULL,
    -- 执行耗时，单位毫秒
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    -- 令牌用量统计
    `usage` TEXT COLLATE NOCASE NOT NULL CHECK (json_valid(usage)),
    -- 创建时间，协调世界时
    created_at DATETIME NOT NULL,
    -- 稳定错误代码
    error_code TEXT COLLATE NOCASE NULL,
    -- 用户输入文本
    prompt TEXT COLLATE NOCASE NOT NULL,
    -- 模型输出文本
    content TEXT COLLATE NOCASE NOT NULL,
    -- 本次执行使用的配置版本快照
    revisions TEXT COLLATE NOCASE NOT NULL CHECK (json_valid(revisions))
);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_agent_run_created ON agent_bot_agent_run (created_at, execution_id);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_agent_run_session ON agent_bot_agent_run (session_id, created_at, execution_id);

-- 智能体执行步骤
CREATE TABLE IF NOT EXISTS agent_bot_agent_step (
    -- 执行标识，三十二位十六进制
    execution_id TEXT NOT NULL,
    -- 执行内递增事件序号
    sequence INTEGER NOT NULL,
    -- 模型调用轮次
    turn INTEGER NOT NULL,
    -- 步骤事件类型
    kind TEXT COLLATE NOCASE NOT NULL,
    -- 工具名称
    tool TEXT COLLATE NOCASE NULL,
    -- 模型工具调用标识
    call_id TEXT COLLATE NOCASE NULL,
    -- 执行状态：运行中、完成、失败或取消
    status TEXT COLLATE NOCASE NOT NULL,
    -- 执行耗时，单位毫秒
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    -- 脱敏后的步骤摘要
    summary TEXT COLLATE NOCASE NOT NULL,
    PRIMARY KEY(execution_id,sequence),
    -- 外键保护引用完整性，保留级联删除规则。
    CONSTRAINT fk_agent_step_run FOREIGN KEY(execution_id) REFERENCES agent_bot_agent_run(execution_id)
);

-- 技能目录
CREATE TABLE IF NOT EXISTS agent_bot_skill (
    -- 自增主键
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK (id >= 0),
    -- 显示名称
    name TEXT COLLATE NOCASE NOT NULL,
    -- 中文用途说明
    description TEXT COLLATE NOCASE NOT NULL,
    -- 功能分类
    category TEXT COLLATE NOCASE NOT NULL,
    -- 系统提示词
    system_prompt TEXT COLLATE NOCASE NOT NULL,
    -- 示例提示词
    sample_prompt TEXT COLLATE NOCASE NOT NULL,
    -- 允许使用的本地工具列表
    tools TEXT COLLATE NOCASE NOT NULL CHECK (json_valid(tools)),
    -- 是否启用：零禁用，一启用
    enabled INTEGER NOT NULL DEFAULT 0,
    -- 乐观锁版本，每次修改递增
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 0),
    -- 创建时间，协调世界时
    created_at DATETIME NOT NULL,
    -- 更新时间，协调世界时
    updated_at DATETIME NOT NULL
);

-- 远程工具服务目录
CREATE TABLE IF NOT EXISTS agent_bot_mcp (
    -- 自增主键
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK (id >= 0),
    -- 区分大小写的唯一代码
    code TEXT NOT NULL UNIQUE,
    -- 显示名称
    name TEXT COLLATE NOCASE NOT NULL,
    -- 中文用途说明
    description TEXT COLLATE NOCASE NOT NULL,
    -- 功能分类
    category TEXT COLLATE NOCASE NOT NULL,
    -- 远程工具传输方式
    transport TEXT COLLATE NOCASE NOT NULL,
    -- 远程工具服务端点
    endpoint TEXT COLLATE NOCASE NOT NULL,
    -- 供应商或 MCP 实际凭据，仅写入和内部调用，不通过管理接口回显
    credential TEXT COLLATE NOCASE NULL,
    -- 是否要求鉴权：零无需凭据，一必须配置凭据
    credential_required INTEGER NOT NULL DEFAULT 0,
    -- 允许使用的远程工具列表
    allowed_tools TEXT COLLATE NOCASE NOT NULL CHECK (json_valid(allowed_tools)),
    -- 搜索工具名称
    search_tool TEXT COLLATE NOCASE NULL,
    -- 调用超时时间，单位秒
    timeout_seconds INTEGER NOT NULL,
    -- 是否为执行必需服务
    required INTEGER NOT NULL DEFAULT 0,
    -- 是否启用：零禁用，一启用
    enabled INTEGER NOT NULL DEFAULT 0,
    -- 乐观锁版本，每次修改递增
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 0),
    -- 创建时间，协调世界时
    created_at DATETIME NOT NULL,
    -- 更新时间，协调世界时
    updated_at DATETIME NOT NULL
);

-- 智能体配置
CREATE TABLE IF NOT EXISTS agent_bot_agent (
    -- 自增主键
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK (id >= 0),
    -- 区分大小写的唯一代码
    code TEXT NOT NULL UNIQUE,
    -- 显示名称
    name TEXT COLLATE NOCASE NOT NULL,
    -- 中文用途说明
    description TEXT COLLATE NOCASE NOT NULL,
    -- 系统提示词
    system_prompt TEXT COLLATE NOCASE NOT NULL,
    -- 关联的模型主键
    model_id INTEGER NOT NULL CHECK (model_id >= 0),
    -- 允许使用的本地工具列表
    tools TEXT COLLATE NOCASE NOT NULL CHECK (json_valid(tools)),
    -- 最大工具调用轮次
    max_steps INTEGER NOT NULL,
    -- 模型生成参数
    options TEXT COLLATE NOCASE NOT NULL CHECK (json_valid(options)),
    -- 是否启用：零禁用，一启用
    enabled INTEGER NOT NULL DEFAULT 0,
    -- 乐观锁版本，每次修改递增
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 0),
    -- 创建时间，协调世界时
    created_at DATETIME NOT NULL,
    -- 更新时间，协调世界时
    updated_at DATETIME NOT NULL,
    -- 外键保护引用完整性，保留级联删除规则。
    CONSTRAINT fk_agent_model FOREIGN KEY(model_id) REFERENCES agent_bot_ai_model(id)
);

-- 智能体技能关联
CREATE TABLE IF NOT EXISTS agent_bot_agent_skill (
    -- 关联的智能体主键
    agent_id INTEGER NOT NULL CHECK (agent_id >= 0),
    -- 关联的技能主键
    skill_id INTEGER NOT NULL CHECK (skill_id >= 0),
    PRIMARY KEY(agent_id,skill_id),
    -- 外键保护引用完整性，保留级联删除规则。
    CONSTRAINT fk_agent_skill_agent FOREIGN KEY(agent_id) REFERENCES agent_bot_agent(id) ON DELETE CASCADE,
    -- 外键保护引用完整性，保留级联删除规则。
    CONSTRAINT fk_agent_skill_skill FOREIGN KEY(skill_id) REFERENCES agent_bot_skill(id)
);

-- 智能体远程工具关联
CREATE TABLE IF NOT EXISTS agent_bot_agent_mcp (
    -- 关联的智能体主键
    agent_id INTEGER NOT NULL CHECK (agent_id >= 0),
    -- 关联的远程工具主键
    mcp_id INTEGER NOT NULL CHECK (mcp_id >= 0),
    PRIMARY KEY(agent_id,mcp_id),
    -- 外键保护引用完整性，保留级联删除规则。
    CONSTRAINT fk_agent_mcp_agent FOREIGN KEY(agent_id) REFERENCES agent_bot_agent(id) ON DELETE CASCADE,
    -- 外键保护引用完整性，保留级联删除规则。
    CONSTRAINT fk_agent_mcp_mcp FOREIGN KEY(mcp_id) REFERENCES agent_bot_mcp(id)
);

-- 模型媒体能力配置
CREATE TABLE IF NOT EXISTS agent_bot_ai_model_media (
    -- 所属模型标识，同模型版本一起更新
    model_id INTEGER NOT NULL CHECK (model_id >= 0),
    -- 是否明确启用图片理解能力
    vision_enabled INTEGER NOT NULL DEFAULT 0,
    -- 图片接口类型：none、gpt-image 或 legacy-b64
    image_api_profile TEXT COLLATE NOCASE NOT NULL DEFAULT 'none',
    -- 是否启用对话音频输出
    audio_chat_enabled INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (model_id),
    -- 外键保护引用完整性，保留级联删除规则。
    CONSTRAINT fk_media_model FOREIGN KEY (model_id) REFERENCES agent_bot_ai_model(id) ON DELETE CASCADE
);

-- 媒体生成执行记录
CREATE TABLE IF NOT EXISTS agent_bot_media_run (
    -- 媒体执行唯一标识
    execution_id TEXT NOT NULL,
    -- 可信入口：admin 或 qq
    entry TEXT COLLATE NOCASE NOT NULL,
    -- 所属会话标识
    session_id TEXT NOT NULL,
    -- 调用方请求标识，仅用于关联
    request_id TEXT COLLATE NOCASE NOT NULL,
    -- 本次配置的模型别名
    model TEXT COLLATE NOCASE NOT NULL,
    -- 实际执行后端 python-media
    backend TEXT COLLATE NOCASE NOT NULL,
    -- 媒体输出类型 IMAGE 或 AUDIO
    output_type TEXT COLLATE NOCASE NOT NULL,
    -- 状态 running、completed、failed 或 cancelled
    status TEXT COLLATE NOCASE NOT NULL,
    -- 从准备到结束的耗时，单位毫秒
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    -- 受限用户请求正文，不含媒体字节
    prompt TEXT COLLATE NOCASE NOT NULL,
    -- 模型返回文字或音频转写正文
    content TEXT COLLATE NOCASE NOT NULL,
    -- 可公开的稳定错误码
    error_code TEXT COLLATE NOCASE NULL,
    -- 创建时间，统一为 UTC
    created_at DATETIME NOT NULL,
    PRIMARY KEY (execution_id)
);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_media_run_created ON agent_bot_media_run (created_at,execution_id);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_media_run_session ON agent_bot_media_run (session_id,created_at,execution_id);

-- 受认证媒体文件元数据
CREATE TABLE IF NOT EXISTS agent_bot_media_asset (
    -- 服务端生成的随机附件标识
    id TEXT NOT NULL,
    -- 文件所属入口：admin 或 qq
    entry TEXT COLLATE NOCASE NOT NULL,
    -- 文件所属会话，绑定输入时校验
    session_id TEXT NOT NULL,
    -- 可空执行标识，与执行类型成对出现
    execution_id TEXT NULL,
    -- 执行类型 agent 或 media，未绑定时为空
    execution_kind TEXT COLLATE NOCASE NULL,
    -- 附件用途 input 或 output
    purpose TEXT COLLATE NOCASE NOT NULL,
    -- 校验后的媒体类型
    mime_type TEXT COLLATE NOCASE NOT NULL,
    -- 实际文件字节数，用于完整性及配额核对
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    -- 实际内容的 SHA256 摘要
    sha256 TEXT NOT NULL,
    -- 受限存储键，等于附件标识而非绝对路径
    storage_key TEXT NOT NULL,
    -- 状态 writing、ready、expired 或 failed
    status TEXT COLLATE NOCASE NOT NULL,
    -- 创建时间，统一为 UTC
    created_at DATETIME NOT NULL,
    -- 到期时间，读取租约释放后方可清理文件
    expires_at DATETIME NOT NULL,
    PRIMARY KEY (id)
);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_media_asset_execution ON agent_bot_media_asset (execution_kind,execution_id);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_media_asset_expiry ON agent_bot_media_asset (status,expires_at,id);

-- 一次性任务所有者配额锁
CREATE TABLE IF NOT EXISTS agent_bot_job_owner (
    -- 机器人、会话、发起人的稳定摘要，作为创建配额行锁
    owner_key TEXT NOT NULL,
    PRIMARY KEY (owner_key)
);

-- Python 一次性文本任务及投递回执
CREATE TABLE IF NOT EXISTS agent_bot_job (
    -- 服务端随机任务标识
    id TEXT NOT NULL,
    -- 可信所有者摘要
    owner_key TEXT NOT NULL,
    -- 所有者与请求组成的幂等摘要，每条请求最多一个任务
    deduplication_key TEXT NOT NULL,
    -- 原机器人 QQ 号
    bot_id TEXT NOT NULL,
    -- 原会话标识
    session_id TEXT NOT NULL,
    -- 发起人 QQ 号
    sender_id TEXT NOT NULL,
    -- 原投递场景 group 或 private
    scene TEXT COLLATE NOCASE NOT NULL,
    -- 群聊目标，私聊为空
    group_id TEXT NULL,
    -- 创建时的有效模型别名，到期仍须在目录启用
    model TEXT COLLATE NOCASE NOT NULL,
    -- 创建时的系统提示快照，不含凭据
    system_prompt TEXT COLLATE NOCASE NOT NULL,
    -- 一次性任务正文，最多八千字符
    prompt TEXT COLLATE NOCASE NOT NULL,
    -- 状态 PENDING、RUNNING、SUCCEEDED、FAILED、CANCELLED
    status TEXT COLLATE NOCASE NOT NULL,
    -- 成功模型正文，最多六万四千字符
    result TEXT COLLATE NOCASE NULL,
    -- 稳定脱敏错误码，不保存异常正文
    error_code TEXT COLLATE NOCASE NULL,
    -- 计划执行时间，UTC
    run_at DATETIME NOT NULL,
    -- 创建时间，UTC
    created_at DATETIME NOT NULL,
    -- 获得执行权时间，UTC
    started_at DATETIME NULL,
    -- 进入终态时间，UTC
    completed_at DATETIME NULL,
    -- 消息队列确认投递完成时间，UTC
    delivered_at DATETIME NULL,
    PRIMARY KEY (id),
    -- 外键保护引用完整性，保留级联删除规则。
    CONSTRAINT fk_job_owner FOREIGN KEY (owner_key) REFERENCES agent_bot_job_owner(owner_key)
);
-- 唯一索引保护业务标识。
CREATE UNIQUE INDEX IF NOT EXISTS uk_job_request ON agent_bot_job (deduplication_key);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_job_owner_status ON agent_bot_job (owner_key,status);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_job_owner_created ON agent_bot_job (owner_key,created_at,id);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_job_due ON agent_bot_job (status,run_at,id);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_job_started ON agent_bot_job (status,started_at);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_job_delivery ON agent_bot_job (bot_id,status,delivered_at,id);

-- Python 自动发言固定选择决策记录
CREATE TABLE IF NOT EXISTS agent_bot_auto_speak_decision (
    -- 服务端决策记录标识
    id TEXT NOT NULL,
    -- 自动发言会话标识
    session_id TEXT NOT NULL,
    -- 本次决策请求标识
    request_id TEXT COLLATE NOCASE NOT NULL,
    -- 已登记的决策服务代码
    mcp_code TEXT COLLATE NOCASE NOT NULL,
    -- 本次决策使用的服务配置版本
    mcp_revision INTEGER NULL CHECK (mcp_revision >= 0),
    -- completed、failed 或 cancelled
    status TEXT COLLATE NOCASE NOT NULL,
    -- 有效结果是否发言，失败为空
    should_speak INTEGER NULL,
    -- 有效结果置信度，零到一，失败为空
    confidence REAL NULL,
    -- 本次调用耗时，毫秒
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    -- 稳定脱敏错误码，不保存远端原文
    error_code TEXT COLLATE NOCASE NULL,
    -- 记录时间，UTC
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id)
);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_auto_decision_created ON agent_bot_auto_speak_decision (created_at,id);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_auto_decision_session ON agent_bot_auto_speak_decision (session_id,created_at,id);
