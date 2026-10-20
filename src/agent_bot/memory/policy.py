"""纯函数实现事实规范化、有效性与排序，不负责模型语义裁决。"""

import hashlib
import json
import unicodedata
from datetime import datetime, timezone
from typing import Any

IMPORTANCE = {
    "relationship": 0.9,
    "preference": 0.8,
    "agreement": 0.9,
    "task": 0.9,
    "fact": 0.5,
    "correction": 1.0,
}


def utc_now() -> datetime:
    """数据库统一使用无时区 UTC，API 序列化时重新标明时区。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalized_content(content: str) -> str:
    """只规范全半角与空白，保留否定、标点、数字和大小写含义。"""
    return " ".join(unicodedata.normalize("NFKC", content).split())


def fingerprint(content: str) -> str:
    """摘要仅用于查找候选，实际去重仍核对规范正文。"""
    return hashlib.sha256(normalized_content(content).encode()).hexdigest()


def time_value(value: Any) -> datetime | None:
    """统一驱动返回的 UTC 时间；旧原始时间不用于事实有效性。"""
    if value is None:
        return None
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def effective_status(row: dict, at: datetime | None = None) -> str:
    """失效按半开时间区间计算，读操作不偷偷写状态。"""
    status = row.get("status", "deleted" if row.get("is_deleted") else "active")
    if status != "active":
        return status
    now = at or utc_now()
    until, start = time_value(row.get("valid_until")), time_value(row.get("valid_from"))
    if until is not None and until <= now:
        return "expired"
    if start is not None and start > now:
        return "future"
    return "active"


def preview_digest(items: list, action: str, target_id: int | None = None) -> str:
    """摘要绑定目标、版本及正文，不把客户端摘要本身当作授权。"""
    source = [(item.id, item.revision, item.content, item.status) for item in items]
    data = json.dumps((action, target_id, sorted(source)), ensure_ascii=False)
    return hashlib.sha256(data.encode()).hexdigest()
