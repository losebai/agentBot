"""知识与管理员记忆输入不放宽标签、身份或分页边界。"""

import base64
import json

import pytest
from pydantic import ValidationError

from agent_bot.knowledge.contracts import (
    KnowledgeCreate,
    KnowledgeSearchArguments,
    MemoryCreate,
)
from agent_bot.knowledge.errors import KnowledgeError
from agent_bot.knowledge.pagination import decode_cursor, encode_cursor, literal_like


def test_knowledge_limits_and_tag_normalization():
    """拒绝空正文和超限，归一化后的标签不产生重复主题。"""
    value = KnowledgeCreate(
        title="x" * 200, content="文" * 16000, tags=[" \uff27\uff21\uff2d\uff25 ", "game", "中文"]
    )
    assert value.tags == ("game", "中文")
    for fields in (
        {"title": "x" * 201},
        {"content": "文" * 16001},
        {"content": " \n "},
        {"tags": ["a" * 33]},
        {"tags": ["\u200b"]},
        {"content": "文\0"},
    ):
        with pytest.raises(ValidationError):
            KnowledgeCreate.model_validate(
                {"title": "标题", "content": "正文", "tags": ["主题"], **fields}
            )


def test_memory_target_and_search_validation():
    """管理员只能明确选择已有作用域或提供与 QQ 相同的目标标识。"""
    base = {"scope": "user", "key": "偏好", "content": "喜欢中文", "tags": ["语言"]}
    assert MemoryCreate(**base, bot_id="10001", target="10002").target == "10002"
    assert MemoryCreate(**base, scope_id="a" * 64).scope_id == "a" * 64
    for fields in (
        {},
        {"bot_id": "10001"},
        {"bot_id": "10001", "target": "用户"},
        {"scope_id": "a" * 64, "bot_id": "10001", "target": "10002"},
        {"scope_id": "broken"},
    ):
        with pytest.raises(ValidationError):
            MemoryCreate(**base, **fields)
    with pytest.raises(ValidationError):
        KnowledgeSearchArguments(query=" ", tags=[])
    assert KnowledgeSearchArguments(tags=["中文"]).tags == ("中文",)


def test_cursor_filter_binding_and_literal_like():
    """分页不能混用筛选或非法 ID，通配符仅按字面量匹配。"""
    filters = {"query": "中文", "tag": "主题"}
    cursor = encode_cursor(42, filters)
    assert decode_cursor(cursor, filters) == 42
    assert decode_cursor(None, filters) is None
    with pytest.raises(KnowledgeError):
        decode_cursor(cursor, {"query": "英文", "tag": "主题"})
    for value in (True, -1, 0, "42"):
        broken = base64.urlsafe_b64encode(
            json.dumps({"id": value, "filters": filters}).encode()
        ).decode()
        with pytest.raises(KnowledgeError):
            decode_cursor(broken, filters)
    assert literal_like("a%_!\\") == "%a!%!_!!\\%"
