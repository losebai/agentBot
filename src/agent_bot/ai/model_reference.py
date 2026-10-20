"""统一模型记录 ID 的持久引用编码，历史别名仅由目录兼容解析。"""

import re


def model_reference(id: int) -> str:
    """只将正整数主键编码为无歧义的模型引用。"""
    if isinstance(id, bool) or not isinstance(id, int) or id <= 0:
        raise ValueError("invalid model id")
    return f"model:{id}"


def parse_model_reference(value: str) -> int | None:
    """普通历史别名返回空值；保留前缀必须严格遵循正整数编码。"""
    if not value.startswith("model:"):
        return None
    if re.fullmatch(r"model:[1-9][0-9]*", value) is None:
        raise ValueError("invalid model reference")
    return int(value[6:])
