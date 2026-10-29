"""独立视觉配置及语境匹配不访问付费模型，覆盖模型结果与修订边界。"""

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_bot.agent.errors import AgentError


def sticker(id="a" * 32, **changes):
    """构造与图库公开契约一致的已启用表情。"""
    from agent_bot.plugins.stickers.contracts import StickerView

    return StickerView(**({
        "id": id, "name": "猫猫崩溃", "tags": ["加班", "崩溃"],
        "description": "一只趴在桌上的疲惫猫", "emotion": "无奈",
        "usage_hint": "适合加班受挫、表达疲惫", "enabled": True, "revision": 1,
        "mime_type": "image/gif", "size_bytes": 10, "sha256": "0" * 64,
        "width": 2, "height": 2, "frame_count": 2, "animated": True,
        "analysis_status": "pending", "analysis_error": None,
        "created_at": datetime(2026, 1, 1), "updated_at": datetime(2026, 1, 1),
    } | changes))


class Library:
    """模型业务单测替代慢存储；真实SQL和文件事务由独立存储测试覆盖。"""

    def __init__(self, rows):
        """保留公开模型，避免缺字段的随意模拟结果掩盖接口错误。"""
        self.rows = list(rows)

    async def list(self, *, offset=0, limit=20, enabled=None, **filters):
        """模拟稳定分页，显式区分禁用表情。"""
        rows = [row for row in self.rows if enabled is None or row.enabled == enabled]
        items = rows[offset:offset + limit]
        return {"items": items, "total": len(rows),
                "next_offset": offset + len(items) if offset + len(items) < len(rows) else None}

    async def get(self, id):
        """精确读取真实ID，不能由模型虚构表情。"""
        return next(row for row in self.rows if row.id == id)

    async def candidates(self, *, after=None, limit=40):
        """内部游标按不可变ID前进，不受已访问条目的启停影响。"""
        return sorted((row for row in self.rows if row.enabled
                       and (after is None or row.id > after)), key=lambda row: row.id)[:limit]

    async def read(self, id, *, preview=False):
        """视觉只读静态预览，发送时才能取到原始GIF。"""
        return await self.get(id), b"preview-png" if preview else b"GIF89a-original"

    async def set_analysis(self, id, *, expected_revision, error=None, **values):
        """用修订检查模拟存储边界，识图不能覆盖并发编辑。"""
        old = await self.get(id)
        if old.revision != expected_revision:
            raise AgentError("STICKER_CONFLICT", status_code=409)
        updates = {} if error else values
        saved = old.model_copy(update={**updates, "revision": old.revision + 1,
                                       "analysis_status": "failed" if error else "ready",
                                       "analysis_error": error})
        self.rows[self.rows.index(old)] = saved
        return saved


def service(rows=(), *, model="model:7"):
    """只替换模型与存储边界；业务服务和输入契约均使用真实实现。"""
    from agent_bot.plugins.stickers.service import StickerService

    complete = AsyncMock()
    runtime = SimpleNamespace(
        config=SimpleNamespace(chat_sticker_model=model,
                               chat_sticker_vision_prompt="自定义识图要求",
                               chat_sticker_timeout_seconds=60),
        state=SimpleNamespace(ai_client=SimpleNamespace(
            agent_service=SimpleNamespace(complete=complete),
        )),
        media_resources=SimpleNamespace(store=SimpleNamespace(
            put=AsyncMock(return_value=SimpleNamespace(id="c" * 32)),
        )),
    )
    return StickerService(Library(rows), runtime), complete, runtime


async def test_empty_library_does_not_call_model_or_require_provider():
    """没有图库时正常返回无匹配，不调用模型或随意选图。"""
    current, complete, _ = service(model="")
    assert (await current.search("今晚又加班了"))["matches"] == []
    complete.assert_not_awaited()


async def test_context_search_uses_independent_model_and_saved_metadata():
    """语境匹配有情绪和用途输入，不能误用聊天Agent配置或再次上传图片。"""
    current, complete, _ = service([sticker()])
    complete.return_value = SimpleNamespace(content=json.dumps({"matches": [
        {"id": "a" * 32, "score": 0.95, "reason": "符合加班受挫的无奈"},
    ]}))
    result = await current.search("今晚又加班，方案还被退回了")
    assert result["matches"][0]["sticker"]["id"] == "a" * 32
    request, authority = complete.await_args.args
    assert request.model == "model:7" and request.agent_code is None
    assert request.tools == request.mcp_services == () and not request.attachment_ids
    assert "加班，方案还被退回" in request.prompt
    assert "适合加班受挫" in request.prompt and "无奈" in request.prompt
    assert authority.allowed_tools == ()


async def test_model_cannot_invent_matching_sticker_ids():
    """模型只可推荐本批候选中的真实表情，否则整次检索明确失败。"""
    current, complete, _ = service([sticker()])
    complete.return_value = SimpleNamespace(content=json.dumps({"matches": [
        {"id": "b" * 32, "score": 0.95, "reason": "虚构"},
    ]}))
    with pytest.raises(AgentError) as rejected:
        await current.search("加班")
    assert rejected.value.code == "STICKER_SEARCH_INVALID"


async def test_no_semantic_match_never_becomes_random_selection():
    """有图库但语境不匹配时保持空列表，禁用项也不能进入候选。"""
    current, complete, _ = service([sticker(), sticker("b" * 32, enabled=False)])
    complete.return_value = SimpleNamespace(content='{"matches":[]}')
    assert (await current.search("解释微积分"))["matches"] == []
    assert "b" * 32 not in complete.await_args.args[0].prompt


async def test_tag_filter_cannot_be_bypassed_by_model():
    """管理员标签参与明确过滤，未满足标签的表情不能发送到检索模型。"""
    current, complete, _ = service([sticker()])
    assert (await current.search("开心", tags=["庆祝"]))["matches"] == []
    complete.assert_not_awaited()


async def test_visual_analysis_preserves_manual_tags_and_uses_png_preview():
    """动画识图使用预览；生成描述不覆盖管理员自定义标签。"""
    current, complete, runtime = service([sticker()])
    complete.return_value = SimpleNamespace(content=json.dumps({
        "description": "猫猫捂脸", "emotion": "尴尬", "usage_hint": "出错后自嘲",
    }))
    saved = await current.analyze("a" * 32, expected_revision=1)
    assert saved.description == "猫猫捂脸" and saved.tags == ("加班", "崩溃")
    assert saved.analysis_status == "ready"
    request, _ = complete.await_args.args
    assert request.model == "model:7" and request.attachment_ids == ("c" * 32,)
    assert "自定义识图要求" in request.prompt and request.tools == ()
    assert runtime.media_resources.store.put.await_args.kwargs["data"] == b"preview-png"


async def test_visual_failure_is_visible_without_losing_saved_metadata():
    """上游失败记录明确状态，不丢原件、不覆盖人工信息、不泄漏原始错误。"""
    current, complete, _ = service([sticker()])
    complete.side_effect = AgentError("AI_UPSTREAM_AUTH_REJECTED", status_code=502)
    saved = await current.analyze("a" * 32, expected_revision=1)
    assert saved.analysis_status == "failed"
    assert saved.description == "一只趴在桌上的疲惫猫" and saved.tags == ("加班", "崩溃")
    assert saved.analysis_error == "AI_UPSTREAM_AUTH_REJECTED"


async def test_stale_revision_prevents_visual_provider_call():
    """管理员已编辑的旧页面不能消耗模型并覆盖新元数据。"""
    current, complete, _ = service([sticker(revision=2)])
    with pytest.raises(AgentError) as rejected:
        await current.analyze("a" * 32, expected_revision=1)
    assert rejected.value.code == "STICKER_CONFLICT"
    complete.assert_not_awaited()
