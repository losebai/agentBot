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
# 剥离兜底只认完整开闭标签；属性中的引号文本不属于标签本体。
_TOOL_MARKUP_OPEN = re.compile(
    r"<[|\uff5c]+DSML[|\uff5c]+\s*(?:function_calls|calls|invoke|parameter)\b[^>]*>",
    re.IGNORECASE,
)
_TOOL_MARKUP_CLOSE = re.compile(
    r"</[|\uff5c]+DSML[|\uff5c]+\s*(?:function_calls|calls|invoke|parameter)\s*>",
    re.IGNORECASE,
)
# 此规则只用于已确认图片送达后的可选通知；出现失败或未完成措辞时保守跳过。
_IMAGE_COMPLETION_CONTRADICTION = re.compile(
    r"失败|报错|出错|(?:没(?:有|能)?|还没|尚未|未能|无法|不能|未)(?:成功)?"
    r"(?:生成|出图|绘制|画好|画出|画完|完成|发出|发送|发给|送达)"
    r"|(?:还在|仍在|正在)(?:生成|绘制|画图|绘图|排队|等待)"
)


def contradicts_image_completion(text: str) -> bool:
    """拦截与已送达图片相冲突的通知措辞，不改变实际任务状态或构造替代话术。"""
    return _IMAGE_COMPLETION_CONTRADICTION.search(re.sub(r"\s+", "", text)) is not None


def contains_tool_call_markup(text: str) -> bool:
    """识别正文中的伪工具控制标记，不解析参数或把生成文字升级为真实工具调用。"""
    return _TOOL_CALL_MARKUP.search(text) is not None


def strip_tool_call_markup(text: str) -> str:
    """丢弃首个伪工具开标记到最后一个闭标记之间的全部内容，只保留正常正文。

    闭标签缺失时从开标记处截断；标记块的参数一律不进入回复。
    """
    opened = [match.start() for match in _TOOL_MARKUP_OPEN.finditer(text)]
    if not opened:
        return text
    closed = [
        match.end() for match in _TOOL_MARKUP_CLOSE.finditer(text)
        if match.end() > opened[0]
    ]
    cleaned = text[: opened[0]] + (text[closed[-1]:] if closed else "")
    return cleaned.strip()


def clean_internal_reply_text(text: str) -> str:
    """剥离内部 @ 和伪提醒标签，不执行属性中的目标或提醒意图，保留真实正文。"""
    cleaned = _INTERNAL_MENTION.sub("", _INTERNAL_AT.sub("", text))
    return cleaned.strip() if cleaned != text else text
