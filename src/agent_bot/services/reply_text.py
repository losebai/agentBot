"""清理模型照抄的内部标记与伪工具外壳，不从生成文字创建平台权限或消息段。"""

import re

_INTERNAL_AT = re.compile(r"\[AT\s+(?:[1-9][0-9]{4,19}|all)\]", re.IGNORECASE)
# 引号中的右括号属于属性文本；只剥离完整标签，正文及普通括号保持原样。
_INTERNAL_MENTION = re.compile(
    r'''\[/?mention_member\b(?:[^\[\]"']|"[^"]*"|'[^']*')*\]''', re.IGNORECASE,
)
# 仅识别保留的 DSML 控制标签；完整、混合正文及被截断的调用均不能冒充回复。
_TOOL_CALL_MARKUP = re.compile(
    r"</?[|\uff5c]+DSML[|\uff5c]+\s*(?:function_calls|calls|invoke|parameter)\b", re.IGNORECASE,
)


def contains_tool_call_markup(text: str) -> bool:
    """识别正文中的伪工具控制标记，不解析参数或把生成文字升级为真实工具调用。"""
    return _TOOL_CALL_MARKUP.search(text) is not None


def clean_internal_reply_text(text: str) -> str:
    """剥离内部 @ 和伪提醒标签，不执行属性中的目标或提醒意图，保留真实正文。"""
    cleaned = _INTERNAL_MENTION.sub("", _INTERNAL_AT.sub("", text))
    return cleaned.strip() if cleaned != text else text
