"""群聊关键词固定回复匹配。"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping

MAX_KEYWORD_RULES = 50
MAX_KEYWORD_CHARS = 64
MAX_KEYWORD_REPLY_CHARS = 1000
MAX_KEYWORD_SCAN_CHARS = 20_000


def normalize_keyword(value: str) -> str:
    """统一全角/半角与大小写，供配置去重和消息匹配共用。"""

    return unicodedata.normalize("NFKC", value.strip()).casefold()


def contains_disallowed_control(value: str, *, allow_layout: bool = False) -> bool:
    """拒绝可能改变消息解释方式的控制字符。"""

    allowed = {"\n", "\t"} if allow_layout else set()
    return any(
        character not in allowed and unicodedata.category(character) == "Cc"
        for character in value
    )


def find_keyword_reply(text: str, rules: Mapping[str, str]) -> str | None:
    """返回最长命中关键词对应的回复；未命中时返回 ``None``。"""

    if not text or not rules:
        return None
    normalized_text = normalize_keyword(text[:MAX_KEYWORD_SCAN_CHARS])
    if not normalized_text:
        return None

    candidates: list[tuple[int, str, str]] = []
    for keyword, reply in rules.items():
        normalized_keyword = normalize_keyword(keyword)
        if normalized_keyword and normalized_keyword in normalized_text:
            candidates.append((len(normalized_keyword), normalized_keyword, reply))
    if not candidates:
        return None

    candidates.sort(key=lambda candidate: (-candidate[0], candidate[1]))
    return candidates[0][2]
