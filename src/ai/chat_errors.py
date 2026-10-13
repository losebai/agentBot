"""聊天服务及 QQ 队列共用的错误分类。"""


class ChatAiError(RuntimeError):
    """聊天 AI 调用错误基类。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
    ) -> None:
        """保留可安全分类的状态，避免上层解析异常文本。"""

        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class ChatAiUnavailableError(ChatAiError):
    """网络、超时或上游服务不可用。"""


class ChatAiRejectedError(ChatAiError):
    """上游明确拒绝请求。"""


class ChatAiProtocolError(ChatAiError):
    """上游响应不符合约定。"""


class ChatAiImageError(ChatAiError):
    """QQ 图片无法安全下载或转换为多模态输入。"""


