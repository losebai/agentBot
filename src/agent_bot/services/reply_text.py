"""清理模型照抄的内部标记与伪工具外壳，不从生成文字创建平台权限或消息段。"""

import re

_INTERNAL_AT = re.compile(r"\[AT\s+(?:[1-9][0-9]{4,19}|all)\]", re.IGNORECASE)
# 引号中的右括号属于属性文本；只剥离完整标签，正文及普通括号保持原样。
_INTERNAL_MENTION = re.compile(
    r'''\[/?mention_member\b(?:[^\[\]"']|"[^"]*"|'[^']*')*\]''', re.IGNORECASE,
)


def clean_internal_reply_text(text: str) -> str:
    """剥离内部 @ 和伪提醒标签，不执行属性中的目标或提醒意图，保留真实正文。"""
    cleaned = _INTERNAL_MENTION.sub("", _INTERNAL_AT.sub("", text))
    return cleaned.strip() if cleaned != text else text
