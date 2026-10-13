"""Safe agent failures; upstream content is never an error message."""

from agent_bot.ai.errors import AiError


class AgentError(AiError):
    """An execution or configuration error with a stable public code."""
