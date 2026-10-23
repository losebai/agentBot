"""验证模型请求的字符和消息上限，不裁切当前输入或受信任系统内容。"""

import pytest

from agent_bot.agent.contracts import AgentRequest
from agent_bot.ai.context import GUARDRAIL, format_qq_context
from agent_bot.ai.contracts import TextRequest
from agent_bot.models import ChatContext, ChatMessage
from agent_bot.services.chat.errors import PromptTooLongError
from agent_bot.services.chat.prompting import ChatPromptBuilder
from agent_bot.state import ConversationContext


def test_prompt_builder_drops_oldest_history_to_fit_total_character_budget():
    """系统与当前输入各占 32000 时，真实历史必须从旧到新裁切到 120000 内。"""
    builder = ChatPromptBuilder(system_prompt="system" * 5333 + "sy", max_input_chars=32000)
    current = "current" * 4571 + "use"
    context = ConversationContext(memory="", messages=(
        ChatMessage(role="assistant", content="a" * 32000),
        ChatMessage(role="assistant", content="b" * 32000),
        ChatMessage(role="assistant", content="c" * 16000),
    ))

    messages = builder.messages(current, conversation_context=context)

    assert sum(len(message.content) for message in messages) <= 120000
    assert [message.content for message in messages] == [
        "system" * 5333 + "sy", "b" * 32000, "c" * 16000, current,
    ]
    TextRequest(model="text", session_id="s", request_id="r", prompt=current,
                messages=tuple(messages))


def test_fixed_memory_group_system_and_current_are_preserved_when_history_is_removed():
    """固定背景资料保留完整包装和角色，预算不足时只移除旧历史。"""
    builder = ChatPromptBuilder(system_prompt="s" * 32000, max_input_chars=32000)
    context = ConversationContext(memory="memory" * 3000, messages=(
        ChatMessage(role="user", content="old-user" * 4000),
        ChatMessage(role="assistant", content="old-assistant" * 2461),
    ))
    group = "group" * 3600
    current = "u" * 32000
    fixed = builder.messages(current, conversation_context=ConversationContext(
        memory=context.memory, messages=(),
    ), recent_group_context=group)

    actual = builder.messages(current, conversation_context=context, recent_group_context=group)

    assert actual == fixed
    assert len(actual) == 6
    assert actual[0].content == "s" * 32000 and actual[-1].content == current
    assert context.memory in actual[1].content and group in actual[-3].content
    assert sum(len(message.content) for message in actual) <= 120000


def test_builder_bounds_legacy_history_messages_without_mutating_context():
    """没有日志合并器的调用方也不能把超长历史送入单条 32000 的模型协议。"""
    builder = ChatPromptBuilder(system_prompt="", max_input_chars=32000)
    original = ChatMessage(role="assistant", content="legacy" * 20000,
                           history_request_id="old-request")
    context = ConversationContext(memory="", messages=(original,))

    messages = builder.messages("当前问题", conversation_context=context)

    assert len(messages) == 2
    assert messages[0].role == "assistant" and len(messages[0].content) <= 32000
    assert messages[0].history_request_id == "old-request"
    assert messages[0].content.startswith("legacy" * 100)
    assert context.messages[0].content == "legacy" * 20000
    TextRequest(model="text", session_id="s", request_id="r", prompt="当前问题",
                messages=tuple(messages))


def test_fixed_content_over_total_budget_raises_clear_prompt_error():
    """固定内容自身超限时不能通过静默裁切系统、记忆或当前用户来迁就模型。"""
    builder = ChatPromptBuilder(system_prompt="s" * 32000, max_input_chars=32000)
    context = ConversationContext(memory="m" * 30000, messages=(
        ChatMessage(role="assistant", content="历史可以删除"),
    ))

    with pytest.raises(PromptTooLongError):
        builder.messages("u" * 32000, conversation_context=context,
                         recent_group_context="g" * 30000)


@pytest.mark.parametrize("oversized", ["system", "current", "memory", "group"])
def test_fixed_message_over_individual_limit_is_rejected_without_truncation(oversized):
    """任一固定消息超过模型单条上限均明确报错，不能悄悄丢失当前要求。"""
    builder = ChatPromptBuilder(system_prompt="s" * 32001 if oversized == "system" else "系统",
                                max_input_chars=64000)
    context = ConversationContext(memory="m" * 32001 if oversized == "memory" else "",
                                  messages=())
    current = "u" * 32001 if oversized == "current" else "当前问题"
    group = "g" * 32001 if oversized == "group" else None

    with pytest.raises(PromptTooLongError):
        builder.messages(current, conversation_context=context, recent_group_context=group)


def test_history_message_count_allows_consecutive_assistants_and_keeps_latest_99():
    """独立助手事件按消息裁切，99 条历史加当前用户正好满足 100 条协议上限。"""
    builder = ChatPromptBuilder(system_prompt="", max_input_chars=32000)
    context = ConversationContext(memory="", messages=tuple(
        ChatMessage(role="assistant", content=f"通知-{index}") for index in range(110)
    ))

    messages = builder.messages("当前问题", conversation_context=context)

    assert len(messages) == 100
    assert messages[0].content == "通知-11"
    assert all(message.role == "assistant" for message in messages[:-1])
    assert messages[-1] == ChatMessage(role="user", content="当前问题")


def test_fixed_message_count_cannot_exceed_upstream_limit():
    """固定消息已经超过 100 条时，不允许悄悄删除系统指令或返回无效请求。"""
    builder = ChatPromptBuilder(system_prompt="", max_input_chars=32000)
    systems = tuple(ChatMessage(role="system", content=f"系统-{index}") for index in range(100))

    with pytest.raises(PromptTooLongError):
        builder.messages("当前问题", system_messages=systems)


def test_qq_source_is_reserved_before_trimming_history_message_count():
    """来源说明新增系统消息也占预算，不能先填满100条再额外追加。"""
    builder = ChatPromptBuilder(system_prompt="", max_input_chars=32000)
    context = ConversationContext(memory="", messages=tuple(
        ChatMessage(role="assistant", content=f"通知-{index}", history_request_id=f"old-{index}")
        for index in range(110)
    ))
    qq = ChatContext(scene="group", groupId="20001", senderId="10001", senderName="bai")
    messages = builder.messages("当前问题", conversation_context=context, chat_context=qq)
    final = format_qq_context(messages, qq)
    assert len(final) == 100
    assert final[0].content == GUARDRAIL
    assert final[1].content == "通知-12"
    assert final[-1].content.endswith("当前问题")
    assert len(context.messages) == 110
    AgentRequest(prompt="当前问题", session_id="s", request_id="r", messages=final)
    TextRequest(model="text", session_id="s", request_id="r", prompt="当前问题", messages=final)


def test_qq_source_characters_are_reserved_before_trimming_history():
    """QQ来源及安全说明占用的字符也要计入总预算，只移除最旧历史。"""
    builder = ChatPromptBuilder(system_prompt="s" * 2000, max_input_chars=32000)
    context = ConversationContext(memory="", messages=tuple(
        ChatMessage(role="assistant", content=letter * size)
        for letter, size in (("a", 32000), ("b", 28000), ("c", 28000), ("d", 28000))
    ))
    qq = ChatContext(scene="private", senderId="10001", senderName="bai")
    messages = builder.messages("u" * 2000, conversation_context=context, chat_context=qq)
    final = format_qq_context(messages, qq)
    assert sum(len(m.content) for m in final) <= 120000
    assert messages[0].content == "s" * 2000 and messages[-1].content == "u" * 2000
    assert messages[1].content == "b" * 28000
    AgentRequest(prompt="u" * 2000, session_id="s", request_id="r", messages=final)


@pytest.mark.parametrize("oversized", ["system", "current"])
def test_qq_source_cannot_push_fixed_message_over_individual_limit(oversized):
    """来源包装后固定内容超限应清晰拒绝，不裁切当前提问或信任边界。"""
    builder = ChatPromptBuilder(system_prompt="s" * 32000 if oversized == "system" else "系统",
                                max_input_chars=32000)
    qq = ChatContext(scene="private", senderId="10001", senderName="bai")
    with pytest.raises(PromptTooLongError):
        builder.messages("u" * 32000 if oversized == "current" else "当前问题", chat_context=qq)
