"""清理模型照抄的内部会话标记，不从生成文字创建任何平台权限或消息段。"""

import re

_INTERNAL_AT = re.compile(r"\[AT\s+(?:[1-9][0-9]{4,19}|all)\]", re.IGNORECASE)


def clean_internal_reply_text(text: str) -> str:
    """移除历史原生 @ 的内部表示，普通 QQ 号和正文内容保持原样。"""
    cleaned = _INTERNAL_AT.sub("", text)
    return cleaned.strip() if cleaned != text else text
