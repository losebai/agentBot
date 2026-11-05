-- 用途：显式初始化 agentBot 0.6.3 SQLite 的业务表与可选本地队列表。
-- Comfy 插件沿用配置及工作流表，无新增结构；新库默认不启用，旧库由 comfy-plugin-v1 流水保留原启用选择。
-- 执行顺序：由 agent-bot database init 在启用外键的事务中执行，再运行 settings init。
-- 数据影响：仅创建缺失结构，不清空数据、不写入配置或密钥。
-- 统一会话仅建 chat_session_context；既有部署校验迁移与备份后使用 2026-10-20-retire-legacy-session-context.sqlite.sql 清理旧表。
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
-- 旧关联表已合入 chat_tagged_memory，不再单独创建。

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
    update_time DATETIME NOT NULL,
    -- 内嵌授权作用域类型，不再依赖作用域关联表。
    scope TEXT NOT NULL,
    -- 规范化主题标签数组，不再依赖标签关联表。
    tags_json TEXT NOT NULL CHECK (json_valid(tags_json)),
    -- 引用素材标识数组，原件授权仍按当前会话校验。
    material_ids_json TEXT NOT NULL CHECK (json_valid(material_ids_json)),
    -- 事实乐观锁版本
    revision INT NOT NULL DEFAULT 1,
    -- 用于整理的规范主题
    topic VARCHAR(80) NOT NULL DEFAULT '',
    -- 事实类型
    kind VARCHAR(20) NOT NULL DEFAULT 'fact',
    -- 生命周期状态
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    -- 被描述主体类型
    subject_type VARCHAR(16) NOT NULL DEFAULT 'unknown',
    -- 匿名主体标识
    subject_id VARCHAR(64) NOT NULL DEFAULT '',
    -- 最近确认的来源类型
    source_kind VARCHAR(24) NOT NULL DEFAULT 'legacy_unknown',
    -- 重要性排序权重非正确概率
    importance DOUBLE NOT NULL DEFAULT 0.5,
    -- 已知事实时间未知为空
    observed_at DATETIME NULL,
    -- UTC有效区间起点含边界
    valid_from DATETIME NULL,
    -- UTC有效区间终点不含边界
    valid_until DATETIME NULL,
    -- 任务未完成完成或取消
    task_state VARCHAR(16) NULL,
    -- 合并后的当前事实标识
    merged_into_id INT NULL,
    -- 待解决冲突集合
    conflict_set_id VARCHAR(64) NULL,
    -- 规范正文摘要用于精确去重候选
    content_fingerprint CHAR(64) NOT NULL DEFAULT '',
    -- 实际采用时间不是事实时间
    last_used_at DATETIME NULL,
    -- 幂等累计采用次数
    use_count INT NOT NULL DEFAULT 0,
    -- 创建时间的UTC或旧未知时区依据
    created_time_basis VARCHAR(24) NOT NULL DEFAULT 'utc',
    -- 更新时间的UTC或旧未知时区依据
    updated_time_basis VARCHAR(24) NOT NULL DEFAULT 'utc'
);
-- 唯一索引保护业务标识。
CREATE UNIQUE INDEX IF NOT EXISTS uk_tagged_memory_scope_key ON chat_tagged_memory (scope_id, memory_key);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_tagged_memory_scope_time ON chat_tagged_memory (scope_id, is_deleted, update_time);

-- 记忆与标签多对多关联
-- 旧关联表已合入 chat_tagged_memory，不再单独创建。
-- 联合索引支持过滤及排序。

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
    -- 请求上游时使用的自定义 User-Agent，空值沿用客户端默认值
    user_agent TEXT NOT NULL DEFAULT '',
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

-- 模型目录，稳定主键定义引用身份
CREATE TABLE IF NOT EXISTS agent_bot_ai_model (
    -- 自增主键
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK (id >= 0),
    -- 历史别名仅供兼容，新模型为空
    alias TEXT NULL,
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
-- 供应商与上游模型共同定义业务身份，保留大小写。
CREATE UNIQUE INDEX IF NOT EXISTS uq_model_provider_upstream ON agent_bot_ai_model (provider_id, upstream_model);

-- 普通调用统一保存在 agent_bot_call_record，不再创建旧执行表。

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
    -- 工具调用的脱敏入参；历史空值表示未记录。
    arguments TEXT DEFAULT NULL CHECK (arguments IS NULL OR json_valid(arguments)),
    -- 脱敏返回值或未知结果错误信息；不包含媒体二进制。
    result TEXT DEFAULT NULL CHECK (result IS NULL OR json_valid(result)),
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
    -- 图片理解模型；空值沿用聊天模型，模型引用阻止被使用的模型删除。
    vision_model_id INTEGER DEFAULT NULL REFERENCES agent_bot_ai_model(id)
        CHECK (vision_model_id IS NULL OR vision_model_id >= 1),
    -- 错误转发模型；空值禁用转发，每请求最多接管一次。
    fallback_model_id INTEGER DEFAULT NULL REFERENCES agent_bot_ai_model(id)
        CHECK (fallback_model_id IS NULL OR fallback_model_id >= 1),
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
    -- 独立人格设定，空字符串表示仅使用角色设定。
    personality_prompt TEXT NOT NULL DEFAULT '',
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
-- 定时任务执行租约与审计；先创建所属任务，不修改既有历史。
CREATE TABLE IF NOT EXISTS agent_bot_job_attempt (
    -- 随机执行租约标识，拒绝旧执行器迟到提交
    id TEXT NOT NULL PRIMARY KEY,
    -- 所属持久任务标识
    job_id TEXT NOT NULL,
    -- 执行状态 RUNNING、SUCCEEDED、FAILED、INTERRUPTED、CANCELLED
    status TEXT NOT NULL,
    -- 领取时间，UTC
    started_at DATETIME NOT NULL,
    -- 执行权过期时间，UTC；晚于模型硬超时
    lease_expires_at DATETIME NOT NULL,
    -- 执行结束或确认中断时间，UTC
    completed_at DATETIME NULL,
    -- 稳定脱敏错误码，不记录凭据或供应商异常正文
    error_code TEXT NULL,
    -- 审计依附原任务，禁止删除仍有执行历史的任务
    CONSTRAINT fk_job_attempt_job FOREIGN KEY (job_id) REFERENCES agent_bot_job(id)
);
-- 任务索引支持恢复次数统计与审计查询。
CREATE INDEX IF NOT EXISTS idx_job_attempt_history
    ON agent_bot_job_attempt (job_id,started_at,id);

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

-- SQLite 统一调用记录增量；先执行本脚本，再运行 records migrate。
-- 只新增表和索引，保留全部旧记录；JSON 使用文本保存并检查合法性。
CREATE TABLE IF NOT EXISTS agent_bot_call_record (
  id INTEGER PRIMARY KEY AUTOINCREMENT, -- 统一记录主键
  session_Id TEXT COLLATE BINARY, -- 会话标识
  content TEXT, -- 用户输入正文
  replay_context TEXT, -- 模型回复正文
  create_time TEXT, -- 调用记录时间，保留源时间
  created_at_utc TEXT, -- 普通执行原始 UTC 时间，保留微秒；其他历史来源可空
  duration INTEGER, -- 调用耗时，单位 ms
  user_certificate TEXT, -- 用户凭据摘要，不保存原始凭据
  model TEXT, -- 实际调用模型
  data TEXT CHECK (data IS NULL OR json_valid(data)), -- 响应数据、用量及扩展
  request_context TEXT CHECK (request_context IS NULL OR json_valid(request_context)), -- 无临时凭据的上下文
  request_id TEXT COLLATE BINARY, -- 请求关联标识
  type TEXT NOT NULL, -- ordinary、agent、auto_speak、unknown
  source TEXT NOT NULL COLLATE BINARY, -- 来源系统及表命名空间
  source_id TEXT NOT NULL COLLATE BINARY, -- 来源原始主键
  execution_id TEXT, -- 本地执行标识，用于关联步骤与附件
  status TEXT NOT NULL, -- 执行状态，历史缺失时为 unknown
  -- 系统分类：auto_speak、command；NULL 为普通对话，兼容历史标记，由索引维护分类值。
  system_kind TEXT COLLATE BINARY GENERATED ALWAYS AS (
  CASE WHEN (
    type = 'auto_speak'
    OR source = 'python:agent_bot_auto_speak_decision'
    OR SUBSTR(request_id, 1, 14) = 'auto-decision-'
    OR JSON_EXTRACT(data, '$.type') IN ('auto_speak_decision', '"auto_speak_decision"')
    OR JSON_EXTRACT(data, '$.system_kind') IN ('auto_speak', '"auto_speak"')
    OR (JSON_EXTRACT(request_context, '$.state') IS NOT NULL
        AND JSON_EXTRACT(data, '$.shouldSpeak') IS NOT NULL)
  ) THEN 'auto_speak' WHEN (
    COALESCE(JSON_EXTRACT(data, '$.system_kind') IN ('command', '"command"'), 0)
    OR JSON_EXTRACT(data, '$.original_data.system_kind') IN ('command', '"command"')
    OR SUBSTR(request_id, 1, 11) = 'qq-summary-'
    OR SUBSTR(request_id, 1, 8) = 'qq-draw-'
    OR SUBSTR(session_Id, -15) = ':quoted-summary'
    OR (SUBSTR(request_id, 1, 9) = 'qq-reply-' AND SUBSTR(content, 1, 1) = '/')
  ) THEN 'command' ELSE NULL END
  ) VIRTUAL
);
-- 来源内唯一，重复导入不得覆盖新记录。
CREATE UNIQUE INDEX IF NOT EXISTS uk_call_source ON agent_bot_call_record(source, source_id);
-- 类型、会话、状态分别支持自增主键分页。
CREATE INDEX IF NOT EXISTS idx_call_type ON agent_bot_call_record(type, create_time, id);
CREATE INDEX IF NOT EXISTS idx_call_session ON agent_bot_call_record(session_Id, create_time, id);
CREATE INDEX IF NOT EXISTS idx_call_status ON agent_bot_call_record(status, create_time, id);

-- 按调用时间及主键稳定分页。
CREATE INDEX IF NOT EXISTS idx_call_time ON agent_bot_call_record(create_time, id);
-- 分类前缀用于精确计数，时间和主键用于分类后的稳定分页，避免解析归档 JSON。
CREATE INDEX IF NOT EXISTS idx_call_system_time ON agent_bot_call_record(system_kind, create_time, id);


-- 可选本地队列结构同步；运行时默认使用独立 data/queue.sqlite3。
-- 本地持久队列初始化：选用 SQLite 队列后，在处理任务之前执行。
-- 可重复执行，仅创建缺失的队列表和索引；不修改业务配置，不清空既有任务。
-- 与主配置数据库独立；图片元数据与入队记录共用事务，保证取消与恢复一致。

-- 待处理任务与重试、投递检查点；命名空间隔离文本和图片队列。
CREATE TABLE IF NOT EXISTS chat_queue_jobs (
    stream_id INTEGER PRIMARY KEY AUTOINCREMENT, -- 单调递增的持久任务顺序，删除后不复用
    namespace TEXT NOT NULL, -- 队列键前缀
    payload TEXT NOT NULL, -- 已验证的任务 JSON，不保存运行凭据
    claimed_at REAL, -- 最近领取时间；空值表示尚未领取
    state TEXT NOT NULL, -- 重试次数、可执行时间及投递位置 JSON
    result TEXT, -- 生成结果 JSON，恢复时复用以避免重复请求模型
    result_expires_at REAL -- 生成结果保留截止时间，空值表示无结果
);
-- 按队列顺序领取及恢复，不扫描其他队列的数据。
CREATE INDEX IF NOT EXISTS chat_queue_jobs_namespace
    ON chat_queue_jobs(namespace, stream_id);

-- 稳定任务标识的入队回执，活动任务持续保留，结束后按保留期过期。
CREATE TABLE IF NOT EXISTS chat_queue_receipts (
    namespace TEXT NOT NULL, -- 所属队列键前缀
    job_id TEXT NOT NULL, -- 应用层稳定任务标识
    digest TEXT NOT NULL, -- 参数摘要，禁止用相同标识替换任务内容
    stream_id INTEGER NOT NULL, -- 原始队列顺序；任务完成后仍用于重复入队响应
    expires_at REAL, -- 回执保留截止时间；空值表示活动任务不可过期
    PRIMARY KEY (namespace, job_id)
);
-- 有界清理已结束的过期回执。
CREATE INDEX IF NOT EXISTS chat_queue_receipts_expiry
    ON chat_queue_receipts(namespace, expires_at);

-- 最终失败或解析损坏的条目，按队列容量保留最近记录。
CREATE TABLE IF NOT EXISTS chat_queue_deadletters (
    id INTEGER PRIMARY KEY AUTOINCREMENT, -- 死信写入顺序
    namespace TEXT NOT NULL, -- 所属队列键前缀
    source_id INTEGER NOT NULL, -- 原始队列顺序
    payload TEXT, -- 合法失败任务的 JSON；损坏任务不复制不可信内容
    reason TEXT NOT NULL, -- 有界错误类型或原因，不保存敏感异常内容
    attempt INTEGER NOT NULL, -- 失败时的执行次数
    failed_at REAL NOT NULL -- 进入死信的时间
);
-- 支持按命名空间裁剪最旧死信。
CREATE INDEX IF NOT EXISTS chat_queue_deadletters_namespace
    ON chat_queue_deadletters(namespace, id);

-- 图片执行元数据；取消意图独立持久化，活动记录不自动过期。
CREATE TABLE IF NOT EXISTS chat_image_tasks (
    namespace TEXT NOT NULL, -- 图片队列键前缀
    task_id TEXT NOT NULL, -- 图片任务稳定标识
    owner TEXT NOT NULL, -- 机器人、会话和用户归属的规范 JSON
    created_at REAL NOT NULL, -- 创建时间，用于最近任务排序
    payload TEXT NOT NULL, -- 完整执行、投递、取消和上游标识 JSON
    expires_at REAL, -- 终态保留截止时间，活动任务为空
    PRIMARY KEY (namespace, task_id)
);
-- 精确按归属列出最近任务，防止跨用户泄露。
CREATE INDEX IF NOT EXISTS chat_image_tasks_owner
    ON chat_image_tasks(namespace, owner, created_at DESC);
-- 清理过期终态，避免本地任务元数据无限增长。
CREATE INDEX IF NOT EXISTS chat_image_tasks_expiry
    ON chat_image_tasks(namespace, expires_at);

-- 工具操作幂等记录，保留已提交副作用及其结果，防止任务重试重复提交。
CREATE TABLE IF NOT EXISTS chat_tool_operations (
    namespace TEXT NOT NULL, -- 服务键前缀
    job_id TEXT NOT NULL, -- 所属对话任务标识
    tool TEXT NOT NULL, -- 工具名称
    payload TEXT NOT NULL, -- 操作参数摘要与状态 JSON
    expires_at REAL NOT NULL, -- 操作记录保留截止时间
    PRIMARY KEY (namespace, job_id, tool)
);
-- 按命名空间清理已过期的幂等记录。
CREATE INDEX IF NOT EXISTS chat_tool_operations_expiry
    ON chat_tool_operations(namespace, expires_at);

-- 标签知识库增量：既有 SQLite 部署显式执行，可重复执行。
-- 在当前业务结构之后执行，仅新增知识正文和标签表，不改写记忆、配置或密钥。
-- 两表关联由应用事务维护，逻辑删除保留正文和标签，不添加外键。
CREATE TABLE IF NOT EXISTS chat_knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT, -- 只新增知识条目的标识
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200), -- 不可编辑的知识标题
    content TEXT NOT NULL CHECK(length(content) BETWEEN 1 AND 16000), -- 不可编辑的知识正文
    is_deleted INTEGER NOT NULL DEFAULT 0 CHECK(is_deleted IN (0, 1)), -- 删除保留历史正文
    create_time TEXT NOT NULL -- 知识创建时间
);
-- 按有效状态与标识进行稳定分页。
CREATE INDEX IF NOT EXISTS idx_knowledge_active_id ON chat_knowledge(is_deleted, id);

CREATE TABLE IF NOT EXISTS chat_knowledge_tag (
    knowledge_id INTEGER NOT NULL, -- 由应用事务维护的知识关联
    tag TEXT NOT NULL CHECK(length(tag) BETWEEN 1 AND 32), -- 规范化主题标签
    PRIMARY KEY (knowledge_id, tag) -- 同条目同主题只关联一次
);
-- 按标签筛选知识并统计有效条目。
CREATE INDEX IF NOT EXISTS idx_knowledge_tag_lookup ON chat_knowledge_tag(tag, knowledge_id);


-- 用途：新增持久图片/文件素材、接收来源和长期记忆素材关联。
-- 顺序：已有 SQLite 数据库备份后显式执行；依赖 chat_tagged_memory 已存在。
-- 数据影响：只新增表和索引，不重建旧表，不迁移或删除临时附件和历史记忆。
CREATE TABLE IF NOT EXISTS chat_material (
    -- 服务端生成的持久素材标识，消息和记忆共享稳定 ID。
    id TEXT PRIMARY KEY,
    -- 可信入口 qq 或 admin。
    entry TEXT NOT NULL,
    -- 匿名会话归属，查询及原件读取均校验。
    session_id TEXT NOT NULL,
    -- 原始文件显示名称，不参与构造磁盘路径。
    name TEXT NOT NULL,
    -- 素材类别 image 或 file。
    kind TEXT NOT NULL,
    -- 已验证的文件媒体类型。
    mime_type TEXT NOT NULL,
    -- 实际原件字节数。
    size_bytes INTEGER NOT NULL,
    -- 原件内容摘要，会话内复用相同文件。
    sha256 TEXT NOT NULL,
    -- 受限存储键，等于素材 ID。
    storage_key TEXT NOT NULL,
    -- 已有文字说明，不自动调用模型生成。
    description TEXT NOT NULL DEFAULT '',
    -- 创建时间，统一使用 UTC。
    created_at DATETIME NOT NULL
);
-- 去重限制在相同入口和会话，不能跨会话探测私有文件。
CREATE UNIQUE INDEX IF NOT EXISTS uk_material_owner_digest
ON chat_material(entry,session_id,sha256);
-- 支持会话内按类别和创建时间分页。
CREATE INDEX IF NOT EXISTS idx_material_owner_time
ON chat_material(entry,session_id,kind,created_at,id);

CREATE TABLE IF NOT EXISTS chat_material_source (
    -- 接收来源的可信入口。
    entry TEXT NOT NULL,
    -- 来源所属匿名会话。
    session_id TEXT NOT NULL,
    -- 机器人及原消息附件身份的 SHA256。
    source_key TEXT NOT NULL,
    -- 关联的持久素材 ID，外键保护来源不会悬空。
    material_id TEXT NOT NULL REFERENCES chat_material(id),
    -- 同一来源不可绑定到不同素材，引用直接复用此关联。
    PRIMARY KEY(entry,session_id,source_key)
);
-- 支持按素材检查来源。
CREATE INDEX IF NOT EXISTS idx_material_source_material ON chat_material_source(material_id);

-- 用途：为持久素材新增多对多来源类型和规范化标签，供管理与会话工具精确筛选。
-- 顺序：适用于已具备 chat_material 的 0.5.5 至 0.5.9 部署，由 material-classification-v1 登记后执行。
-- 数据影响：仅新增关联表和索引，不改写旧素材，不猜测或回填历史分类和标签。
-- 持久素材与实际来源类型关联。
CREATE TABLE IF NOT EXISTS chat_material_category (
    -- 所属持久素材 ID，外键保证来源类型归属于真实素材。
    material_id TEXT NOT NULL REFERENCES chat_material(id),
    -- 来源类型 received、sent、generated 或 uploaded。
    category TEXT NOT NULL,
    -- 同一素材保留多个来源，复用原件不会覆盖先前分类。
    PRIMARY KEY (material_id,category)
);
-- 类型在前支持精确类型筛选，素材在后供 EXISTS 关联和覆盖读取。
CREATE INDEX IF NOT EXISTS idx_material_category_lookup
ON chat_material_category(category,material_id);

-- 持久素材与可编辑主题标签关联。
CREATE TABLE IF NOT EXISTS chat_material_tag (
    -- 所属持久素材 ID，外键保证标签不能引用不存在的素材。
    material_id TEXT NOT NULL REFERENCES chat_material(id),
    -- 经 NFKC、去首尾空白和大小写折叠的主题标签。
    tag TEXT NOT NULL,
    -- 同一素材标签唯一，应用层限制完整标签组最多八个。
    PRIMARY KEY (material_id,tag)
);
-- 标签在前支持精确筛选，避免多标签 JOIN 导致分页和计数重复。
CREATE INDEX IF NOT EXISTS idx_material_tag_lookup ON chat_material_tag(tag,material_id);

-- 旧关联表已合入 chat_tagged_memory，不再单独创建。
-- 支持按素材反查其记忆引用。

-- 用途：统一保存当前会话接收和发送上下文，每个 session 仅一行，不设置过期时间。
-- 顺序：已有 SQLite 部署先停止服务并备份，再执行本脚本及显式历史迁移工具。
-- 数据影响：仅新增表，不删除旧会话、轮次、出站记录，不覆盖已有统一上下文。
CREATE TABLE IF NOT EXISTS chat_session_context (
    -- 会话唯一标识，并发合并由短写事务串行提交。
    session_id TEXT NOT NULL PRIMARY KEY,
    -- 仅显式清空时切换，隔离旧后台任务。
    generation_id TEXT NOT NULL,
    -- 按消息时间及稳定序号排序的双向上下文。
    messages_json TEXT NOT NULL,
    -- 超出近期窗口的历史摘要。
    memory_summary TEXT NOT NULL,
    -- 带真实主体、来源事件和记忆引用的结构化摘要。
    memory_facts_json TEXT NULL CHECK (memory_facts_json IS NULL OR json_valid(memory_facts_json)),
    -- 旧纯文本摘要归档，不默认注入模型。
    legacy_memory_summary TEXT NULL,
    -- 摘要快照序号，不拒绝普通消息并发更新。
    memory_revision INTEGER NOT NULL DEFAULT 0,
    -- 请求去重、发送占位及回执，不再依赖出站正文表。
    deliveries_json TEXT NOT NULL,
    -- UTC 创建时间。
    create_time DATETIME NOT NULL,
    -- UTC 最近合并时间，不用于自动过期。
    update_time DATETIME NOT NULL
);

-- 普通接口按来源、原始 UTC 微秒和执行标识稳定分页。
CREATE INDEX IF NOT EXISTS idx_call_source_created ON agent_bot_call_record(source, created_at_utc, execution_id);

-- 支持授权状态、主体去重与主题整理查询。
CREATE INDEX IF NOT EXISTS idx_memory_status ON chat_tagged_memory (scope_id,status);
-- 支持授权状态、主体去重与主题整理查询。
CREATE INDEX IF NOT EXISTS idx_memory_fingerprint ON chat_tagged_memory (scope_id,subject_id,content_fingerprint);
-- 支持授权状态、主体去重与主题整理查询。
CREATE INDEX IF NOT EXISTS idx_memory_topic ON chat_tagged_memory (scope_id,subject_id,topic);
-- 支持授权状态、主体去重与主题整理查询。
CREATE INDEX IF NOT EXISTS idx_memory_merged ON chat_tagged_memory (merged_into_id);

-- 记忆生命周期：当前事实的历史、来源、幂等和向量缓存；新库直接创建。
-- 历史版本标识
-- 当前事实标识
-- 不可变事实版本
-- 变更操作
-- 完整事实与来源快照
-- 管理员用户或迁移来源
-- 变更理由不含内部推理
-- UTC版本提交时间
-- 记忆生命周期辅助表：各字段、唯一约束和外键见下方中文说明。
CREATE TABLE IF NOT EXISTS chat_memory_revision (
      -- 历史版本标识
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      -- 当前事实标识
      memory_id INT NOT NULL,
      -- 不可变事实版本
      revision INT NOT NULL,
      -- 变更操作
      operation VARCHAR(20) NOT NULL,
      -- 完整事实与来源快照
      snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
      -- 管理员用户或迁移来源
      actor_type VARCHAR(20) NOT NULL,
      -- 变更理由不含内部推理
      reason VARCHAR(300) NOT NULL,
      -- UTC版本提交时间
      created_at DATETIME NOT NULL,
      -- 主键稳定标识历史记录，联合唯一约束禁止覆盖同一事实版本。
      -- 唯一约束防止同版本或来源重复。
      UNIQUE (memory_id,revision),
      -- 外键防止历史版本脱离当前事实根记录。
      -- 外键保证辅助记录归属于已有事实。
      FOREIGN KEY (memory_id) REFERENCES chat_tagged_memory(id)
    );

CREATE TABLE IF NOT EXISTS chat_memory_evidence (
      -- 来源证据标识
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      -- 归属事实
      memory_id INT NOT NULL,
      -- 首次纳入版本
      first_revision INT NOT NULL,
      -- 来源类别不由模型指定
      source_kind VARCHAR(24) NOT NULL,
      -- 可核对来源引用
      source_ref VARCHAR(200) NULL,
      -- 匿名发言人
      source_actor_id VARCHAR(64) NULL,
      -- 来源会话权限边界
      source_session_id VARCHAR(128) NULL,
      -- 真实来源事件引用
      source_event_id VARCHAR(200) NULL,
      -- 来源UTC时间未知为空
      source_time DATETIME NULL,
      -- 证据记录UTC时间
      recorded_at DATETIME NOT NULL,
      -- 真实片段不是可执行指令
      excerpt VARCHAR(300) NOT NULL,
      -- 独立来源去重摘要
      evidence_fingerprint CHAR(64) NOT NULL,
      -- 合并前原始事实
      original_memory_id INT NULL,
      -- 主键稳定标识证据，联合唯一约束防止同一来源反复佐证。
      -- 唯一约束防止同版本或来源重复。
      UNIQUE (memory_id,evidence_fingerprint),
      -- 来源撤回按事件和会话定向定位，避免锁定无关事实证据。
      -- 外键防止来源证据脱离当前事实根记录。
      -- 外键保证辅助记录归属于已有事实。
      FOREIGN KEY (memory_id) REFERENCES chat_tagged_memory(id)
    );

CREATE TABLE IF NOT EXISTS chat_memory_operation (
      -- 写入权限作用域
      scope_id CHAR(64) NOT NULL,
      -- 原始请求标识
      request_id VARCHAR(128) NOT NULL,
      -- 幂等操作标识
      operation_id VARCHAR(128) NOT NULL,
      -- 参数摘要防止复用身份篡改
      arguments_digest CHAR(64) NOT NULL,
      -- 写入或采用操作
      operation_kind VARCHAR(20) NOT NULL,
      -- 仅状态和标识不复制正文
      result_json TEXT NOT NULL CHECK (json_valid(result_json)),
      -- 提交UTC时间
      created_at DATETIME NOT NULL,
      -- 复合主键在授权作用域内隔离请求与操作，防止重放写入。
      -- 复合主键隔离不同事实版本和请求。
      PRIMARY KEY (scope_id,request_id,operation_id)
    );

CREATE TABLE IF NOT EXISTS chat_memory_embedding (
      -- 事实标识
      memory_id INT NOT NULL,
      -- 向量对应的事实版本
      revision INT NOT NULL,
      -- 向量模型及配置版本
      model_key VARCHAR(200) NOT NULL,
      -- 有限数值向量数组
      vector_json TEXT NOT NULL CHECK (json_valid(vector_json)),
      -- 计算UTC时间
      created_at DATETIME NOT NULL,
      -- 复合主键禁止跨事实版本或模型版本复用向量。
      -- 复合主键隔离不同事实版本和请求。
      PRIMARY KEY (memory_id,revision,model_key),
      -- 外键保证向量仅附着于已有事实，遗忘仍由状态和版本筛选。
      -- 外键保证辅助记录归属于已有事实。
      FOREIGN KEY (memory_id) REFERENCES chat_tagged_memory(id)
    );

-- 来源撤回按事件和会话定向定位，避免扫描并锁定无关证据。
CREATE INDEX IF NOT EXISTS idx_memory_evidence_event ON chat_memory_evidence (source_event_id,source_session_id);

-- B 站单账号与外部操作记录；0.5.7 既有 SQLite 库由管理员显式执行一次。
-- 在完整初始化之后执行；只新增表和索引，不修改或删除业务数据，不由普通启动执行。
CREATE TABLE IF NOT EXISTS agent_bot_bilibili_account (
    id INTEGER PRIMARY KEY CHECK (id = 1), -- 固定单账号，避免模型选择其他身份
    uid TEXT NOT NULL, -- B 站账号 UID
    name TEXT NOT NULL, -- 最近验证的账号昵称
    cookies TEXT NOT NULL, -- 仅服务端可读的登录凭据，不回填 API
    updated_at INTEGER NOT NULL -- 最近确认登录的 UTC 秒
);
CREATE TABLE IF NOT EXISTS agent_bot_bilibili_operation (
    operation_key TEXT PRIMARY KEY, -- 账号、归属、请求和参数的 SHA256 唯一占位
    uid TEXT NOT NULL, -- 操作时绑定的账号，不随清除登录删除记录
    tool TEXT NOT NULL, -- 产生外部副作用的工具名
    status TEXT NOT NULL CHECK (status IN ('pending', 'completed')), -- 未知结果禁止重发
    result TEXT NULL, -- 成功后的安全 JSON 结果，不含凭据
    created_at INTEGER NOT NULL -- 操作占位的 UTC 秒
);

-- 启动版本管理增量：用于已有 0.5.5–0.5.7 完整结构，先执行本脚本再运行已登记业务增量。
-- 仅新增版本与迁移流水表，不写入版本号、不修改业务数据；运行器成功核验后登记版本。
CREATE TABLE IF NOT EXISTS agent_bot_schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1), -- 单例版本记录，防止并行版本分叉
    version TEXT NOT NULL, -- 完成核验的程序稳定版本号
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP -- 版本推进时间
);
CREATE TABLE IF NOT EXISTS agent_bot_schema_migration (
    migration_id TEXT PRIMARY KEY, -- 发布清单中的唯一迁移 ID，主键避免重复登记
    target_version TEXT NOT NULL, -- 该迁移所属程序版本
    checksum TEXT NOT NULL, -- 当前后端 SQL 的 SHA256，禁止静默篡改历史迁移
    status TEXT NOT NULL CHECK (status IN ('applying', 'applied', 'failed')), -- 中断可审计
    applied_at DATETIME NULL, -- 迁移完成时间，未完成时为空
    last_error TEXT NULL -- 仅保存固定错误码，不记录业务值或原始数据库异常
);

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

-- 长期互动演化增量：完整旧业务库先完成已登记历史迁移，再由启动升级器执行本脚本。
-- 仅新增演化配置、匿名画像和因果事件，不改写原角色、凭据、记忆或历史数据。
-- 新库完整初始化含同样结构；表和索引允许中断后重入，不扫描历史 SQL。

-- 每个 Agent 的独立启停配置；无配置行时应用默认启用，空画像不改变旧行为。
CREATE TABLE IF NOT EXISTS agent_bot_evolution_config (
    agent_id BIGINT PRIMARY KEY, -- 真实 Agent；随删除级联清理
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)), -- 暂停同时停止学习和应用
    revision BIGINT NOT NULL DEFAULT 1, -- 管理配置的乐观锁版本
    FOREIGN KEY (agent_id) REFERENCES agent_bot_agent(id) ON DELETE CASCADE
);

-- 画像按 Agent、匿名用户隔离，不能以一个群友的意见改变其他人的互动习惯。
CREATE TABLE IF NOT EXISTS agent_bot_evolution_profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT, -- 稳定分页及审计标识
    agent_id BIGINT NOT NULL, -- 所属 Agent
    subject_key CHAR(64) NOT NULL, -- 会话密钥命名空间中的匿名用户摘要
    traits TEXT NOT NULL, -- 四个固定表达维度，值域为负三至三
    revision BIGINT NOT NULL DEFAULT 1, -- 行为变化和重置递增
    UNIQUE (agent_id,subject_key), -- 同一 Agent 的同一主体只有一个画像
    FOREIGN KEY (agent_id) REFERENCES agent_bot_agent(id) ON DELETE CASCADE
);

-- 原始反馈、实际变化和重置共享有序事件流，变化保留全部参与反馈的主键。
CREATE TABLE IF NOT EXISTS agent_bot_evolution_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT, -- 因果引用与分页主键
    profile_id BIGINT NOT NULL, -- 画像归属
    source_key VARCHAR(64) NOT NULL, -- 服务端真实事件匿名摘要，内部操作使用随机标识
    kind VARCHAR(16) NOT NULL CHECK (kind IN ('feedback','change','reset')), -- 事件类型
    dimension VARCHAR(16) NOT NULL, -- 固定维度；重置使用空串
    direction INTEGER NOT NULL CHECK (direction BETWEEN -1 AND 1), -- 明确反馈的方向
    excerpt VARCHAR(300) NOT NULL, -- 完整明确反馈句或固定变化原因
    observed_at DATETIME NOT NULL, -- UTC 来源时间，不由模型填写
    consumed INTEGER NOT NULL DEFAULT 0 CHECK (consumed IN (0,1)), -- 防止证据重复驱动变化
    before_value INTEGER NULL, -- 变化前档位，仅变化事件填写
    after_value INTEGER NULL, -- 变化后档位，仅变化事件填写
    evidence_ids TEXT NOT NULL, -- 变化所依据的真实反馈主键数组
    UNIQUE (profile_id,source_key,dimension,kind), -- 同一来源重放不重复累计
    FOREIGN KEY (profile_id) REFERENCES agent_bot_evolution_profile(id) ON DELETE CASCADE
);
-- 只读取当前画像、维度的未消费证据，并按来源时间选择九十天窗口。
CREATE INDEX IF NOT EXISTS idx_evolution_pending
    ON agent_bot_evolution_event(profile_id,dimension,kind,consumed,observed_at);

-- 可选角色关联的完整新库结构；旧三表保留兼容基线，应用使用以下范围、状态和记录表。
-- 通用范围由首次启动登记的独立增量创建，既有数据复制也仅在独立增量中执行。
CREATE TABLE IF NOT EXISTS agent_bot_evolution_scope (
    scope_key VARCHAR(80) PRIMARY KEY, -- global 为通用范围，agent:ID 为历史或可选角色范围
    agent_id BIGINT NULL UNIQUE, -- 可选角色关联；删除角色只解除关联
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)), -- 本范围的学习和应用开关
    revision BIGINT NOT NULL DEFAULT 0, -- 配置乐观锁；零代表尚未显式保存
    FOREIGN KEY (agent_id) REFERENCES agent_bot_agent(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS agent_bot_evolution_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT, -- 保留旧画像 ID，保证历史链接有效
    scope_key VARCHAR(80) NOT NULL, -- 范围与用户共同隔离状态
    subject_key CHAR(64) NOT NULL, -- 真实用户的匿名命名空间
    traits TEXT NOT NULL, -- 四个固定表达维度的有界档位
    revision BIGINT NOT NULL DEFAULT 1, -- 行为变化和重置版本
    UNIQUE (scope_key,subject_key), -- 同一范围内用户只有一个状态
    FOREIGN KEY (scope_key) REFERENCES agent_bot_evolution_scope(scope_key)
);
CREATE TABLE IF NOT EXISTS agent_bot_evolution_record (
    id INTEGER PRIMARY KEY AUTOINCREMENT, -- 保留旧事件主键和因果引用
    profile_id BIGINT NOT NULL, -- 所属演化状态
    source_key VARCHAR(64) NOT NULL, -- 服务端真实来源匿名摘要
    kind VARCHAR(16) NOT NULL CHECK (kind IN ('feedback','change','reset')), -- 反馈、变化或重置
    dimension VARCHAR(16) NOT NULL, -- 固定维度，重置为空
    direction INTEGER NOT NULL CHECK (direction BETWEEN -1 AND 1), -- 有界方向
    excerpt VARCHAR(300) NOT NULL, -- 原始反馈或固定原因
    observed_at DATETIME NOT NULL, -- 真实 UTC 来源时间或变化时间
    consumed INTEGER NOT NULL DEFAULT 0 CHECK (consumed IN (0,1)), -- 防止旧证据重复生效
    before_value INTEGER NULL, -- 变化前档位
    after_value INTEGER NULL, -- 变化后档位
    evidence_ids TEXT NOT NULL, -- 真实反馈主键数组，迁移不重编号
    UNIQUE (profile_id,source_key,dimension,kind), -- 原始来源重放去重
    FOREIGN KEY (profile_id) REFERENCES agent_bot_evolution_state(id)
);
-- 按状态和维度选取未消费的九十天证据。
CREATE INDEX IF NOT EXISTS idx_evolution_record_pending
    ON agent_bot_evolution_record(profile_id,dimension,kind,consumed,observed_at);
-- 按事件类型和主键支持管理记录列表倒序分页。
CREATE INDEX IF NOT EXISTS idx_evolution_record_kind ON agent_bot_evolution_record(kind,id);

-- 用途：为完整 0.6.2 业务库增加持续身份、任务、情节及审批审计；由显式升级清单执行。
-- 顺序：现有任务恢复及调用分类迁移之后；数据影响仅新增表，不启用自主循环、不改身份或密钥。
CREATE TABLE IF NOT EXISTS agent_bot_autonomy_profile (
  -- 稳定身份主键，修改设定不改变此标识
  id CHAR(32) NOT NULL PRIMARY KEY,
  -- 会话密钥派生命名空间，换密钥保留旧记录但不重新认领
  namespace CHAR(64) NOT NULL,
  -- 同一命名空间的可信机器人、会话、用户摘要
  owner_key CHAR(64) NOT NULL,
  -- 管理设定乐观锁版本，不因工作线程更新进度而推进
  revision INTEGER NOT NULL,
  -- 受信绑定、设定、宿主策略、自我状态及预算计数
  document_json TEXT NOT NULL CHECK(json_valid(document_json)),
  -- 下次事件发现与复盘时间，UTC
  next_wake_at DATETIME NOT NULL,
  -- 创建UTC时间
  created_at DATETIME NOT NULL,
  -- 最近状态提交UTC时间
  updated_at DATETIME NOT NULL,
  -- 唯一约束避免重启后创建重复身份
  UNIQUE(namespace,owner_key)
);
-- 索引仅检索当前密钥空间的到期唤醒
CREATE INDEX IF NOT EXISTS idx_autonomy_wake ON agent_bot_autonomy_profile(namespace,next_wake_at,id);
CREATE TABLE IF NOT EXISTS agent_bot_autonomy_task (
  -- 持久任务主键，同时作为汇报幂等标识
  id CHAR(32) NOT NULL PRIMARY KEY,
  -- 所属固定身份，外键阻止身份被随意删除
  profile_id CHAR(32) NOT NULL REFERENCES agent_bot_autonomy_profile(id),
  -- 创建事件或请求的幂等键
  request_id VARCHAR(128) NOT NULL,
  -- 审批和计划变更版本
  revision INTEGER NOT NULL,
  -- pending/running/retry/blocked/approval/succeeded/failed/cancelled
  status VARCHAR(16) NOT NULL,
  -- 目标、计划、已完成步骤、原始身份版本和错误
  document_json TEXT NOT NULL CHECK(json_valid(document_json)),
  -- 当前步骤已领取次数，完成步骤后清零
  attempts INTEGER NOT NULL,
  -- 拒绝旧工作线程迟到提交的随机租约令牌
  lease_token CHAR(32) NULL,
  -- 执行租约结束UTC时间，网络超时必须小于该值
  lease_expires_at DATETIME NULL,
  -- 重试或定时执行UTC时间
  next_run_at DATETIME NOT NULL,
  -- 创建UTC时间
  created_at DATETIME NOT NULL,
  -- 最近提交UTC时间
  updated_at DATETIME NOT NULL,
  -- 同一身份的创建事件仅产生一个任务
  UNIQUE(profile_id,request_id)
);
-- 调度索引支持到期任务及中断租约恢复
CREATE INDEX IF NOT EXISTS idx_autonomy_task_due ON agent_bot_autonomy_task(profile_id,status,next_run_at);
CREATE TABLE IF NOT EXISTS agent_bot_autonomy_episode (
  -- 情节主键，由身份和来源事件确定
  id CHAR(32) NOT NULL PRIMARY KEY,
  -- 可信身份绑定
  profile_id CHAR(32) NOT NULL REFERENCES agent_bot_autonomy_profile(id),
  -- 去重后的来源事件标识
  event_id VARCHAR(128) NOT NULL,
  -- 原始来源、发生时间、摘要和任务意图
  document_json TEXT NOT NULL CHECK(json_valid(document_json)),
  -- 有限保留原情节的到期UTC时间，不影响已确认长期事实
  expires_at DATETIME NOT NULL,
  -- 创建UTC时间
  created_at DATETIME NOT NULL,
  -- 同一来源不会重复归档或发现任务
  UNIQUE(profile_id,event_id)
);
-- 过期索引支持有界情节遗忘，不扫描长期事实正文
CREATE INDEX IF NOT EXISTS idx_autonomy_episode_expiry ON agent_bot_autonomy_episode(profile_id,expires_at);
CREATE TABLE IF NOT EXISTS agent_bot_autonomy_proposal (
  -- 事实提取提案主键
  id CHAR(32) NOT NULL PRIMARY KEY,
  -- 所属身份
  profile_id CHAR(32) NOT NULL REFERENCES agent_bot_autonomy_profile(id),
  -- 来源情节，不删除情节记录以保留证据关联
  episode_id CHAR(32) NOT NULL REFERENCES agent_bot_autonomy_episode(id),
  -- 申请/执行/完成/拒绝/人工核对状态
  status VARCHAR(16) NOT NULL,
  -- 提案审批版本
  revision INTEGER NOT NULL,
  -- 有来源绑定的操作及最终回执；不保存模型凭据
  document_json TEXT NOT NULL CHECK(json_valid(document_json)),
  -- 创建UTC时间
  created_at DATETIME NOT NULL
);
-- 审批列表按身份和状态定位
CREATE INDEX IF NOT EXISTS idx_autonomy_proposal_status ON agent_bot_autonomy_proposal(profile_id,status,created_at);
CREATE TABLE IF NOT EXISTS agent_bot_autonomy_audit (
  -- 审计主键，UTC时间前缀加随机后缀，支持稳定倒序分页
  id CHAR(32) NOT NULL PRIMARY KEY,
  -- 审计所属身份
  profile_id CHAR(32) NOT NULL REFERENCES agent_bot_autonomy_profile(id),
  -- 事件类型，不记录供应商原始异常或密钥
  kind VARCHAR(40) NOT NULL,
  -- 对应任务或提案；空值表示身份级事件
  subject_id CHAR(32) NULL,
  -- 版本快照、状态变化或评估结果
  document_json TEXT NOT NULL CHECK(json_valid(document_json)),
  -- 事件UTC时间
  created_at DATETIME NOT NULL
);
-- 审计支持按身份和时间进行有限分页
CREATE INDEX IF NOT EXISTS idx_autonomy_audit_time ON agent_bot_autonomy_audit(profile_id,created_at,id);
