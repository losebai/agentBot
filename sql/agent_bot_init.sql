-- 用途：统一初始化 agentBot 0.5.9 的业务表与可选本地队列表，供新独立数据库使用。
-- Comfy 插件沿用配置及工作流表，无新增结构；新库默认不启用，旧库由 comfy-plugin-v1 流水保留原启用选择。
-- 执行顺序：管理员先创建并选中数据库，再执行本文件，最后用 settings import-env 导入配置。
-- 数据影响：仅创建不存在的表，不插入配置、不覆盖模型、不清空或迁移现有数据。
-- 统一会话仅建 chat_session_context；既有部署校验迁移与备份后使用 2026-10-20-retire-legacy-session-context.sql 清理旧表。
-- 记忆及工作流索引使用 chat 前缀；既有表遗留 dispatch 索引名时执行 2026-10-13-chat-index-names.sql。
-- 旧服务模型目录 ai_model 已退出运行时，新库不再创建；本脚本不会删除已有旧表。
-- 聊天相关表统一使用 chat_ 前缀；配置键使用 chat_*，对应环境变量为 CHAT_*。
-- 建表按外键依赖排序；字段类型、默认值、可空性、索引和删除约束与运行时代码一致。
-- 服务启动不会自动执行本文件；已有十张自有表补注释时使用独立的注释增量脚本。


-- 运行配置与群成员目录

CREATE TABLE IF NOT EXISTS `chat_setting` (
    `id`            BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    `config_key`    VARCHAR(128) NOT NULL                COMMENT '配置键名（RuntimeConfig 字段名）',
    `config_value`  MEDIUMTEXT                           COMMENT '配置值（JSON 编码）',
    `description`   VARCHAR(256)                         COMMENT '配置项中文说明',
    `create_time`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `update_time`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    -- 主键定义记录身份，防止同一业务记录重复创建。
    PRIMARY KEY (`id`),
    -- 唯一索引约束业务标识，支持并发写入时的重复检查。
    UNIQUE KEY `uk_config_key` (`config_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='chat 配置键值表';

CREATE TABLE IF NOT EXISTS `chat_setting_revision` (
    `id`           BIGINT   NOT NULL COMMENT '固定为 1，保证单行',
    `revision`     BIGINT   NOT NULL COMMENT '当前配置快照版本号，从 1 起',
    `update_time`  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    -- 主键定义记录身份，防止同一业务记录重复创建。
    PRIMARY KEY (`id`),
    -- 固定单行标识，保证配置版本号只维护一个快照。
    CONSTRAINT `chk_chat_setting_revision_singleton` CHECK (`id` = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='chat 配置版本号（单行）';

CREATE TABLE IF NOT EXISTS `chat_group_member` (
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
    -- 主键定义记录身份，防止同一业务记录重复创建。
    PRIMARY KEY (`bot_id`, `group_id`, `user_id`),
    -- 联合索引支持对应字段过滤和有序读取，避免全表扫描。
    KEY `idx_chat_group_member_user` (`bot_id`, `user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='NapCat OneBot QQ群成员目录';

-- 聊天记忆、幂等回执与群上下文

CREATE TABLE IF NOT EXISTS `chat_group_context` (
        `scope_id` VARCHAR(128) NOT NULL COMMENT 'HMAC Bot/群作用域',
        `messages_json` MEDIUMTEXT NOT NULL COMMENT '强类型最近群文本 JSON',
        `expires_at` DATETIME(6) NOT NULL COMMENT '滑动过期时间',
        `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
        `update_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
            ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间',
        -- 主键定义记录身份，防止同一业务记录重复创建。
        PRIMARY KEY (`scope_id`),
        -- 联合索引支持对应字段过滤和有序读取，避免全表扫描。
        KEY `idx_chat_group_context_expires` (`expires_at`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='chat 群聊最近文本';

-- 标签记忆：由应用事务维护关联，不添加外键

-- 旧关联表已合入 chat_tagged_memory，不再单独创建。

CREATE TABLE IF NOT EXISTS chat_tagged_memory (
    id INT NOT NULL AUTO_INCREMENT COMMENT '记忆自增主键',
    scope_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '关联作用域',
    memory_key VARCHAR(80) COLLATE utf8mb4_bin NOT NULL COMMENT '稳定事实键',
    content VARCHAR(1000) NOT NULL COMMENT '已确认的事实',
    is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '逻辑删除标记',
    create_time DATETIME NOT NULL COMMENT '创建时间',
    update_time DATETIME NOT NULL COMMENT '更新时间',
    scope VARCHAR(16) NOT NULL COMMENT 'global/session/user/group',
    tags_json JSON NOT NULL COMMENT '规范化主题标签数组',
    material_ids_json JSON NOT NULL COMMENT '引用素材标识数组',
    revision INT NOT NULL DEFAULT 1 COMMENT '事实乐观锁版本',
    topic VARCHAR(80) NOT NULL DEFAULT '' COMMENT '用于整理的规范主题',
    kind VARCHAR(20) NOT NULL DEFAULT 'fact' COMMENT '事实类型',
    status VARCHAR(16) NOT NULL DEFAULT 'active' COMMENT '生命周期状态',
    subject_type VARCHAR(16) NOT NULL DEFAULT 'unknown' COMMENT '被描述主体类型',
    subject_id VARCHAR(64) NOT NULL DEFAULT '' COMMENT '匿名主体标识',
    source_kind VARCHAR(24) NOT NULL DEFAULT 'legacy_unknown' COMMENT '最近确认的来源类型',
    importance DOUBLE NOT NULL DEFAULT 0.5 COMMENT '重要性排序权重非正确概率',
    observed_at DATETIME NULL COMMENT '已知事实时间未知为空',
    valid_from DATETIME NULL COMMENT 'UTC有效区间起点含边界',
    valid_until DATETIME NULL COMMENT 'UTC有效区间终点不含边界',
    task_state VARCHAR(16) NULL COMMENT '任务未完成完成或取消',
    merged_into_id INT NULL COMMENT '合并后的当前事实标识',
    conflict_set_id VARCHAR(64) NULL COMMENT '待解决冲突集合',
    content_fingerprint CHAR(64) NOT NULL DEFAULT '' COMMENT '规范正文摘要用于精确去重候选',
    last_used_at DATETIME NULL COMMENT '实际采用时间不是事实时间',
    use_count INT NOT NULL DEFAULT 0 COMMENT '幂等累计采用次数',
    created_time_basis VARCHAR(24) NOT NULL DEFAULT 'utc' COMMENT '创建时间的UTC或旧未知时区依据',
    updated_time_basis VARCHAR(24) NOT NULL DEFAULT 'utc' COMMENT '更新时间的UTC或旧未知时区依据',
    -- 主键定义记录身份，防止同一业务记录重复创建。
    PRIMARY KEY (id),
    -- 唯一索引约束业务标识，支持并发写入时的重复检查。
    UNIQUE KEY uk_tagged_memory_scope_key (scope_id, memory_key),
    -- 联合索引支持对应字段过滤和有序读取，避免全表扫描。
    KEY idx_tagged_memory_scope_time (scope_id, is_deleted, update_time),
    -- 支持授权状态、主体去重与主题整理查询。
    KEY idx_memory_status (scope_id,status),
    -- 支持授权状态、主体去重与主题整理查询。
    KEY idx_memory_fingerprint (scope_id,subject_id,content_fingerprint),
    -- 支持授权状态、主体去重与主题整理查询。
    KEY idx_memory_topic (scope_id,subject_id,topic),
    -- 支持授权状态、主体去重与主题整理查询。
    KEY idx_memory_merged (merged_into_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='LLM 自主保存的标签记忆';

-- 旧关联表已合入 chat_tagged_memory，不再单独创建。

-- 本地 ComfyUI 工作流：包含图生图字段和内容唯一索引

CREATE TABLE IF NOT EXISTS `chat_workflow` (
    `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键',
    `workflow_key` VARCHAR(128) NOT NULL COMMENT '稳定工作流键',
    `display_name` VARCHAR(256) NOT NULL COMMENT '工作流显示名称',
    `source_file` VARCHAR(512) NOT NULL COMMENT '导入来源文件名',
    `source_format` VARCHAR(16) NOT NULL COMMENT '来源格式：ui 或 api',
    `ui_json` MEDIUMTEXT NULL COMMENT 'ComfyUI 画布工作流 JSON',
    `api_json` MEDIUMTEXT NULL COMMENT '可提交给 ComfyUI 的 API 工作流 JSON',
    `positive_node_id` VARCHAR(64) NULL COMMENT '正向提示词节点 ID',
    `negative_node_id` VARCHAR(64) NULL COMMENT '负向提示词节点 ID',
    `output_node_id` VARCHAR(64) NULL COMMENT '图片输出节点 ID',
    `seed_node_id` VARCHAR(64) NULL COMMENT '随机种子所在节点 ID',
    `workflow_type` VARCHAR(16) NOT NULL DEFAULT 'text_to_image' COMMENT '工作流类型',
    `input_image_node_id` VARCHAR(64) NULL COMMENT '用户原图 LoadImage 节点 ID',
    `positive_input_name` VARCHAR(64) NOT NULL DEFAULT 'text' COMMENT '正向或编辑指令字段名',
    `negative_input_name` VARCHAR(64) NOT NULL DEFAULT 'text' COMMENT '负向提示词字段名',
    `description` VARCHAR(2000) NOT NULL DEFAULT '' COMMENT '供 Agent 选择工作流的用途描述',
    `additional_positive_prompt` VARCHAR(2000) NOT NULL DEFAULT '' COMMENT '生成时固定追加的正向提示词',
    `additional_negative_prompt` VARCHAR(2000) NOT NULL DEFAULT '' COMMENT '生成时固定追加的负向提示词',
    `enabled` TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否允许 Agent 执行',
    `content_sha256` CHAR(64) NOT NULL COMMENT '规范化来源内容 SHA-256',
    `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `update_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    -- 主键定义记录身份，防止同一业务记录重复创建。
    PRIMARY KEY (`id`),
    -- 唯一索引约束业务标识，支持并发写入时的重复检查。
    UNIQUE KEY `uk_chat_workflow_key` (`workflow_key`),
    -- 唯一索引约束业务标识，支持并发写入时的重复检查。
    UNIQUE KEY `uk_chat_workflow_content_sha256` (`content_sha256`),
    -- 联合索引支持对应字段过滤和有序读取，避免全表扫描。
    KEY `idx_chat_workflow_enabled` (`enabled`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='本地 Comfy 工作流定义与执行绑定';

-- Python AI：供应商先于模型创建

CREATE TABLE IF NOT EXISTS agent_bot_ai_provider (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  name VARCHAR(100) COLLATE utf8mb4_bin NOT NULL COMMENT '显示名称',
  protocol VARCHAR(32) NOT NULL COMMENT '供应商接口协议',
  base_url VARCHAR(2048) NOT NULL COMMENT '供应商接口根地址',
  credential TEXT NULL COMMENT '供应商或 MCP 实际凭据，仅写入和内部调用，不通过管理接口回显',
  credential_required TINYINT NOT NULL DEFAULT 0 COMMENT '是否要求鉴权：零无需凭据，一必须配置凭据',
  enabled TINYINT NOT NULL DEFAULT 0 COMMENT '是否启用：零禁用，一启用',
  description VARCHAR(1000) NOT NULL DEFAULT '' COMMENT '中文用途说明',
  revision BIGINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '乐观锁版本，每次修改递增',
  created_at DATETIME(6) NOT NULL COMMENT '创建时间，协调世界时',
  updated_at DATETIME(6) NOT NULL COMMENT '更新时间，协调世界时',
  -- 主键定义记录身份，防止同一业务记录重复创建。
  PRIMARY KEY (id),
  -- 唯一索引约束业务标识，支持并发写入时的重复检查。
  UNIQUE KEY uq_provider_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模型供应商配置';

CREATE TABLE IF NOT EXISTS agent_bot_ai_model (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  alias VARCHAR(200) COLLATE utf8mb4_bin NULL COMMENT '历史别名仅供兼容，新模型为空',
  upstream_model VARCHAR(200) COLLATE utf8mb4_bin NOT NULL COMMENT '上游实际模型标识',
  provider_id BIGINT UNSIGNED NOT NULL COMMENT '所属供应商主键',
  enabled TINYINT NOT NULL DEFAULT 0 COMMENT '是否启用：零禁用，一启用',
  revision BIGINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '乐观锁版本，每次修改递增',
  created_at DATETIME(6) NOT NULL COMMENT '创建时间，协调世界时',
  updated_at DATETIME(6) NOT NULL COMMENT '更新时间，协调世界时',
  -- 主键定义记录身份，防止同一业务记录重复创建。
  PRIMARY KEY (id),
  -- 唯一索引约束业务标识，支持并发写入时的重复检查。
  UNIQUE KEY uq_model_alias (alias),
  -- 供应商与上游模型共同定义业务身份，二进制排序规则保留大小写。
  UNIQUE KEY uq_model_provider_upstream (provider_id, upstream_model),
  -- 外键保证引用存在；级联规则沿用原定义，保持删除语义。
  CONSTRAINT fk_agent_bot_model_provider FOREIGN KEY (provider_id) REFERENCES agent_bot_ai_provider (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模型目录，稳定主键定义引用身份';

-- 普通调用统一保存在 agent_bot_call_record，不再创建旧执行表。

-- Python Agent、Skill、MCP 与运行记录：被引用表先创建

CREATE TABLE IF NOT EXISTS agent_bot_agent_run (
  execution_id CHAR(32) COLLATE utf8mb4_bin NOT NULL PRIMARY KEY COMMENT '执行标识，三十二位十六进制',
  session_id VARCHAR(256) COLLATE utf8mb4_bin NOT NULL COMMENT '区分大小写的会话标识',
  request_id VARCHAR(256) NOT NULL COMMENT '调用方请求标识',
  entry VARCHAR(16) NOT NULL COMMENT '调用入口：管理端或机器人',
  model VARCHAR(255) NOT NULL COMMENT '实际使用的模型标识',
  backend VARCHAR(32) NOT NULL COMMENT '执行后端标识',
  `status` VARCHAR(16) NOT NULL COMMENT '执行状态：运行中、完成、失败或取消',
  duration_ms BIGINT UNSIGNED NOT NULL COMMENT '执行耗时，单位毫秒',
  `usage` JSON NOT NULL COMMENT '令牌用量统计',
  created_at DATETIME(6) NOT NULL COMMENT '创建时间，协调世界时',
  error_code VARCHAR(100) NULL COMMENT '稳定错误代码',
  prompt MEDIUMTEXT NOT NULL COMMENT '用户输入文本',
  content MEDIUMTEXT NOT NULL COMMENT '模型输出文本',
  revisions JSON NOT NULL COMMENT '本次执行使用的配置版本快照',
  -- 联合索引支持对应字段过滤和有序读取，避免全表扫描。
  KEY idx_agent_run_created(created_at, execution_id),
  -- 联合索引支持对应字段过滤和有序读取，避免全表扫描。
  KEY idx_agent_run_session(session_id, created_at, execution_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='智能体执行记录';

CREATE TABLE IF NOT EXISTS agent_bot_agent_step (
  execution_id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '执行标识，三十二位十六进制',
  sequence INT NOT NULL COMMENT '执行内递增事件序号',
  turn INT NOT NULL COMMENT '模型调用轮次',
  kind VARCHAR(32) NOT NULL COMMENT '步骤事件类型',
  tool VARCHAR(200) NULL COMMENT '工具名称',
  call_id VARCHAR(256) NULL COMMENT '模型工具调用标识',
  status VARCHAR(16) NOT NULL COMMENT '执行状态：运行中、完成、失败或取消',
  duration_ms BIGINT UNSIGNED NOT NULL COMMENT '执行耗时，单位毫秒',
  summary VARCHAR(1000) NOT NULL COMMENT '脱敏后的步骤摘要',
  arguments JSON NULL DEFAULT NULL COMMENT '工具调用的脱敏入参；历史空值表示未记录',
  result JSON NULL DEFAULT NULL COMMENT '脱敏返回值或未知结果错误信息；不包含媒体二进制',
  -- 主键定义记录身份，防止同一业务记录重复创建。
  PRIMARY KEY(execution_id,sequence),
  -- 外键保证引用存在；级联规则沿用原定义，保持删除语义。
  CONSTRAINT fk_agent_step_run FOREIGN KEY(execution_id) REFERENCES agent_bot_agent_run(execution_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='智能体执行步骤';

CREATE TABLE IF NOT EXISTS agent_bot_skill (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
  name VARCHAR(200) NOT NULL COMMENT '显示名称',
  description VARCHAR(2000) NOT NULL COMMENT '中文用途说明',
  category VARCHAR(64) NOT NULL COMMENT '功能分类',
  system_prompt MEDIUMTEXT NOT NULL COMMENT '系统提示词',
  sample_prompt MEDIUMTEXT NOT NULL COMMENT '示例提示词',
  tools JSON NOT NULL COMMENT '允许使用的本地工具列表',
  enabled TINYINT NOT NULL DEFAULT 0 COMMENT '是否启用：零禁用，一启用',
  revision BIGINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '乐观锁版本，每次修改递增',
  created_at DATETIME(6) NOT NULL COMMENT '创建时间，协调世界时',
  updated_at DATETIME(6) NOT NULL COMMENT '更新时间，协调世界时'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='技能目录';

CREATE TABLE IF NOT EXISTS agent_bot_mcp (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
  code VARCHAR(64) COLLATE utf8mb4_bin NOT NULL UNIQUE COMMENT '区分大小写的唯一代码',
  name VARCHAR(200) NOT NULL COMMENT '显示名称',
  description VARCHAR(2000) NOT NULL COMMENT '中文用途说明',
  category VARCHAR(16) NOT NULL COMMENT '功能分类',
  transport VARCHAR(16) NOT NULL COMMENT '远程工具传输方式',
  endpoint VARCHAR(512) NOT NULL COMMENT '远程工具服务端点',
  credential TEXT NULL COMMENT '供应商或 MCP 实际凭据，仅写入和内部调用，不通过管理接口回显',
  credential_required TINYINT NOT NULL DEFAULT 0 COMMENT '是否要求鉴权：零无需凭据，一必须配置凭据',
  allowed_tools JSON NOT NULL COMMENT '允许使用的远程工具列表',
  search_tool VARCHAR(100) NULL COMMENT '搜索工具名称',
  timeout_seconds INT NOT NULL COMMENT '调用超时时间，单位秒',
  required TINYINT NOT NULL DEFAULT 0 COMMENT '是否为执行必需服务',
  enabled TINYINT NOT NULL DEFAULT 0 COMMENT '是否启用：零禁用，一启用',
  revision BIGINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '乐观锁版本，每次修改递增',
  created_at DATETIME(6) NOT NULL COMMENT '创建时间，协调世界时',
  updated_at DATETIME(6) NOT NULL COMMENT '更新时间，协调世界时'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='远程工具服务目录';

CREATE TABLE IF NOT EXISTS agent_bot_agent (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
  code VARCHAR(64) COLLATE utf8mb4_bin NOT NULL UNIQUE COMMENT '区分大小写的唯一代码',
  name VARCHAR(200) NOT NULL COMMENT '显示名称',
  description VARCHAR(2000) NOT NULL COMMENT '中文用途说明',
  system_prompt MEDIUMTEXT NOT NULL COMMENT '系统提示词',
  model_id BIGINT UNSIGNED NOT NULL COMMENT '关联的模型主键',
  vision_model_id BIGINT UNSIGNED NULL DEFAULT NULL COMMENT '图片理解模型；空值沿用聊天模型',
  fallback_model_id BIGINT UNSIGNED NULL DEFAULT NULL COMMENT '错误转发模型；空值禁用转发，每请求最多接管一次',
  tools JSON NOT NULL COMMENT '允许使用的本地工具列表',
  max_steps INT NOT NULL COMMENT '最大工具调用轮次',
  options JSON NOT NULL COMMENT '模型生成参数',
  enabled TINYINT NOT NULL DEFAULT 0 COMMENT '是否启用：零禁用，一启用',
  revision BIGINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '乐观锁版本，每次修改递增',
  created_at DATETIME(6) NOT NULL COMMENT '创建时间，协调世界时',
  updated_at DATETIME(6) NOT NULL COMMENT '更新时间，协调世界时',
  personality_prompt MEDIUMTEXT NOT NULL COMMENT '独立人格设定，空字符串表示仅使用角色设定',
  -- 外键保证引用存在；级联规则沿用原定义，保持删除语义。
  CONSTRAINT fk_agent_model FOREIGN KEY(model_id) REFERENCES agent_bot_ai_model(id),
  -- 路由引用阻止删除仍被 Agent 使用的模型，不级联删除 Agent。
  CONSTRAINT fk_agent_vision_model FOREIGN KEY(vision_model_id) REFERENCES agent_bot_ai_model(id),
  CONSTRAINT fk_agent_fallback_model FOREIGN KEY(fallback_model_id) REFERENCES agent_bot_ai_model(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='智能体配置';

CREATE TABLE IF NOT EXISTS agent_bot_agent_skill (
  agent_id BIGINT UNSIGNED NOT NULL COMMENT '关联的智能体主键',
  skill_id BIGINT UNSIGNED NOT NULL COMMENT '关联的技能主键',
  -- 主键定义记录身份，防止同一业务记录重复创建。
  PRIMARY KEY(agent_id,skill_id),
  -- 外键保证引用存在；级联规则沿用原定义，保持删除语义。
  CONSTRAINT fk_agent_skill_agent FOREIGN KEY(agent_id) REFERENCES agent_bot_agent(id) ON DELETE CASCADE,
  -- 外键保证引用存在；级联规则沿用原定义，保持删除语义。
  CONSTRAINT fk_agent_skill_skill FOREIGN KEY(skill_id) REFERENCES agent_bot_skill(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='智能体技能关联';

CREATE TABLE IF NOT EXISTS agent_bot_agent_mcp (
  agent_id BIGINT UNSIGNED NOT NULL COMMENT '关联的智能体主键',
  mcp_id BIGINT UNSIGNED NOT NULL COMMENT '关联的远程工具主键',
  -- 主键定义记录身份，防止同一业务记录重复创建。
  PRIMARY KEY(agent_id,mcp_id),
  -- 外键保证引用存在；级联规则沿用原定义，保持删除语义。
  CONSTRAINT fk_agent_mcp_agent FOREIGN KEY(agent_id) REFERENCES agent_bot_agent(id) ON DELETE CASCADE,
  -- 外键保证引用存在；级联规则沿用原定义，保持删除语义。
  CONSTRAINT fk_agent_mcp_mcp FOREIGN KEY(mcp_id) REFERENCES agent_bot_mcp(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='智能体远程工具关联';

-- 媒体能力、执行记录与文件元数据

CREATE TABLE IF NOT EXISTS agent_bot_ai_model_media (
  model_id BIGINT UNSIGNED NOT NULL COMMENT '所属模型标识，同模型版本一起更新',
  vision_enabled TINYINT NOT NULL DEFAULT 0 COMMENT '是否明确启用图片理解能力',
  image_api_profile VARCHAR(32) NOT NULL DEFAULT 'none' COMMENT '图片接口类型：none、gpt-image 或 legacy-b64',
  audio_chat_enabled TINYINT NOT NULL DEFAULT 0 COMMENT '是否启用对话音频输出',
  -- 主键定义记录身份，防止同一业务记录重复创建。
  PRIMARY KEY (model_id),
  -- 能力记录随模型删除，避免遗留无归属配置。
  -- 外键保证引用存在；级联规则沿用原定义，保持删除语义。
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
  -- 主键定义记录身份，防止同一业务记录重复创建。
  PRIMARY KEY (execution_id),
  -- 时间与唯一标识组成稳定分页游标；会话索引支持过滤。
  -- 联合索引支持对应字段过滤和有序读取，避免全表扫描。
  KEY idx_media_run_created (created_at,execution_id),
  -- 联合索引支持对应字段过滤和有序读取，避免全表扫描。
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
  -- 主键定义记录身份，防止同一业务记录重复创建。
  PRIMARY KEY (id),
  -- 执行关联支持两类运行记录，归属由服务层校验；清理索引避免扫描正文。
  -- 联合索引支持对应字段过滤和有序读取，避免全表扫描。
  KEY idx_media_asset_execution (execution_kind,execution_id),
  -- 联合索引支持对应字段过滤和有序读取，避免全表扫描。
  KEY idx_media_asset_expiry (status,expires_at,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='受认证媒体文件元数据';

-- 用途：为纯 Python 一次性任务及自动发言决策增加三张表。
-- 顺序：已有 agentBot 数据库由管理员备份后显式执行；新库使用统一初始化脚本。
-- 数据影响：仅创建不存在的表，不复制 Java 待办、不覆盖配置、不删除任何数据；无自动执行。
-- 所有时间为 UTC，任务正文不包含服务凭据；决策记录不保存群聊状态正文。

CREATE TABLE IF NOT EXISTS agent_bot_job_owner (
  owner_key CHAR(64) COLLATE utf8mb4_bin NOT NULL COMMENT '机器人、会话、发起人的稳定摘要，作为创建配额行锁',
  -- 主键串行化同一所有者的创建事务，防止并发突破五十个活动任务的上限。
  PRIMARY KEY (owner_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='一次性任务所有者配额锁';

CREATE TABLE IF NOT EXISTS agent_bot_job (
  id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '服务端随机任务标识',
  owner_key CHAR(64) COLLATE utf8mb4_bin NOT NULL COMMENT '可信所有者摘要',
  deduplication_key CHAR(64) COLLATE utf8mb4_bin NOT NULL COMMENT '所有者与请求组成的幂等摘要，每条请求最多一个任务',
  bot_id VARCHAR(20) COLLATE utf8mb4_bin NOT NULL COMMENT '原机器人 QQ 号',
  session_id VARCHAR(256) COLLATE utf8mb4_bin NOT NULL COMMENT '原会话标识',
  sender_id VARCHAR(20) COLLATE utf8mb4_bin NOT NULL COMMENT '发起人 QQ 号',
  scene VARCHAR(16) NOT NULL COMMENT '原投递场景 group 或 private',
  group_id VARCHAR(20) COLLATE utf8mb4_bin NULL COMMENT '群聊目标，私聊为空',
  model VARCHAR(200) NOT NULL COMMENT '创建时的有效模型别名，到期仍须在目录启用',
  system_prompt MEDIUMTEXT NOT NULL COMMENT '创建时的系统提示快照，不含凭据',
  prompt TEXT NOT NULL COMMENT '一次性任务正文，最多八千字符',
  status VARCHAR(16) NOT NULL COMMENT '状态 PENDING、RUNNING、SUCCEEDED、FAILED、CANCELLED',
  result MEDIUMTEXT NULL COMMENT '成功模型正文，最多六万四千字符',
  error_code VARCHAR(100) NULL COMMENT '稳定脱敏错误码，不保存异常正文',
  run_at DATETIME(6) NOT NULL COMMENT '计划执行时间，UTC',
  created_at DATETIME(6) NOT NULL COMMENT '创建时间，UTC',
  started_at DATETIME(6) NULL COMMENT '获得执行权时间，UTC',
  completed_at DATETIME(6) NULL COMMENT '进入终态时间，UTC',
  delivered_at DATETIME(6) NULL COMMENT '消息队列确认投递完成时间，UTC',
  -- 主键与唯一幂等索引分别约束任务身份和每请求唯一创建。
  PRIMARY KEY (id),
  UNIQUE KEY uk_job_request (deduplication_key),
  -- 所有者索引支持活动配额和最近任务查询。
  KEY idx_job_owner_status (owner_key,status),
  KEY idx_job_owner_created (owner_key,created_at,id),
  -- 调度、陈旧执行恢复和机器人待投递结果分别使用独立复合索引。
  KEY idx_job_due (status,run_at,id),
  KEY idx_job_started (status,started_at),
  KEY idx_job_delivery (bot_id,status,delivered_at,id),
  -- 任务引用稳定所有者，删除任务不会删除其他历史或配额锁。
  CONSTRAINT fk_job_owner FOREIGN KEY (owner_key) REFERENCES agent_bot_job_owner(owner_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Python 一次性文本任务及投递回执';

CREATE TABLE IF NOT EXISTS agent_bot_auto_speak_decision (
  id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '服务端决策记录标识',
  session_id VARCHAR(256) COLLATE utf8mb4_bin NOT NULL COMMENT '自动发言会话标识',
  request_id VARCHAR(256) NOT NULL COMMENT '本次决策请求标识',
  mcp_code VARCHAR(64) NOT NULL COMMENT '已登记的决策服务代码',
  mcp_revision BIGINT UNSIGNED NULL COMMENT '本次决策使用的服务配置版本',
  status VARCHAR(16) NOT NULL COMMENT 'completed、failed 或 cancelled',
  should_speak TINYINT NULL COMMENT '有效结果是否发言，失败为空',
  confidence DOUBLE NULL COMMENT '有效结果置信度，零到一，失败为空',
  duration_ms BIGINT UNSIGNED NOT NULL COMMENT '本次调用耗时，毫秒',
  error_code VARCHAR(100) NULL COMMENT '稳定脱敏错误码，不保存远端原文',
  created_at DATETIME(6) NOT NULL COMMENT '记录时间，UTC',
  -- 主键保证记录身份；时间与会话索引用于有界查询与部署方清理。
  PRIMARY KEY (id),
  KEY idx_auto_decision_created (created_at,id),
  KEY idx_auto_decision_session (session_id,created_at,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Python 自动发言固定选择决策记录';

-- 统一普通调用、Agent 与旧系统调用记录；先执行本脚本，再运行 records migrate。
-- 仅新增表，不修改或删除原始记录。来源唯一键支持重复执行历史导入。
CREATE TABLE IF NOT EXISTS agent_bot_call_record (
  `id` INT NOT NULL AUTO_INCREMENT COMMENT '统一调用记录主键',
  `session_Id` VARCHAR(255) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '会话标识',
  `content` TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin COMMENT '用户输入正文',
  `replay_context` TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin COMMENT '模型回复正文',
  `create_time` DATETIME DEFAULT NULL COMMENT '调用记录时间，保留源时间',
  `created_at_utc` DATETIME(6) DEFAULT NULL COMMENT '普通执行原始UTC时间，保留微秒；其他历史来源可空',
  `duration` INT DEFAULT NULL COMMENT '调用耗时，单位 ms',
  `user_certificate` VARCHAR(255) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '用户凭据摘要，不保存原始凭据',
  `model` VARCHAR(255) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '实际调用模型',
  `data` JSON DEFAULT NULL COMMENT '响应数据、用量及兼容扩展字段',
  `request_context` JSON DEFAULT NULL COMMENT 'AI请求上下文（不含临时凭据）',
  `request_id` VARCHAR(255) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '请求关联标识',
  `type` VARCHAR(16) COLLATE utf8mb4_bin NOT NULL COMMENT '类型：ordinary、agent、auto_speak、unknown',
  `source` VARCHAR(191) COLLATE utf8mb4_bin NOT NULL COMMENT '来源系统及表的稳定命名空间',
  `source_id` VARCHAR(64) COLLATE utf8mb4_bin NOT NULL COMMENT '来源记录原始主键',
  `execution_id` CHAR(32) COLLATE utf8mb4_bin DEFAULT NULL COMMENT '本地执行标识，保留步骤和附件关联',
  `status` VARCHAR(16) COLLATE utf8mb4_bin NOT NULL COMMENT '执行状态，缺失历史状态为 unknown',
  PRIMARY KEY (`id`),
  KEY idx_call_time (`create_time`, `id`) COMMENT '按调用时间稳定分页',
  KEY idx_call_source_created (`source`, `created_at_utc`, `execution_id`) COMMENT '普通来源按原始UTC微秒和执行标识分页',
  UNIQUE KEY uk_call_source (`source`, `source_id`) COMMENT '来源内去重，避免不同系统主键冲突',
  KEY idx_call_type (`type`, `create_time`, `id`) COMMENT '按类型分页',
  KEY idx_call_session (`session_Id`, `create_time`, `id`) COMMENT '按会话分页',
  KEY idx_call_status (`status`, `create_time`, `id`) COMMENT '按状态分页'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='统一 AI 调用记录';


-- 可选本地队列结构同步；运行时默认使用独立 data/queue.sqlite3。
-- 用途：为既有 MySQL 结构同步可选本地队列的数据结构定义，保持统一初始化基线完整。
-- 执行顺序：在已有业务初始化脚本之后执行；默认运行时实际使用独立 SQLite 队列文件。
-- 数据影响：仅创建缺失表，不迁移 Redis 任务、不覆盖业务数据；MySQL 队列表不参与默认运行。

CREATE TABLE IF NOT EXISTS chat_queue_jobs (
    stream_id BIGINT NOT NULL AUTO_INCREMENT COMMENT '单调递增任务顺序',
    namespace VARCHAR(128) NOT NULL COMMENT '文本或图片队列命名空间',
    payload LONGTEXT NOT NULL COMMENT '已验证任务 JSON',
    claimed_at DOUBLE NULL COMMENT '最近领取时间，空值为未领取',
    state LONGTEXT NOT NULL COMMENT '重试和投递检查点 JSON',
    result LONGTEXT NULL COMMENT '已生成结果 JSON，恢复时避免重复模型调用',
    result_expires_at DOUBLE NULL COMMENT '结果保留截止时间',
    PRIMARY KEY (stream_id),
    KEY chat_queue_jobs_namespace (namespace, stream_id) COMMENT '按队列顺序领取及恢复'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='持久任务和执行检查点';

CREATE TABLE IF NOT EXISTS chat_queue_receipts (
    namespace VARCHAR(128) NOT NULL COMMENT '队列命名空间',
    job_id VARCHAR(128) NOT NULL COMMENT '稳定任务标识',
    digest CHAR(64) NOT NULL COMMENT '任务内容摘要',
    stream_id BIGINT NOT NULL COMMENT '原始任务顺序，完成后仍可返回回执',
    expires_at DOUBLE NULL COMMENT '回执到期时间，活动任务为空',
    PRIMARY KEY (namespace, job_id),
    KEY chat_queue_receipts_expiry (namespace, expires_at) COMMENT '清理已完成的过期回执'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='任务入队去重回执';

CREATE TABLE IF NOT EXISTS chat_queue_deadletters (
    id BIGINT NOT NULL AUTO_INCREMENT COMMENT '死信写入顺序',
    namespace VARCHAR(128) NOT NULL COMMENT '队列命名空间',
    source_id BIGINT NOT NULL COMMENT '原始任务顺序',
    payload LONGTEXT NULL COMMENT '合法失败任务 JSON，损坏内容不复制',
    reason VARCHAR(128) NOT NULL COMMENT '有界脱敏失败原因',
    attempt INT NOT NULL COMMENT '失败时尝试次数',
    failed_at DOUBLE NOT NULL COMMENT '失败时间',
    PRIMARY KEY (id),
    KEY chat_queue_deadletters_namespace (namespace, id) COMMENT '按命名空间裁剪最旧死信'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='有界失败任务记录';

CREATE TABLE IF NOT EXISTS chat_image_tasks (
    namespace VARCHAR(128) NOT NULL COMMENT '图片队列命名空间',
    task_id VARCHAR(128) NOT NULL COMMENT '稳定图片任务标识',
    owner VARCHAR(512) NOT NULL COMMENT '机器人会话用户归属 JSON',
    created_at DOUBLE NOT NULL COMMENT '创建时间',
    payload LONGTEXT NOT NULL COMMENT '执行投递取消和上游任务 JSON',
    expires_at DOUBLE NULL COMMENT '终态保留截止时间，活动任务为空',
    PRIMARY KEY (namespace, task_id),
    KEY chat_image_tasks_owner (namespace, owner, created_at) COMMENT '按完整归属列出最近任务',
    KEY chat_image_tasks_expiry (namespace, expires_at) COMMENT '回收过期终态'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='图片任务及不可撤销取消意图';

CREATE TABLE IF NOT EXISTS chat_tool_operations (
    namespace VARCHAR(128) NOT NULL COMMENT '服务命名空间',
    job_id VARCHAR(128) NOT NULL COMMENT '所属对话任务标识',
    tool VARCHAR(64) NOT NULL COMMENT '授权工具名称',
    payload LONGTEXT NOT NULL COMMENT '参数摘要执行状态和结果 JSON',
    expires_at DOUBLE NOT NULL COMMENT '记录保留截止时间',
    PRIMARY KEY (namespace, job_id, tool),
    KEY chat_tool_operations_expiry (namespace, expires_at) COMMENT '回收过期执行权和结果'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='工具副作用执行权与持久结果';

-- 标签知识库增量：既有 MySQL 部署显式执行一次，可重复执行。
-- 在当前业务结构之后执行，仅新增知识正文和标签表，不改写记忆、配置或密钥。
-- 两表关系由应用事务维护，逻辑删除保留原文和标签，不添加外键。
CREATE TABLE IF NOT EXISTS chat_knowledge (
    id INT NOT NULL AUTO_INCREMENT COMMENT '只新增知识条目的自增标识',
    title VARCHAR(200) NOT NULL COMMENT '知识标题，新增后不可编辑',
    content TEXT NOT NULL COMMENT '知识正文，应用限制一至一万六千字符，新增后不可编辑',
    is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '逻辑删除标记，删除保留历史正文',
    create_time DATETIME NOT NULL COMMENT '知识创建时间',
    PRIMARY KEY (id),
    KEY idx_knowledge_active_id (is_deleted, id) COMMENT '按有效状态和标识分页'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='管理员维护的只新增共享知识';

CREATE TABLE IF NOT EXISTS chat_knowledge_tag (
    knowledge_id INT NOT NULL COMMENT '知识条目标识，由应用事务保证关联',
    tag VARCHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '规范化主题标签，新增后不可编辑',
    PRIMARY KEY (knowledge_id, tag),
    KEY idx_knowledge_tag_lookup (tag, knowledge_id) COMMENT '按标签筛选知识和统计有效条目'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='知识条目与主题标签关联';


-- 用途：新增持久图片/文件素材、接收来源和长期记忆素材关联。
-- 顺序：已有 MySQL 数据库备份后显式执行；依赖 chat_tagged_memory 已存在。
-- 数据影响：只新增表和索引，不重建旧表，不迁移或删除临时附件和历史记忆。
CREATE TABLE IF NOT EXISTS chat_material (
    id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '服务端生成的持久素材标识',
    entry VARCHAR(16) NOT NULL COMMENT '可信入口 qq 或 admin',
    session_id VARCHAR(256) COLLATE utf8mb4_bin NOT NULL COMMENT '匿名会话归属，查询及原件读取均校验',
    name VARCHAR(255) NOT NULL COMMENT '原始文件显示名称，不参与构造磁盘路径',
    kind VARCHAR(16) NOT NULL COMMENT '素材类别 image 或 file',
    mime_type VARCHAR(127) NOT NULL COMMENT '已验证的文件媒体类型',
    size_bytes BIGINT UNSIGNED NOT NULL COMMENT '实际原件字节数',
    sha256 CHAR(64) COLLATE utf8mb4_bin NOT NULL COMMENT '原件内容摘要，会话内复用相同文件',
    storage_key CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '受限存储键，等于素材 ID',
    description VARCHAR(1000) NOT NULL DEFAULT '' COMMENT '已有文字说明，不自动调用模型生成',
    created_at DATETIME(6) NOT NULL COMMENT '创建时间，统一使用 UTC',
    -- 主键作为消息和记忆引用的稳定身份。
    PRIMARY KEY (id),
    -- 去重限制在相同入口和会话，不能跨会话探测私有文件。
    UNIQUE KEY uk_material_owner_digest (entry,session_id,sha256),
    -- 支持会话内按类别和创建时间分页。
    KEY idx_material_owner_time (entry,session_id,kind,created_at,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='独立于临时附件生命周期的持久素材';

CREATE TABLE IF NOT EXISTS chat_material_source (
    entry VARCHAR(16) NOT NULL COMMENT '接收来源的可信入口',
    session_id VARCHAR(256) COLLATE utf8mb4_bin NOT NULL COMMENT '来源所属匿名会话',
    source_key CHAR(64) COLLATE utf8mb4_bin NOT NULL COMMENT '机器人及原消息附件身份的 SHA256',
    material_id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '关联的持久素材 ID',
    -- 同一来源不可绑定到不同素材，引用直接复用此关联。
    PRIMARY KEY (entry,session_id,source_key),
    -- 支持按素材检查来源，外键保护来源不会悬空。
    KEY idx_material_source_material (material_id),
    CONSTRAINT fk_material_source_material FOREIGN KEY (material_id) REFERENCES chat_material(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='收到或引用的消息附件与素材关联';

-- 用途：为持久素材新增多对多来源类型和规范化标签，供管理与会话工具精确筛选。
-- 顺序：适用于已具备 chat_material 的 0.5.5 至 0.5.9 部署，由 material-classification-v1 登记后执行。
-- 数据影响：仅新增关联表和索引，不改写旧素材，不猜测或回填历史分类和标签。
CREATE TABLE IF NOT EXISTS chat_material_category (
    material_id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '所属持久素材 ID',
    category VARCHAR(16) COLLATE utf8mb4_bin NOT NULL COMMENT '来源类型 received、sent、generated 或 uploaded',
    -- 同一素材保留多个来源，复用原件不会覆盖先前分类。
    PRIMARY KEY (material_id,category),
    -- 类型在前支持精确类型筛选，素材在后供 EXISTS 关联和覆盖读取。
    KEY idx_material_category_lookup (category,material_id),
    -- 外键保证每个来源类型都归属于真实素材。
    CONSTRAINT fk_material_category_material FOREIGN KEY (material_id) REFERENCES chat_material(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='持久素材与实际来源类型关联';

CREATE TABLE IF NOT EXISTS chat_material_tag (
    material_id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '所属持久素材 ID',
    tag VARCHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '经 NFKC、去首尾空白和大小写折叠的主题标签',
    -- 同一素材标签唯一，应用层限制完整标签组最多八个。
    PRIMARY KEY (material_id,tag),
    -- 标签在前支持精确筛选，避免多标签 JOIN 导致分页和计数重复。
    KEY idx_material_tag_lookup (tag,material_id),
    -- 外键保证标签不能引用不存在的素材。
    CONSTRAINT fk_material_tag_material FOREIGN KEY (material_id) REFERENCES chat_material(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='持久素材与可编辑主题标签关联';

-- 旧关联表已合入 chat_tagged_memory，不再单独创建。

-- 用途：统一保存当前会话接收和发送上下文，每个 session 仅一行，不设置过期时间。
-- 顺序：已有 MySQL 部署先停止服务并备份，再执行本脚本及显式历史迁移工具。
-- 数据影响：仅新增表，不删除旧会话、轮次、出站记录，不覆盖已有统一上下文。
CREATE TABLE IF NOT EXISTS chat_session_context (
    session_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL COMMENT '会话唯一标识，同一会话并发更新同一行',
    generation_id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '仅显式清空时切换，隔离旧后台任务',
    messages_json MEDIUMTEXT NOT NULL COMMENT '按消息时间和稳定序号排列的双向上下文 JSON',
    memory_summary MEDIUMTEXT NOT NULL COMMENT '超出近期窗口后的历史摘要',
    memory_facts_json JSON NULL COMMENT '带主体来源和引用的结构化摘要，空值表示尚未生成',
    legacy_memory_summary MEDIUMTEXT NULL COMMENT '升级前纯文本摘要原样归档，不默认注入模型',
    memory_revision BIGINT NOT NULL DEFAULT 0 COMMENT '摘要快照序号，仅防止旧压缩结果覆盖新摘要',
    deliveries_json MEDIUMTEXT NOT NULL COMMENT '请求去重、发送占位及回执 JSON，不另存发送正文表',
    create_time DATETIME(6) NOT NULL COMMENT '会话创建时间，UTC',
    update_time DATETIME(6) NOT NULL COMMENT '最近合并时间，UTC，不用于自动过期',
    -- 主键保证每个 session 仅一行，并为短事务合并提供行锁。
    PRIMARY KEY (session_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='无过期的统一会话上下文';

-- 记忆生命周期：当前事实的历史、来源、幂等和向量缓存；新库直接创建。
-- 记忆生命周期辅助表：各字段、唯一约束和外键见下方中文说明。
CREATE TABLE IF NOT EXISTS chat_memory_revision (
      id INT NOT NULL AUTO_INCREMENT COMMENT '历史版本标识',
      memory_id INT NOT NULL COMMENT '当前事实标识',
      revision INT NOT NULL COMMENT '不可变事实版本',
      operation VARCHAR(20) NOT NULL COMMENT '变更操作',
      snapshot_json JSON NOT NULL COMMENT '完整事实与来源快照',
      actor_type VARCHAR(20) NOT NULL COMMENT '管理员用户或迁移来源',
      reason VARCHAR(300) NOT NULL COMMENT '变更理由不含内部推理',
      created_at DATETIME NOT NULL COMMENT 'UTC版本提交时间',
      -- 主键稳定标识历史记录，联合唯一约束禁止覆盖同一事实版本。
      PRIMARY KEY (id),
      UNIQUE KEY uk_memory_revision (memory_id,revision),
      -- 外键防止历史版本脱离当前事实根记录。
      FOREIGN KEY (memory_id) REFERENCES chat_tagged_memory(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='不可变长期记忆版本';

CREATE TABLE IF NOT EXISTS chat_memory_evidence (
      id INT NOT NULL AUTO_INCREMENT COMMENT '来源证据标识',
      memory_id INT NOT NULL COMMENT '归属事实',
      first_revision INT NOT NULL COMMENT '首次纳入版本',
      source_kind VARCHAR(24) NOT NULL COMMENT '来源类别不由模型指定',
      source_ref VARCHAR(200) NULL COMMENT '可核对来源引用',
      source_actor_id VARCHAR(64) NULL COMMENT '匿名发言人',
      source_session_id VARCHAR(128) NULL COMMENT '来源会话权限边界',
      source_event_id VARCHAR(200) NULL COMMENT '真实来源事件引用',
      source_time DATETIME NULL COMMENT '来源UTC时间未知为空',
      recorded_at DATETIME NOT NULL COMMENT '证据记录UTC时间',
      excerpt VARCHAR(300) NOT NULL COMMENT '真实片段不是可执行指令',
      evidence_fingerprint CHAR(64) NOT NULL COMMENT '独立来源去重摘要',
      original_memory_id INT NULL COMMENT '合并前原始事实',
      -- 主键稳定标识证据，联合唯一约束防止同一来源反复佐证。
      PRIMARY KEY (id),
      UNIQUE KEY uk_memory_evidence (memory_id,evidence_fingerprint),
      -- 来源撤回按事件和会话定向定位，避免锁定无关事实证据。
      KEY idx_memory_evidence_event (source_event_id,source_session_id),
      -- 外键防止来源证据脱离当前事实根记录。
      FOREIGN KEY (memory_id) REFERENCES chat_tagged_memory(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='长期记忆的可核对来源';

CREATE TABLE IF NOT EXISTS chat_memory_operation (
      scope_id CHAR(64) NOT NULL COMMENT '写入权限作用域',
      request_id VARCHAR(128) NOT NULL COMMENT '原始请求标识',
      operation_id VARCHAR(128) NOT NULL COMMENT '幂等操作标识',
      arguments_digest CHAR(64) NOT NULL COMMENT '参数摘要防止复用身份篡改',
      operation_kind VARCHAR(20) NOT NULL COMMENT '写入或采用操作',
      result_json JSON NOT NULL COMMENT '仅状态和标识不复制正文',
      created_at DATETIME NOT NULL COMMENT '提交UTC时间',
      -- 复合主键在授权作用域内隔离请求与操作，防止重放写入。
      PRIMARY KEY (scope_id,request_id,operation_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='记忆写入与采用幂等回执';

CREATE TABLE IF NOT EXISTS chat_memory_embedding (
      memory_id INT NOT NULL COMMENT '事实标识',
      revision INT NOT NULL COMMENT '向量对应的事实版本',
      model_key VARCHAR(200) NOT NULL COMMENT '向量模型及配置版本',
      vector_json JSON NOT NULL COMMENT '有限数值向量数组',
      created_at DATETIME NOT NULL COMMENT '计算UTC时间',
      -- 复合主键禁止跨事实版本或模型版本复用向量。
      PRIMARY KEY (memory_id,revision,model_key),
      -- 外键保证向量仅附着于已有事实，遗忘仍由状态和版本筛选。
      FOREIGN KEY (memory_id) REFERENCES chat_tagged_memory(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='现有数据库内的记忆向量缓存';

-- B 站单账号与外部操作记录；0.5.7 既有 MySQL 库由管理员显式执行一次。
-- 在完整初始化之后执行；仅新增表，不修改或删除业务数据，普通启动不执行升级。
CREATE TABLE IF NOT EXISTS agent_bot_bilibili_account (
    id TINYINT NOT NULL PRIMARY KEY COMMENT '固定单账号标识，只允许值 1',
    uid VARCHAR(20) NOT NULL COMMENT 'B 站账号 UID',
    name VARCHAR(200) NOT NULL COMMENT '最近验证的账号昵称',
    cookies TEXT NOT NULL COMMENT '仅服务端可读的登录凭据，不回填 API',
    updated_at BIGINT NOT NULL COMMENT '最近确认登录的 UTC 秒',
    CONSTRAINT chk_bilibili_account_singleton CHECK (id = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='B 站单账号登录状态';
CREATE TABLE IF NOT EXISTS agent_bot_bilibili_operation (
    operation_key CHAR(64) NOT NULL PRIMARY KEY COMMENT '账号、归属、请求和参数的 SHA256 唯一占位',
    uid VARCHAR(20) NOT NULL COMMENT '操作绑定账号，清除登录时保留，不设级联外键',
    tool VARCHAR(64) NOT NULL COMMENT '产生外部副作用的工具名',
    status VARCHAR(16) NOT NULL COMMENT 'pending 代表进行中或结果未知，completed 代表已保存结果',
    result TEXT NULL COMMENT '成功后的安全 JSON 结果，不含凭据',
    created_at BIGINT NOT NULL COMMENT '操作占位的 UTC 秒',
    CONSTRAINT chk_bilibili_operation_status CHECK (status IN ('pending', 'completed'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='B 站外部操作幂等记录，主键仲裁并发，不自动重试未知结果';

-- 启动版本管理增量：用于已有 0.5.5–0.5.7 完整结构，先执行本脚本再运行已登记业务增量。
-- 仅新增版本与迁移流水表，不写版本号、不修改业务数据；运行器核验完成后登记版本。
CREATE TABLE IF NOT EXISTS agent_bot_schema_version (
    id TINYINT NOT NULL PRIMARY KEY COMMENT '单例版本记录，防止并行版本分叉',
    version VARCHAR(32) NOT NULL COMMENT '完成核验的程序稳定版本号',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '版本推进时间',
    CONSTRAINT chk_schema_version_singleton CHECK (id = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='启动自动升级的数据库版本';
CREATE TABLE IF NOT EXISTS agent_bot_schema_migration (
    migration_id VARCHAR(128) NOT NULL PRIMARY KEY COMMENT '发布清单迁移唯一标识，主键防止重复登记',
    target_version VARCHAR(32) NOT NULL COMMENT '该迁移所属程序版本',
    checksum CHAR(64) NOT NULL COMMENT '当前后端 SQL 的 SHA256，禁止静默篡改历史迁移',
    status VARCHAR(16) NOT NULL COMMENT 'applying 执行中、applied 完成、failed 失败',
    applied_at DATETIME NULL COMMENT '迁移完成时间，未完成时为空',
    last_error VARCHAR(64) NULL COMMENT '仅保存固定错误码，不记录业务值或原始数据库异常',
    CONSTRAINT chk_schema_migration_status CHECK (status IN ('applying', 'applied', 'failed'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='启动迁移流水，不设业务外键以保留失败审计';

-- 表情包图库增量，适用完整 0.5.5 至 0.5.10 业务库；按登记清单在既有增量后执行。
-- 仅新增独立表与索引，不改写已有配置、凭据或业务数据；可重入，不自动启用插件。
CREATE TABLE IF NOT EXISTS agent_bot_sticker (
    id CHAR(32) NOT NULL COMMENT '服务端随机32位十六进制标识，不接受客户端路径',
    name VARCHAR(255) NOT NULL COMMENT '管理员命名',
    tags_json TEXT NOT NULL COMMENT '管理员维护的有序标签JSON数组',
    description TEXT NOT NULL COMMENT '识图或管理员维护的画面说明',
    emotion VARCHAR(100) NOT NULL COMMENT '主要情绪',
    usage_hint TEXT NOT NULL COMMENT '适用聊天场景',
    enabled BOOLEAN NOT NULL DEFAULT TRUE COMMENT '停用后禁止机器人发送',
    revision BIGINT NOT NULL DEFAULT 1 COMMENT '乐观更新修订',
    mime_type VARCHAR(32) NOT NULL COMMENT '已验证原件容器的媒体类型',
    size_bytes BIGINT NOT NULL COMMENT '原件字节数',
    sha256 CHAR(64) NOT NULL COMMENT '原件完整性摘要，墓碑仍保留',
    active_sha256 CHAR(64) NULL COMMENT '只对活跃原件去重，删除设NULL允许再次上传',
    width INT NOT NULL COMMENT '原件画布宽',
    height INT NOT NULL COMMENT '原件画布高',
    frame_count INT NOT NULL COMMENT '已解码验证帧数',
    animated BOOLEAN NOT NULL COMMENT '原件是否为GIF动画',
    analysis_status VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT '识图生命周期pending、ready或failed',
    analysis_error VARCHAR(100) NULL COMMENT '仅保存安全错误码，不保存供应商正文',
    storage_key CHAR(32) NOT NULL COMMENT '原件服务端私有文件键',
    preview_key CHAR(32) NOT NULL COMMENT '静态PNG预览服务端私有文件键',
    preview_size_bytes BIGINT NOT NULL COMMENT '预览完整性大小',
    preview_sha256 CHAR(64) NOT NULL COMMENT '预览完整性摘要',
    deleted BOOLEAN NOT NULL DEFAULT FALSE COMMENT '删除墓碑先阻止访问再清理文件',
    created_at DATETIME(6) NOT NULL COMMENT 'UTC创建时间',
    updated_at DATETIME(6) NOT NULL COMMENT 'UTC最后修订时间',
    PRIMARY KEY (id),
    UNIQUE KEY uq_sticker_active_sha256 (active_sha256) COMMENT '活跃原件去重，墓碑允许空值',
    UNIQUE KEY uq_sticker_storage_key (storage_key) COMMENT '原件文件键只归属一条记录',
    UNIQUE KEY uq_sticker_preview_key (preview_key) COMMENT '预览文件键只归属一条记录',
    KEY ix_sticker_active_created (deleted, enabled, created_at, id) COMMENT '按活跃状态与创建时间分页'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='独立表情包图库，文件位置仅服务端可见';

-- 长期互动演化增量：完整旧业务库先完成已登记历史迁移，再由启动升级器执行本脚本。
-- 仅新增演化表，不改写原角色、凭据、记忆或历史；新库初始化包含相同结构。
-- CREATE TABLE IF NOT EXISTS 支持 MySQL 隐式提交后的重入；最终核验失败即停止启动。
CREATE TABLE IF NOT EXISTS agent_bot_evolution_config (
    agent_id BIGINT UNSIGNED PRIMARY KEY COMMENT '真实 Agent；随删除级联清理',
    enabled TINYINT NOT NULL DEFAULT 1 COMMENT '暂停同时停止学习和应用',
    revision BIGINT NOT NULL DEFAULT 1 COMMENT '管理配置乐观锁版本',
    -- 外键防止孤立配置或 Agent 删除后身份复用。
    FOREIGN KEY (agent_id) REFERENCES agent_bot_agent(id) ON DELETE CASCADE,
    CHECK (enabled IN (0,1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Agent 演化启停配置；无行时默认启用';

CREATE TABLE IF NOT EXISTS agent_bot_evolution_profile (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '稳定分页和审计标识',
    agent_id BIGINT UNSIGNED NOT NULL COMMENT '所属 Agent',
    subject_key CHAR(64) NOT NULL COMMENT '会话密钥命名空间中的匿名用户摘要',
    traits TEXT NOT NULL COMMENT '四个固定表达维度；档位负三至三',
    revision BIGINT NOT NULL DEFAULT 1 COMMENT '实际行为变化或重置时递增',
    -- 唯一键隔离 Agent 与用户，禁止跨用户共享画像。
    UNIQUE KEY uq_evolution_subject (agent_id,subject_key),
    -- Agent 删除时释放对应画像，不能保留孤立人格。
    FOREIGN KEY (agent_id) REFERENCES agent_bot_agent(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='按 Agent 和匿名用户隔离的演化状态';

CREATE TABLE IF NOT EXISTS agent_bot_evolution_event (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '因果引用及分页主键',
    profile_id BIGINT UNSIGNED NOT NULL COMMENT '所属画像',
    source_key VARCHAR(64) NOT NULL COMMENT '真实来源匿名摘要；内部操作用随机标识',
    kind VARCHAR(16) NOT NULL COMMENT 'feedback 反馈、change 变化、reset 重置',
    dimension VARCHAR(16) NOT NULL COMMENT '固定维度；重置为空串',
    direction TINYINT NOT NULL COMMENT '反馈方向负一、零或一',
    excerpt VARCHAR(300) NOT NULL COMMENT '完整明确反馈句或固定原因',
    observed_at DATETIME(6) NOT NULL COMMENT '真实来源 UTC 时间',
    consumed TINYINT NOT NULL DEFAULT 0 COMMENT '证据已消费后不能再次推动变化',
    before_value TINYINT NULL COMMENT '变化前档位',
    after_value TINYINT NULL COMMENT '变化后档位',
    evidence_ids TEXT NOT NULL COMMENT '参与本次变化的真实反馈主键数组',
    -- 去重索引保证请求重放不能虚增独立证据。
    UNIQUE KEY uq_evolution_source (profile_id,source_key,dimension,kind),
    -- 按画像和维度检索未消费的近期反馈。
    KEY idx_evolution_pending (profile_id,dimension,kind,consumed,observed_at),
    -- 画像删除时一并删除其证据，避免匿名主体跨生命周期复用。
    FOREIGN KEY (profile_id) REFERENCES agent_bot_evolution_profile(id) ON DELETE CASCADE,
    CHECK (kind IN ('feedback','change','reset')),
    CHECK (direction BETWEEN -1 AND 1),
    CHECK (consumed IN (0,1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='演化原始反馈、因果变化和重置事件';

-- 可选角色关联的完整新库结构；旧三表保留兼容基线，应用使用以下范围、状态和记录表。
-- 通用范围由首次启动登记的独立增量创建，既有数据复制也仅在独立增量中执行。
CREATE TABLE IF NOT EXISTS agent_bot_evolution_scope (
    scope_key VARCHAR(80) COLLATE utf8mb4_bin PRIMARY KEY COMMENT 'global 通用；agent:ID 为可选角色范围',
    agent_id BIGINT UNSIGNED NULL UNIQUE COMMENT '可选角色关联；删除只解除关联',
    enabled TINYINT NOT NULL DEFAULT 1 COMMENT '本范围学习和应用开关',
    revision BIGINT NOT NULL DEFAULT 0 COMMENT '配置乐观锁；零为未显式保存',
    -- 可选外键保留角色删除后的演化状态与历史。
    FOREIGN KEY (agent_id) REFERENCES agent_bot_agent(id) ON DELETE SET NULL,
    CHECK (enabled IN (0,1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='不依赖角色的通用或可选关联演化范围';
CREATE TABLE IF NOT EXISTS agent_bot_evolution_state (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '保留旧画像 ID',
    scope_key VARCHAR(80) COLLATE utf8mb4_bin NOT NULL COMMENT '所属演化范围',
    subject_key CHAR(64) NOT NULL COMMENT '真实用户匿名命名空间',
    traits TEXT NOT NULL COMMENT '固定表达维度的有界档位',
    revision BIGINT NOT NULL DEFAULT 1 COMMENT '行为变化和重置版本',
    -- 唯一索引防止同范围同用户产生多个状态。
    UNIQUE KEY uq_evolution_state_subject (scope_key,subject_key),
    -- 范围为状态的持久身份，不随角色删除。
    FOREIGN KEY (scope_key) REFERENCES agent_bot_evolution_scope(scope_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='通用或可选角色范围内的用户演化状态';
CREATE TABLE IF NOT EXISTS agent_bot_evolution_record (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '保留旧事件主键和因果引用',
    profile_id BIGINT UNSIGNED NOT NULL COMMENT '所属演化状态',
    source_key VARCHAR(64) NOT NULL COMMENT '真实来源匿名摘要',
    kind VARCHAR(16) NOT NULL COMMENT 'feedback 反馈、change 变化、reset 重置',
    dimension VARCHAR(16) NOT NULL COMMENT '固定维度；重置为空',
    direction TINYINT NOT NULL COMMENT '有界方向',
    excerpt VARCHAR(300) NOT NULL COMMENT '真实原话或固定原因',
    observed_at DATETIME(6) NOT NULL COMMENT '真实 UTC 来源时间或变化时间',
    consumed TINYINT NOT NULL DEFAULT 0 COMMENT '防止旧证据重复生效',
    before_value TINYINT NULL COMMENT '变化前档位',
    after_value TINYINT NULL COMMENT '变化后档位',
    evidence_ids TEXT NOT NULL COMMENT '真实反馈主键数组，迁移不重编号',
    -- 同状态同来源同维度去重，重放不能虚增证据。
    UNIQUE KEY uq_evolution_record_source (profile_id,source_key,dimension,kind),
    -- 状态机按维度、消费标记和来源时间选取证据。
    KEY idx_evolution_record_pending (profile_id,dimension,kind,consumed,observed_at),
    -- 管理列表按类型倒序分页。
    KEY idx_evolution_record_kind (kind,id),
    -- 外键保留状态与因果记录的归属，应用不提供删除审计入口。
    FOREIGN KEY (profile_id) REFERENCES agent_bot_evolution_state(id),
    CHECK (kind IN ('feedback','change','reset')),
    CHECK (direction BETWEEN -1 AND 1),
    CHECK (consumed IN (0,1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='跨演化范围可查询的因果记录';
