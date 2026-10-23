"""在调用模型前提供配置允许的联网能力并选择已授权本地工具。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from agent_bot.models import ChatToolName

_NARAKA_REQUEST = re.compile(r"永劫|战绩|段位|角色\s*ID|玩家|naraka", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ChatRoute:
    """经过本地选择和授权交集后的单次聊天能力。"""

    chat_tools: tuple[ChatToolName, ...]
    web_search: bool


class ChatRouteSelector:
    """保留已授权的本地工具，联网能力仅由配置开关控制。"""

    def select(
        self,
        prompt: str,
        *,
        available_tools: Sequence[ChatToolName] = (),
        web_search_enabled: bool = True,
    ) -> ChatRoute:
        """联网开关启用时始终提供搜索能力，是否调用由模型决定。"""

        text = unicodedata.normalize("NFKC", prompt).strip()[:20_000]
        selected: set[ChatToolName] = set()
        if _NARAKA_REQUEST.search(text):
            selected.update(("naraka_query", "naraka_search"))
        selected.update(
            (
                "memory",
                "job",
                "soloncode",
                "mention_member",
                "local_workflow_list",
                "local_workflow_generate_image", "local_workflow_generate_video",
                "local_workflow_tasks",
                "local_workflow_cancel",
                "tts_generate",
            )
        )
        tools = tuple(tool for tool in dict.fromkeys(available_tools) if tool in selected)
        return ChatRoute(chat_tools=tools, web_search=web_search_enabled)
