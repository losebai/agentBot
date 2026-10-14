"""筛选绑定的 ID 游标与跨数据库 LIKE 字面量转义。"""

import base64
import binascii
import json

from agent_bot.knowledge.errors import KnowledgeError


def encode_cursor(last_id: int, filters: dict[str, str]) -> str:
    """仅携带数据库 ID 和完整筛选，不承载 SQL 表达式。"""
    payload = json.dumps({"id": last_id, "filters": filters}, ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(payload).decode()


def decode_cursor(cursor: str | None, filters: dict[str, str]) -> int | None:
    """损坏或跨筛选游标明确拒绝，避免用户误以为查询了完整结果。"""
    if cursor is None:
        return None
    try:
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (
            not isinstance(value, dict)
            or set(value) != {"id", "filters"}
            or value["filters"] != filters
            or type(value["id"]) is not int
            or not 1 <= value["id"] <= 2147483647
        ):
            raise ValueError("invalid cursor")
        return value["id"]
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error) as error:
        raise KnowledgeError("KNOWLEDGE_INVALID_PAGE", status_code=422) from error


def literal_like(value: str) -> str:
    """用感叹号作显式 ESCAPE，兼容 SQLite 和 MySQL 的反斜杠差异。"""
    return "%" + value.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
