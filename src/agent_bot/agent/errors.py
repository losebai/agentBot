"""Agent 稳定错误码与 QQ 超时后可投递正文，异常字符串始终不包含模型内容。"""

from agent_bot.ai.errors import AiError

# 仅这些中断允许 QQ 发送已有正文；真正权限拒绝和取消不能沿用草稿。
PARTIAL_REPLY_ERRORS = frozenset({"AGENT_TIMEOUT", "AI_TIMEOUT", "AGENT_TOOL_SESSION_EXPIRED"})


class AgentError(AiError):
    """错误正文只交给 QQ 投递适配器，不进入日志、管理错误响应或成功元数据。"""

    def __init__(
        self, code: str, *, status_code: int = 502, partial_content: str = "",
        completed_tool_calls: int = 0,
    ) -> None:
        """保存可投递正文和成功工具回执数，失败说明不携带原始工具负载。"""
        super().__init__(code, status_code=status_code)
        self.partial_content = partial_content
        self.completed_tool_calls = completed_tool_calls


class ToolResultRejectedError(AgentError):
    """工具响应已收到但本地拒绝其结果，不能据此推断远端操作未执行。"""
