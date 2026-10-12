-- 用途：统一初始化 agentBot 0.4.0 的全部 25 张业务表，供新独立数据库使用。
-- 执行顺序：管理员先创建并选中数据库，再执行本文件，最后用 settings import-env 导入配置。
-- 数据影响：仅创建不存在的表，不插入配置、不覆盖模型、不清空或迁移现有数据。
-- 本文件不升级既有表；旧库按 sql/README.md 选择增量脚本，不能批量执行 legacy 目录。
-- 建表按外键依赖排序；保留现有表名、字段类型、默认值、可空性、索引和删除约束。
-- 服务启动不会自动执行本文件；已有十张自有表补注释时使用独立的注释增量脚本。


-- 运行配置与群成员目录

CREATE TABLE IF NOT EXISTS `dispatch_chat_setting` (
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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='dispatch-chat 配置键值表';

CREATE TABLE IF NOT EXISTS `dispatch_chat_setting_revision` (
    `id`           BIGINT   NOT NULL COMMENT '固定为 1，保证单行',
    `revision`     BIGINT   NOT NULL COMMENT '当前配置快照版本号，从 1 起',
    `update_time`  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    -- 主键定义记录身份，防止同一业务记录重复创建。
    PRIMARY KEY (`id`),
    -- 固定单行标识，保证配置版本号只维护一个快照。
    CONSTRAINT `chk_dispatch_chat_setting_revision_singleton` CHECK (`id` = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='dispatch-chat 配置版本号（单行）';

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
    -- 主键定义记录身份，防止同一业务记录重复创建。
    PRIMARY KEY (`bot_id`, `group_id`, `user_id`),
    -- 联合索引支持对应字段过滤和有序读取，避免全表扫描。
    KEY `idx_dispatch_chat_group_member_user` (`bot_id`, `user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='NapCat OneBot QQ群成员目录';

-- 聊天记忆、幂等回执与群上下文

CREATE TABLE IF NOT EXISTS `dispatch_chat_memory_session` (
        `session_id` VARCHAR(128) NOT NULL COMMENT 'HMAC 会话标识',
        `generation_id` CHAR(32) NOT NULL COMMENT '会话代际标识，重建时重新生成',
        `memory_summary` MEDIUMTEXT NOT NULL COMMENT '压缩后的会话摘要',
        `memory_revision` BIGINT NOT NULL DEFAULT 0 COMMENT '摘要乐观锁版本',
        `compacted_through_turn_id` BIGINT NOT NULL DEFAULT 0 COMMENT '已压缩最大轮次 ID',
        `expires_at` DATETIME(6) NOT NULL COMMENT '滑动过期时间',
        `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
        `update_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
            ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间',
        -- 主键定义记录身份，防止同一业务记录重复创建。
        PRIMARY KEY (`session_id`),
        -- 联合索引支持对应字段过滤和有序读取，避免全表扫描。
        KEY `idx_dispatch_chat_memory_session_expires` (`expires_at`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='dispatch-chat 持久会话记忆';

CREATE TABLE IF NOT EXISTS `dispatch_chat_memory_turn` (
        `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '轮次主键',
        `session_id` VARCHAR(128) NOT NULL COMMENT 'HMAC 会话标识',
        `request_id` VARCHAR(128) COLLATE utf8mb4_bin NULL COMMENT '幂等请求标识，旧轮次为空',
        `user_content` MEDIUMTEXT NOT NULL COMMENT '用户历史文本',
        `assistant_content` MEDIUMTEXT NOT NULL COMMENT '已投递助手内容',
        `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
        -- 主键定义记录身份，防止同一业务记录重复创建。
        PRIMARY KEY (`id`),
        -- 联合索引支持对应字段过滤和有序读取，避免全表扫描。
        KEY `idx_dispatch_chat_memory_turn_session_id` (`session_id`, `id`),
        -- 唯一索引约束业务标识，支持并发写入时的重复检查。
        UNIQUE KEY `uk_dispatch_chat_memory_turn_request` (`session_id`, `request_id`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='dispatch-chat 最近会话轮次';

CREATE TABLE IF NOT EXISTS `dispatch_chat_memory_receipt` (
        `session_id` VARCHAR(128) NOT NULL COMMENT 'HMAC 会话标识',
        `request_id` VARCHAR(128) COLLATE utf8mb4_bin NOT NULL COMMENT '幂等请求标识',
        `expires_at` DATETIME(6) NOT NULL COMMENT '独立重试去重截止时间',
        -- 主键定义记录身份，防止同一业务记录重复创建。
        PRIMARY KEY (`session_id`, `request_id`),
        -- 联合索引支持对应字段过滤和有序读取，避免全表扫描。
        KEY `idx_dispatch_chat_memory_receipt_expires` (`expires_at`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='dispatch-chat 已提交请求回执';

CREATE TABLE IF NOT EXISTS `dispatch_chat_group_context` (
        `scope_id` VARCHAR(128) NOT NULL COMMENT 'HMAC Bot/群作用域',
        `messages_json` MEDIUMTEXT NOT NULL COMMENT '强类型最近群文本 JSON',
        `expires_at` DATETIME(6) NOT NULL COMMENT '滑动过期时间',
        `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
        `update_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
            ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间',
        -- 主键定义记录身份，防止同一业务记录重复创建。
        PRIMARY KEY (`scope_id`),
        -- 联合索引支持对应字段过滤和有序读取，避免全表扫描。
        KEY `idx_dispatch_chat_group_context_expires` (`expires_at`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='dispatch-chat 群聊最近文本';

-- 标签记忆：由应用事务维护关联，不添加外键

CREATE TABLE IF NOT EXISTS dispatch_chat_memory_scope (
    scope_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '匿名作用域键',
    scope VARCHAR(16) NOT NULL COMMENT '记忆作用域：全局、会话、用户或群组',
    -- 主键定义记录身份，防止同一业务记录重复创建。
    PRIMARY KEY (scope_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='标签记忆作用域与写锁';

CREATE TABLE IF NOT EXISTS dispatch_chat_tagged_memory (
    id INT NOT NULL AUTO_INCREMENT COMMENT '记忆自增主键',
    scope_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '关联作用域',
    memory_key VARCHAR(80) COLLATE utf8mb4_bin NOT NULL COMMENT '稳定事实键',
    content VARCHAR(1000) NOT NULL COMMENT '已确认的事实',
    is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '逻辑删除标记',
    create_time DATETIME NOT NULL COMMENT '创建时间',
    update_time DATETIME NOT NULL COMMENT '更新时间',
    -- 主键定义记录身份，防止同一业务记录重复创建。
    PRIMARY KEY (id),
    -- 唯一索引约束业务标识，支持并发写入时的重复检查。
    UNIQUE KEY uk_tagged_memory_scope_key (scope_id, memory_key),
    -- 联合索引支持对应字段过滤和有序读取，避免全表扫描。
    KEY idx_tagged_memory_scope_time (scope_id, is_deleted, update_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='LLM 自主保存的标签记忆';

CREATE TABLE IF NOT EXISTS dispatch_chat_memory_tag (
    memory_id INT NOT NULL COMMENT '关联记忆',
    tag VARCHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '规范化主题标签',
    -- 主键定义记录身份，防止同一业务记录重复创建。
    PRIMARY KEY (memory_id, tag),
    -- 联合索引支持对应字段过滤和有序读取，避免全表扫描。
    KEY idx_memory_tag_lookup (tag, memory_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='记忆与标签多对多关联';

-- 本地 ComfyUI 工作流：包含图生图字段和内容唯一索引

CREATE TABLE IF NOT EXISTS `dispatch_chat_workflow` (
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
    UNIQUE KEY `uk_dispatch_chat_workflow_key` (`workflow_key`),
    -- 唯一索引约束业务标识，支持并发写入时的重复检查。
    UNIQUE KEY `uk_dispatch_chat_workflow_content_sha256` (`content_sha256`),
    -- 联合索引支持对应字段过滤和有序读取，避免全表扫描。
    KEY `idx_dispatch_chat_workflow_enabled` (`enabled`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='本地 Comfy 工作流定义与执行绑定';

-- Java 模型兼容目录：管理员显式登记模型，初始为空

CREATE TABLE IF NOT EXISTS ai_model (
    model_id VARCHAR(255) NOT NULL COMMENT 'Java 服务中的实际模型标识',
    type VARCHAR(32) NOT NULL COMMENT '模型类型，聊天使用 chat',
    enabled TINYINT NOT NULL DEFAULT 0 COMMENT '是否启用，默认禁用',
    -- 联合主键允许同一标识登记不同类型，并支持模型可用性查询。
    -- 主键定义记录身份，防止同一业务记录重复创建。
    PRIMARY KEY (model_id, type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Java 模型可用性兼容目录';

-- Python AI：供应商先于模型创建

CREATE TABLE IF NOT EXISTS agent_bot_ai_provider (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  name VARCHAR(100) COLLATE utf8mb4_bin NOT NULL COMMENT '显示名称',
  protocol VARCHAR(32) NOT NULL COMMENT '供应商接口协议',
  base_url VARCHAR(2048) NOT NULL COMMENT '供应商接口根地址',
  credential_ref VARCHAR(128) NULL COMMENT '环境凭据引用，不保存密钥',
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
  alias VARCHAR(200) COLLATE utf8mb4_bin NOT NULL COMMENT '区分大小写的模型别名',
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
  -- 外键保证引用存在；级联规则沿用原定义，保持删除语义。
  CONSTRAINT fk_agent_bot_model_provider FOREIGN KEY (provider_id) REFERENCES agent_bot_ai_provider (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模型别名目录';

CREATE TABLE IF NOT EXISTS agent_bot_ai_session (
  execution_id CHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '执行标识，三十二位十六进制',
  session_id VARCHAR(256) COLLATE utf8mb4_bin NOT NULL COMMENT '区分大小写的会话标识',
  request_id VARCHAR(256) NOT NULL COMMENT '调用方请求标识',
  configured_model VARCHAR(200) NOT NULL COMMENT '请求指定的模型别名',
  model VARCHAR(255) NOT NULL COMMENT '实际使用的模型标识',
  status VARCHAR(16) NOT NULL COMMENT '执行状态：运行中、完成、失败或取消',
  duration_ms BIGINT UNSIGNED NOT NULL COMMENT '执行耗时，单位毫秒',
  usage JSON NOT NULL COMMENT '令牌用量统计',
  created_at DATETIME(6) NOT NULL COMMENT '创建时间，协调世界时',
  error_code VARCHAR(100) NULL COMMENT '稳定错误代码',
  prompt MEDIUMTEXT NOT NULL COMMENT '用户输入文本',
  content MEDIUMTEXT NOT NULL COMMENT '模型输出文本',
  -- 主键定义记录身份，防止同一业务记录重复创建。
  PRIMARY KEY (execution_id),
  -- 联合索引支持对应字段过滤和有序读取，避免全表扫描。
  KEY idx_session_created (session_id, created_at, execution_id),
  -- 联合索引支持对应字段过滤和有序读取，避免全表扫描。
  KEY idx_created (created_at, execution_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='普通文本调用记录';

-- Python Agent、Skill、MCP 与运行记录：被引用表先创建

CREATE TABLE IF NOT EXISTS agent_bot_agent_run (
  execution_id CHAR(32) COLLATE utf8mb4_bin NOT NULL PRIMARY KEY COMMENT '执行标识，三十二位十六进制',
  session_id VARCHAR(256) COLLATE utf8mb4_bin NOT NULL COMMENT '区分大小写的会话标识',
  request_id VARCHAR(256) NOT NULL COMMENT '调用方请求标识',
  entry VARCHAR(16) NOT NULL COMMENT '调用入口：管理端或机器人',
  model VARCHAR(255) NOT NULL COMMENT '实际使用的模型标识',
  backend VARCHAR(32) NOT NULL COMMENT '执行后端标识',
  status VARCHAR(16) NOT NULL COMMENT '执行状态：运行中、完成、失败或取消',
  duration_ms BIGINT UNSIGNED NOT NULL COMMENT '执行耗时，单位毫秒',
  usage JSON NOT NULL COMMENT '令牌用量统计',
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
  credential_ref VARCHAR(128) NULL COMMENT '环境凭据引用，不保存密钥',
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
  tools JSON NOT NULL COMMENT '允许使用的本地工具列表',
  max_steps INT NOT NULL COMMENT '最大工具调用轮次',
  options JSON NOT NULL COMMENT '模型生成参数',
  enabled TINYINT NOT NULL DEFAULT 0 COMMENT '是否启用：零禁用，一启用',
  revision BIGINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '乐观锁版本，每次修改递增',
  created_at DATETIME(6) NOT NULL COMMENT '创建时间，协调世界时',
  updated_at DATETIME(6) NOT NULL COMMENT '更新时间，协调世界时',
  -- 外键保证引用存在；级联规则沿用原定义，保持删除语义。
  CONSTRAINT fk_agent_model FOREIGN KEY(model_id) REFERENCES agent_bot_ai_model(id)
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
