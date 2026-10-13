"""openai-chat 的兼容入口，模型请求统一使用 LangChain。"""

from agent_bot.ai.providers.langchain import OPTIONS, LangChainProvider


class AgentChatProvider(LangChainProvider):
    """保留现有导入与传输注入接口，移除独立协议解析。"""

    options = OPTIONS["openai-chat"]

    def __init__(self, transport):
        """当前运行时继续拥有传输，退出时统一关闭。"""
        super().__init__("openai-chat", provider_transport=transport)
