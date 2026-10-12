"""使用稳定错误码隔离媒体服务的底层响应和本地路径。"""

from agent_bot.ai.errors import AiError


class MediaError(AiError):
    """媒体失败沿用统一管理错误结构，禁止携带原始供应商内容。"""
