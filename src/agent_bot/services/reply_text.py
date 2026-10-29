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


def clean_internal_reply_text(text: str) -> str:
    """剥离内部 @ 和伪提醒标签，不执行属性中的目标或提醒意图，保留真实正文。"""
    cleaned = _INTERNAL_MENTION.sub("", _INTERNAL_AT.sub("", text))
    return cleaned.strip() if cleaned != text else text
