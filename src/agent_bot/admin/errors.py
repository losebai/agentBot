"""统一公开错误文案，不暴露凭据、请求体或供应商原始响应。"""

from agent_bot.ai.errors import AiError


def classify_api_error(error: AiError) -> tuple[int, str, str]:
    """稳定中文错误文案不回显上游响应、文件路径或凭据。"""
    messages = {
        "KNOWLEDGE_NOT_INITIALIZED": "知识库表尚未初始化，请执行对应知识库增量 SQL",
        "KNOWLEDGE_UNAVAILABLE": "知识库数据库暂时不可用，请检查连接",
        "KNOWLEDGE_NOT_FOUND": "知识条目不存在或已删除",
        "KNOWLEDGE_CONFLICT": "知识条目保存冲突，请重新新增",
        "KNOWLEDGE_INVALID_INPUT": "知识内容或标签不符合要求",
        "KNOWLEDGE_INVALID_PAGE": "分页条件已改变或游标无效，请重新查询",
        "MEMORY_NOT_FOUND": "记忆不存在或已删除",
        "MEMORY_SCOPE_NOT_FOUND": "记忆作用域不存在或类型不匹配",
        "MEMORY_CONFLICT": "记忆与已有事实冲突，请核对当前内容后修订或整理",
        "MEMORY_REVISION_CONFLICT": "记忆版本或操作目标已变化，请重新读取并核对后操作",
        "MEMORY_INVALID_INPUT": "记忆内容、有效期或操作目标不符合要求",
        "MEMORY_CAPACITY": "此作用域记忆已满，请先整理重复或失效事实",
        "MEMORY_UNAVAILABLE": "记忆数据库不可用，请检查连接与初始化结构",
        "WORKFLOW_NOT_READY": "工作流存储尚未就绪，请检查服务状态",
        "WORKFLOW_TOO_LARGE": "工作流文件超过 1 MiB 大小限制",
        "WORKFLOW_DIRECTORY_MISSING": "未配置工作流同步目录，请在启动配置中设置后重启服务",
        "WORKFLOW_DIRECTORY_UNAVAILABLE": "工作流目录不可读取，请检查路径和服务文件权限",
        "AI_CAPABILITY_UNSUPPORTED": "当前请求包含尚未实现的能力，请移除视频或相关工具",
        "MEDIA_NOT_INITIALIZED": "媒体数据表尚未初始化，请导入 agent_bot_init.sql 后重启服务",
        "MATERIAL_NOT_INITIALIZED": "素材库尚未初始化，请检查媒体目录与对应增量 SQL",
        "MATERIAL_NOT_FOUND": "素材不存在或不可访问",
        "MATERIAL_INVALID_PAGE": "素材筛选条件已改变或游标无效，请重新查询",
        "MEDIA_STORAGE_UNAVAILABLE": "媒体目录不可用，请修复后重启服务",
        "MEDIA_NOT_READY": "媒体资源尚未就绪，请检查状态并重启服务",
        "MEDIA_EXPIRED": "附件已过期",
        "MEDIA_NOT_FOUND": "附件不存在或不可访问",
        "MEDIA_TOO_LARGE": "媒体超过大小限制",
        "MEDIA_TOO_MANY_IMAGES": "图片数量超过限制",
        "MEDIA_INVALID_IMAGE": "图片格式无效、截断或与声明不符",
        "MEDIA_ANIMATION_UNSUPPORTED": "暂不支持动画图片，请使用静态图片",
        "MEDIA_BUSY": "媒体并发已满，请稍后再试",
        "MEDIA_SAVE_FAILED": "上游已调用，但媒体保存失败，请检查存储状态",
        "MEDIA_STORAGE_FULL": "媒体存储配额已满",
        "MEDIA_VISION_UNSUPPORTED": (
            "所选模型尚未启用图片理解能力，请在“供应商与模型”中开启后重试"
        ),
        "MEDIA_INPUT_ALREADY_USED": "此上传已用于另一次执行，请重新上传",
        "MEDIA_CANCELLED": "媒体请求已取消",
        "MEDIA_TIMEOUT": "媒体生成已超时",
        "MEDIA_UPLOAD_TIMEOUT": "上传超时，请重新选择文件",
        "MEDIA_INVALID_RESPONSE": "上游未返回有效的媒体结果",
        "AI_NOT_INITIALIZED": (
            "AI 数据表或字段缺失；既有库请执行对应增量 SQL，"
            "新库使用 agent_bot_init.sql"
        ),
        "AI_CONFIG_CONFLICT": "配置已被修改或仍在启用，请重新加载后操作",
        "AI_MODEL_ALIAS_CONFLICT": "模型别名已被其他记录使用，请设置不同别名后导入",
        "AI_CONFIG_NOT_FOUND": "所选供应商或模型不存在或已删除，请刷新列表后重新选择",
        "AI_CONFIG_IN_USE": "配置仍被模型或 Agent 引用，请先解除关联",
        "AI_CREDENTIAL_MISSING": "供应商密钥尚未设置，请在供应商管理中重新录入",
        "AGENT_CREDENTIAL_MISSING": "MCP 访问令牌尚未设置，请在 MCP 服务中重新录入",
        "AGENT_MCP_UNAVAILABLE": "MCP 服务连接失败或超时，请检查地址及服务状态",
        "AGENT_MCP_AUTH_REJECTED": "MCP 服务拒绝鉴权，请检查此服务的访问令牌",
        "AGENT_MCP_PROTOCOL_ERROR": "MCP 协议交互失败，请检查连接协议及服务端日志",
        "AGENT_MCP_RESPONSE_TOO_LARGE": "MCP 响应超过允许的大小限制",
        "AGENT_MCP_TOOL_LIMIT": "MCP 工具数量超过上限，单次最多支持 32 个工具",
        "AGENT_MCP_DISCOVERY_LIMIT": "MCP 工具目录过大或分页异常，管理目录最多支持 256 项",
        "AI_MODEL_LIST_INVALID": "上游模型列表格式无效或超过数量限制",
        "AI_UPSTREAM_AUTH_REJECTED": "上游拒绝鉴权，请检查此供应商的 Key",
        "AI_UPSTREAM_REJECTED": "上游拒绝请求，请检查协议、地址和模型列表接口支持情况",
        "AI_UPSTREAM_UNAVAILABLE": "上游服务暂时不可用，请检查地址及连接",
        "AI_RATE_LIMITED": "上游限制请求频率，请稍后再试",
        "AI_OPTION_UNSUPPORTED": "当前协议不支持已配置的采样参数",
        "AI_TIMEOUT": "模型请求已超时",
        "AGENT_NOT_INITIALIZED": (
            "Agent 数据表或字段缺失；既有库请执行对应增量 SQL，"
            "新库使用 agent_bot_init.sql"
        ),
        "AGENT_CONFIG_CONFLICT": "此配置已有更新，请保留草稿并重新加载后合并修改",
        "AGENT_CONFIG_IN_USE": "配置仍被 Agent 引用，请先解除关联",
        "AGENT_CONFIG_NOT_FOUND": (
            "所选 Agent、Skill 或 MCP 配置不存在或已删除，请刷新列表后重新选择"
        ),
        "AGENT_MODEL_UNAVAILABLE": "所选模型或供应商未启用，请在“供应商与模型”中启用后重试",
        "AGENT_VISION_MODEL_REQUIRED": "本次请求包含图片，请先为 Agent 配置图片理解模型",
        "AGENT_CONFIG_DISABLED": "Agent、Skill 或 MCP 配置已停用",
        "AGENT_TOOL_UNAUTHORIZED": "当前入口或 Agent 未授权这些工具",
        "AGENT_TOOL_SESSION_EXPIRED": "QQ 工具会话已过期或撤销，请重新发起请求",
        "AGENT_TOOL_UNAVAILABLE": "预设工具不可用，请检查工具名称及 MCP 白名单",
        "AGENT_SEARCH_UNAVAILABLE": "请配置已启用的 search 分类 MCP 服务",
        "AGENT_SKILL_UNAVAILABLE": "关联 Skill 已停用或缺少已授权的工具依赖",
        "AGENT_BUSY": "Agent 并发已满，请稍后再试",
        "AGENT_CAPABILITY_NOT_MIGRATED": "此 Agent 要求尚未实现的能力，请移除相关工具",
    }
    return (
        error.status_code,
        error.code,
        messages.get(error.code, "AI 操作未完成，请检查配置或服务状态"),
    )
