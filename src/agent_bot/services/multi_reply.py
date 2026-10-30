"""QQ 回复的 JSON 正文解码与严格分条协议，不改变模型工具调用协议。"""

import json
from typing import TYPE_CHECKING

from agent_bot.ai.chat_errors import ChatAiProtocolError
from agent_bot.services.reply_text import clean_internal_reply_text

if TYPE_CHECKING:
    from agent_bot.config import RuntimeConfig

DEFAULT_MULTI_REPLY_PROMPT = (
    "按自然聊天语义决定是否分成多条消息；简短回答只用一条，不为凑数拆句。"
    "每条应表达完整意思，不拆断代码块、引用或列表，不重复内容，不输出内部过程。"
)


def build_multi_reply_prompt(config: "RuntimeConfig") -> str:
    """自定义表达规则之后固定传输协议，避免编辑提示词意外取消校验约束。"""
    return (
        f"{config.chat_multi_reply_prompt}\n\n"
        "[QQ 多条回复协议]\n"
        "仅最终回复正文使用标准 JSON 字符串数组，所有数组元素都是非空消息。"
        f"消息数量为 1 至 {config.chat_multi_reply_max_messages} 条。"
        '例如：["第一条消息", "第二条消息"]；单条也使用 ["回答正文"]。'
        "数组之外不得输出说明、Markdown 代码块围栏或其他文字。"
        "工具调用保持原工具协议；该数组不是工具参数，不要把工具轮的解释当成最终回复。"
    )


def parse_multi_reply(text: str, maximum: int) -> tuple[str, ...]:
    """完整解析后检查类型和条数，格式错误不得修复或退回原文发送。"""
    try:
        messages = json.loads(text)
    except (ValueError, RecursionError) as error:
        raise ChatAiProtocolError(
            "多条回复必须是完整的 JSON 字符串数组", error_code="CHAT_MULTI_REPLY_INVALID",
        ) from error
    if (isinstance(messages, list) and 1 <= len(messages) <= maximum
            and all(isinstance(item, str) for item in messages)):
        # 在生成持久化正文和分段之前使用同一结果，避免投递时改写造成来源不一致。
        cleaned = tuple(clean_internal_reply_text(item) for item in messages)
        if all(item.strip() for item in cleaned):
            return cleaned
    raise ChatAiProtocolError(
        f"多条回复须包含 1 至 {maximum} 条非空字符串消息",
        error_code="CHAT_MULTI_REPLY_INVALID",
    )


def join_reply_array(text: str) -> str:
    """关闭分条时解码字符串、数组及已确认的单字段包装，其他结构保持原样。"""
    try:
        messages = json.loads(text)
    except (ValueError, RecursionError):
        # 此模式允许自由文本，解析失败不是协议错误，继续使用原有普通回复规则。
        return text
    if isinstance(messages, str):
        # 人设允许单条 JSON 字符串，解析一层即可恢复换行，不能再替换正文中的反斜杠。
        return messages
    if (isinstance(messages, dict) and messages.keys() == {"消息内容"}
            and isinstance(messages["消息内容"], str)):
        # 兼容已出现的模型包装；只取明确的单个文本字段，不递归解码或猜测其他对象。
        return messages["消息内容"]
    if isinstance(messages, list) and all(isinstance(message, str) for message in messages):
        return "".join(messages)
    return text
