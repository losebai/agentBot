"""聊天消息入口的路由规则。"""

from collections.abc import Collection

_REMOVED_CHAT_COMMANDS = frozenset({"chat", "聊天"})


def is_removed_chat_command(message: str, command_starts: Collection[str]) -> bool:
    """判断消息是否使用已移除的聊天命令。

    `/chatgpt` 之类的普通文本不能被前缀误伤，因此命令名称之后只允许结束或空白。
    """

    normalized = message.lstrip()
    for command_start in command_starts:
        if not command_start or not normalized.startswith(command_start):
            continue
        command_text = normalized[len(command_start) :]
        for command in _REMOVED_CHAT_COMMANDS:
            if command_text == command or (
                command_text.startswith(command)
                and command_text[len(command) : len(command) + 1].isspace()
            ):
                return True
    return False
