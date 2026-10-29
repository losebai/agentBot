"""真实插件资源生命周期、Agent绑定及分页语境检索验证。"""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from agent_bot.agent.contracts import AgentRequest, ExecutionAuthority, ToolCall
from agent_bot.agent.tools.runtime import ToolRuntime
from agent_bot.core.lifecycle import RuntimeLeaseGate
from agent_bot.media.files import MediaFiles
from agent_bot.plugins.stickers.contracts import StickerPatch
from agent_bot.plugins.stickers.plugin import StickersPlugin
from agent_bot.plugins.stickers.tools import StickerTools
from stickers.test_service import service, sticker
from stickers.test_store import store as store
from stickers.test_tools import tool_session


async def test_plugin_shutdown_drains_leases_and_releases_its_directory(store, tmp_path):
    """撤销服务立即生效，已经开始的读写完成后才能释放图库目录锁。"""
    runtime = SimpleNamespace(
        database_params=store.params, database_pool=SimpleNamespace(connect=store.connect),
        media_resources=SimpleNamespace(config=SimpleNamespace(
            directory=tmp_path / "plugin-media",
        )),
        gate=RuntimeLeaseGate(), sticker_service=None,
    )
    plugin = StickersPlugin()
    plugin.prepare(SimpleNamespace(runtime=runtime))
    await plugin.start()
    assert await plugin.is_ready() and runtime.sticker_service is plugin.service
    async with runtime.gate.lease():
        stopping = asyncio.create_task(plugin.stop())
        await asyncio.sleep(0)
        assert runtime.sticker_service is None and not stopping.done()
    await stopping
    assert not await plugin.is_ready()
    # 实际重新取得同一锁，证明退出未保留文件句柄或关闭其他目录。
    files = MediaFiles(tmp_path / "plugin-media" / "stickers")
    files.start()
    files.close()
    assert (await store.list())["total"] == 0


async def test_tool_runtime_obeys_agent_and_qq_authorization_intersection():
    """完整运行时绑定仍须取Agent选择与原始QQ令牌授权的交集。"""
    _, current, context = tool_session()

    @asynccontextmanager
    async def no_remote(*args, **kwargs):
        """替代外部MCP连接，保留本地ToolRuntime真实选取和调用。"""
        yield SimpleNamespace(tools=(), warnings=())

    from unittest.mock import AsyncMock

    runtime = ToolRuntime(
        SimpleNamespace(open=no_remote),
        chat_bridge=SimpleNamespace(context=AsyncMock(return_value=context)),
        sticker_tools=StickerTools(lambda: current),
    )
    request = AgentRequest(prompt="今天又加班", session_id="s", request_id="r")
    authority = ExecutionAuthority(entry="qq", allowed_tools=("sticker_search", "sticker_send"))
    snapshot = SimpleNamespace(agent=SimpleNamespace(tools=("sticker_search",)), mcps=())
    async with runtime.open(snapshot, request, authority) as bound:
        assert {item.name for item in bound.definitions} == {"sticker_search"}
        result = await bound.call(ToolCall(call_id="search", name="sticker_search",
                                          arguments={"context": "加班好累"}))
        assert json.loads(result.content)["matches"]


async def test_context_search_batches_all_metadata_without_silent_truncation():
    """图库超出一轮提示预算时遍历全部分页和批次，最后一张也有机会被匹配。"""
    rows = [sticker(f"{index:032x}", description="猫" * 1000,
                    usage_hint="表达加班疲惫" * 100) for index in range(1, 48)]
    current, complete, _ = service(rows)
    target = f"{47:032x}"

    async def ranking(request, authority):
        """只模拟真实模型边界，期待的目标ID来自人工构造的最后一条。"""
        assert len(request.prompt) < 32000
        matches = [{"id": target, "score": 0.9, "reason": "符合累瘫的语境"}]
        return SimpleNamespace(content=json.dumps({
            "matches": matches if target in request.prompt else [],
        }))

    complete.side_effect = ranking
    result = await current.search("今天工作好累")
    assert len(result["matches"]) == 1 and result["matches"][0]["sticker"]["id"] == target
    assert complete.await_count > 1


async def test_disabling_a_visited_sticker_cannot_skip_later_candidates():
    """模型等待期间停用前页条目，仍必须访问没有被停用的末页表情。"""
    rows = [sticker(f"{index:032x}", description="猫" * 1000) for index in range(1, 42)]
    current, complete, _ = service(rows)
    target = f"{41:032x}"

    async def rank_and_disable(request, authority):
        """模拟管理员在第一次模型往返期间停用已访问条目。"""
        current.store.rows[0] = current.store.rows[0].model_copy(update={"enabled": False})
        result = [{"id": target, "score": 0.95, "reason": "仍然符合语境"}]
        return SimpleNamespace(content=json.dumps({
            "matches": result if target in request.prompt else [],
        }))

    complete.side_effect = rank_and_disable
    result = await current.search("猫猫加班")
    assert [item["sticker"]["id"] for item in result["matches"]] == [target]


async def test_candidate_cursor_survives_disabling_previous_rows(store):
    """真实SQL游标使用不可变ID，启用过滤变化不能把后续记录挤到游标前。"""
    from stickers.test_images import picture

    rows = [await store.create("表情", picture(kind), "image/" + mime)
            for kind, mime in (("PNG", "png"), ("JPEG", "jpeg"), ("GIF", "gif"))]
    expected = sorted(item.id for item in rows)
    first = await store.candidates(limit=2)
    await store.update(first[0].id, StickerPatch(expected_revision=1, enabled=False))
    rest = await store.candidates(after=first[-1].id, limit=2)
    assert [item.id for item in rest] == expected[2:]
