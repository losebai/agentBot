"""Agent 稳定错误码与 QQ 超时后可投递正文，异常字符串始终不包含模型内容。"""

from agent_bot.ai.errors import AiError

# 仅这些中断允许 QQ 发送已有正文；真正权限拒绝和取消不能沿用草稿。
PARTIAL_REPLY_ERRORS = frozenset({"AGENT_TIMEOUT", "AI_TIMEOUT", "AGENT_TOOL_SESSION_EXPIRED"})


class AgentError(AiError):
    """错误正文只交给 QQ 投递适配器，不进入日志、管理错误响应或成功元数据。"""

    def __init__(self, code: str, *, status_code: int = 502, partial_content: str = "") -> None:
        """保持原错误码接口，独立保存经过调用边界选择的已生成正文。"""
        super().__init__(code, status_code=status_code)
        self.partial_content = partial_content
