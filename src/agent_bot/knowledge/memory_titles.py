"""从已知群成员目录匹配管理显示标题，不改变匿名作用域和 QQ 读取权限。"""

from collections.abc import Iterable, Mapping
from typing import Any

from agent_bot.services.tagged_memory import MemoryScope, memory_scope_owner
from agent_bot.state import build_session_scope, external_session_id


def scope_titles(
    owners: Mapping[str, MemoryScope], profiles: Iterable[Mapping[str, Any]], secret: str,
) -> dict[str, str]:
    """只计算已知真实身份的完整 HMAC；目录按最新优先，同一标题首次匹配后保留。"""
    titles: dict[str, str] = {}
    kinds = set(owners.values())

    def remember(scope: MemoryScope, subject: str, title: str, bot: str) -> None:
        """同时匹配类型和完整身份，其他机器人或旧密钥命名空间不能借用名称。"""
        key = memory_scope_owner(bot, scope, subject, secret)
        if owners.get(key) == scope and key not in titles:
            titles[key] = title if scope == "global" else f"{title} · 机器人 {bot}"

    for profile in profiles:
        bot, group, user = (str(profile[name]) for name in ("bot_id", "group_id", "user_id"))
        nickname = profile["nickname"] or profile["card"] or "QQ 用户"
        member_name = profile["card"] or profile["nickname"] or "QQ 用户"
        user_title = f"{nickname} ({user})"
        group_title = f"{profile['group_name'] or 'QQ群'} ({group})"
        if "global" in kinds:
            remember("global", "", f"机器人 {bot}", bot)
        if "user" in kinds:
            remember("user", user, user_title, bot)
        if "group" in kinds:
            remember("group", group, group_title, bot)
        if "session" in kinds:
            for group_id, shared, title in (
                (None, False, f"与{user_title}的私聊"),
                (group, False, f"{group_title} · {member_name} ({user}) 的个人会话"),
                (group, True, f"{group_title}的共享会话"),
            ):
                session = external_session_id(build_session_scope(
                    bot_id=bot, user_id=user, group_id=group_id, group_shared=shared,
                ), secret)
                remember("session", session, title, bot)
        if len(titles) == len(owners):
            break
    return titles
