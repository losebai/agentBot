"""聊天领域异常。"""


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
