"""聊天领域异常。"""

from agent_bot.ai.chat_errors import ChatAiError


class ChatInputError(ValueError):
    """聊天输入错误基类。"""


class EmptyPromptError(ChatInputError):
    """用户没有提供文本。"""


class PromptTooLongError(ChatInputError):
    """用户输入超过配置限制。"""


class ChatRateLimitedError(RuntimeError):
    """同一会话请求过于频繁。"""

    def __init__(self, retry_after: float) -> None:
        """记录调用方再次请求前需要等待的秒数。"""

        super().__init__("chat request rate limited")
        self.retry_after = retry_after


class ChatBusyError(RuntimeError):
    """当前会话或全局 AI 并发已经占满。"""


def is_chat_busy_error(error: Exception) -> bool:
    """本地额度满与上游429均是立即拒绝，不能当作暂时故障退避重试。"""
    return isinstance(error, ChatBusyError) or (
        isinstance(error, ChatAiError) and error.status_code == 429
    )
