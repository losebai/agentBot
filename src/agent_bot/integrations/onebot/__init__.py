"""OneBot/NapCat 外部适配。"""

from agent_bot.integrations.onebot.auto_speak import AutoSpeakBot
from agent_bot.integrations.onebot.auto_speak_trigger import GroupAutoSpeakTrigger
from agent_bot.integrations.onebot.context import EventContextService
from agent_bot.integrations.onebot.conversation import ConversationHandler
from agent_bot.integrations.onebot.drawing import DrawingCommandHandler
from agent_bot.integrations.onebot.naraka_command import NarakaCommandHandler
from agent_bot.integrations.onebot.queue_processor import ConversationQueueProcessor
from agent_bot.integrations.onebot.replies import OneBotReplyGateway, OneBotReplyService
from agent_bot.integrations.onebot.routing import MessageRouting
from agent_bot.integrations.onebot.summary import SummaryCommandHandler
from agent_bot.integrations.onebot.tooling import ChatToolSessionManager

__all__ = [
    "AutoSpeakBot",
    "ChatToolSessionManager",
    "ConversationHandler",
    "ConversationQueueProcessor",
    "DrawingCommandHandler",
    "EventContextService",
    "GroupAutoSpeakTrigger",
    "MessageRouting",
    "NarakaCommandHandler",
    "OneBotReplyGateway",
    "OneBotReplyService",
    "SummaryCommandHandler",
]
