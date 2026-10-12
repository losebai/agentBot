from agent_bot.message_routing import is_removed_chat_command


def test_removed_chat_commands_are_recognized_at_the_message_start() -> None:
    command_starts = {"/", "!"}

    assert is_removed_chat_command("/chat", command_starts)
    assert is_removed_chat_command("/chat 你好", command_starts)
    assert is_removed_chat_command("!聊天\t你好", command_starts)


def test_similar_text_remains_available_to_natural_chat() -> None:
    command_starts = {"/"}

    assert not is_removed_chat_command("/chatgpt 是什么", command_starts)
    assert not is_removed_chat_command("聊聊 /chat", command_starts)
    assert not is_removed_chat_command("普通聊天", command_starts)
