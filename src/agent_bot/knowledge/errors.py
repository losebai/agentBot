"""知识管理的固定错误码，避免泄露数据库和身份参数。"""

from agent_bot.ai.errors import AiError


class KnowledgeError(AiError):
    """由统一管理错误处理器输出的知识与记忆业务错误。"""
