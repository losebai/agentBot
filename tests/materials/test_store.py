"""用真实临时 SQLite 验证素材持久化、会话隔离及失败回滚。"""

import sqlite3
from pathlib import Path

import pytest

from agent_bot.database_pool import create_database_pool
from agent_bot.media.contracts import MediaOwner
from agent_bot.media.files import MediaFiles
from agent_bot.settings import DatabaseParams


@pytest.fixture
def material_database(tmp_path):
    """只初始化测试临时库，不使用部署连接。"""
    path = tmp_path / "materials.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(Path("sql/agent_bot_init.sqlite.sql").read_text(encoding="utf-8"))
    params = DatabaseParams(backend="sqlite", sqlite_path=str(path))
    pool = create_database_pool(params)
    yield params, pool
    pool.close()


@pytest.fixture
async def materials(material_database, tmp_path):
    """素材目录独立于临时附件目录，结束后释放进程锁。"""
    from agent_bot.materials.store import MaterialStore

    params, pool = material_database
    store = MaterialStore(params, connect=pool.connect, files=MediaFiles(tmp_path / "materials"))
    await store.start()
    yield store
    await store.aclose()


@pytest.mark.asyncio
async def test_file_roundtrip_reuses_id_and_keeps_private_metadata(materials):
    """再次接收或引用同一内容不会新建素材，公开结果不含内部路径。"""
    from agent_bot.materials.contracts import MaterialSearchArguments

    owner = MediaOwner(entry="qq", session_id="session-a")
    first = await materials.put(
        owner=owner, name="计划.txt", mime_type="text/plain", data=b"plan",
        source_key="a" * 64,
    )
    repeated = await materials.put(
        owner=owner, name="计划.txt", mime_type="text/plain", data=b"plan",
        source_key="b" * 64,
    )
    assert repeated.id == first.id
    assert await materials.find_source(owner, "b" * 64) == first
    result = await materials.search(owner, MaterialSearchArguments(query="计划"))
    assert [item.id for item in result.records] == [first.id]
    assert "storage_key" not in result.model_dump_json()
    assert "session-a" not in result.model_dump_json()
    async with materials.open_content(first.id, owner=owner) as file:
        assert file.read() == b"plan"


@pytest.mark.asyncio
async def test_other_session_cannot_query_or_read_material(materials):
    """知道素材 ID 也不能绕过真实会话授权。"""
    from agent_bot.materials.contracts import MaterialSearchArguments
    from agent_bot.media.errors import MediaError

    asset = await materials.put(
        owner=MediaOwner(entry="qq", session_id="a"),
        name="private.txt", mime_type="text/plain", data=b"secret",
    )
    other = MediaOwner(entry="qq", session_id="b")
    result = await materials.search(other, MaterialSearchArguments(ids=(asset.id,)))
    assert result.records == ()
    with pytest.raises(MediaError, match="MATERIAL_NOT_FOUND"):
        await materials.describe(asset.id, owner=other)


@pytest.mark.asyncio
async def test_failed_write_rolls_back_database_and_files(materials, monkeypatch):
    """文件发布失败时不能留下可查询的成功记录或半成品。"""
    from agent_bot.materials.contracts import MaterialSearchArguments

    def fail(key, data):
        """模拟文件写完后、提交元数据前的磁盘失败。"""
        path = materials.files.path(key, temporary=True)
        path.write_bytes(data)
        raise OSError("disk full")

    monkeypatch.setattr(materials.files, "write", fail)
    owner = MediaOwner(entry="qq", session_id="a")
    with pytest.raises(OSError):
        await materials.put(owner=owner, name="a.txt", mime_type="text/plain", data=b"x")
    assert (await materials.search(owner, MaterialSearchArguments())).records == ()
    assert materials.files.entries() == []


@pytest.mark.asyncio
async def test_memory_links_material_and_rejects_foreign_material_atomically(
    materials, material_database,
):
    """图片/文件记忆召回稳定 ID，非法关联不能留下半条记忆。"""
    from agent_bot.integrations.tagged_memory_store import TaggedMemoryStore
    from agent_bot.services.tagged_memory import MemoryArguments, MemoryIdentity

    params, pool = material_database
    memory = TaggedMemoryStore(params, connect=pool.connect, secret="test-secret")
    first = await materials.put(
        owner=MediaOwner(entry="qq", session_id="a"),
        name="计划.txt", mime_type="text/plain", data=b"plan",
    )
    identity = MemoryIdentity(bot_id="10000", user_id="10001", session_id="a")
    memory.execute(identity, MemoryArguments(
        action="save", key="计划", content="保存原件", tags=("计划",), material_ids=(first.id,),
    ))
    records = memory.execute(identity, MemoryArguments(action="recall", tags=("计划",))).records
    assert records[0].material_ids == (first.id,)
    foreign = MemoryIdentity(bot_id="10000", user_id="10002", session_id="b")
    with pytest.raises(ValueError, match="素材"):
        memory.execute(foreign, MemoryArguments(
            action="save", key="越权", content="不应保存", tags=("计划",), material_ids=(first.id,),
        ))
    assert memory.execute(foreign, MemoryArguments(action="recall", tags=("计划",))).records == ()
    # 共享文字记忆不会把原会话的素材访问权授予另一会话。
    memory.execute(identity, MemoryArguments(
        action="save", scope="global", key="共享计划", content="有一个计划", tags=("共享",),
        material_ids=(first.id,),
    ))
    shared = memory.execute(foreign, MemoryArguments(action="recall", scope="all", tags=("共享",)))
    assert shared.records[0].material_ids == ()


@pytest.mark.asyncio
async def test_search_tool_uses_bound_owner_and_valid_schema(materials):
    """真实工具接受可用 schema，但不能通过参数切换素材所属会话。"""
    from agent_bot.agent.contracts import ToolCall
    from agent_bot.agent.tools.materials import MaterialSearchTool
    from agent_bot.agent.tools.schema import ToolSchema

    owner = MediaOwner(entry="admin", session_id="admin_test")
    asset = await materials.put(owner=owner, name="sample.txt", mime_type="text/plain", data=b"a")
    tool = MaterialSearchTool(materials, owner)
    ToolSchema.validate_definition(tool.definition)
    result = await tool.call(ToolCall(call_id="lookup-1", name="material_search",
                                     arguments={"ids": [asset.id]}))
    assert asset.id in result.content
    assert not result.images


@pytest.mark.asyncio
async def test_received_and_quoted_image_share_material_id(materials):
    """引用同一图片时复用持久原件，不依赖可能已失效的 QQ 链接。"""
    from io import BytesIO
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from nonebot.adapters.onebot.v11 import Message, MessageSegment
    from PIL import Image

    from agent_bot.materials.receiver import MaterialReceiver
    from agent_bot.media.inputs import ImagePreparer

    output = BytesIO()
    Image.new("RGB", (2, 2), "red").save(output, "PNG")
    image = ImagePreparer().prepare(output.getvalue(), "image/png")
    downloader = SimpleNamespace(fetch=AsyncMock(return_value=image))
    receiver = MaterialReceiver(materials, image_downloader=downloader)
    owner = MediaOwner(entry="qq", session_id="a")
    bot = SimpleNamespace(self_id="10000", call_api=AsyncMock())
    segment = MessageSegment("image", {"file": "same-image", "url": "https://qpic.cn/image"})
    first = await receiver.register(bot, owner, segment, group_id=None)
    downloader.fetch.side_effect = AssertionError("引用应读取已保存原件")
    quoted = await receiver.register(bot, owner, segment, group_id=None)
    assert first.id == quoted.id

    from agent_bot.multimodal import MultimodalInput
    from agent_bot.quoted_summary import build_quoted_chat_input

    async def register(item):
        """引用解析器使用与直接接收相同的注册边界。"""
        return await receiver.register(bot, owner, item, group_id=None)

    result = await build_quoted_chat_input(
        bot, Message(segment), current_input=MultimodalInput(prompt="这张图", image_urls=()),
        quoted_sender="用户", image_recognition_enabled=True, max_images=8, max_videos=2,
        max_prompt_chars=1000, register_material=register,
    )
    assert result.material_ids == (first.id,)
    assert result.image_material_ids == (first.id,)
    assert result.image_urls == ()
    assert first.id in result.prompt


@pytest.mark.asyncio
async def test_received_file_uses_onebot_url_and_stable_id(materials):
    """普通文件通过可信 API 获取 URL；登记文件不会调用图片模型或图片下载器。"""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from nonebot.adapters.onebot.v11 import MessageSegment

    from agent_bot.materials.receiver import MaterialReceiver

    files = SimpleNamespace(fetch=AsyncMock(return_value=(b"document", "text/plain")))
    images = SimpleNamespace(fetch=AsyncMock(side_effect=AssertionError("file is not image")))
    bot = SimpleNamespace(self_id="10000", call_api=AsyncMock(return_value={
        "url": "https://files.qq.com/document",
    }))
    receiver = MaterialReceiver(materials, image_downloader=images, file_downloader=files)
    owner = MediaOwner(entry="qq", session_id="a")
    segment = MessageSegment("file", {"file_id": "file-1", "file": "计划.txt", "file_size": 8})
    first = await receiver.register(bot, owner, segment, group_id=20000)
    bot.call_api.assert_awaited_once_with("get_group_file_url", group_id=20000, file_id="file-1")
    assert first.kind == "file" and first.name == "计划.txt"
    assert (await receiver.register(bot, owner, segment, group_id=20000)).id == first.id


@pytest.mark.asyncio
async def test_cancelled_publish_preserves_committed_file(materials, monkeypatch):
    """取消等待已开始事务结束，不能删除已提交元数据引用的原件。"""
    import asyncio
    import threading

    from agent_bot.materials.contracts import MaterialSearchArguments

    entered, release = threading.Event(), threading.Event()
    original = materials.files.write

    def pause(key, data):
        """暂停实际文件发布，模拟客户端在事务中途断开。"""
        entered.set()
        assert release.wait(5)
        original(key, data)

    monkeypatch.setattr(materials.files, "write", pause)
    owner = MediaOwner(entry="qq", session_id="a")
    task = asyncio.create_task(materials.put(
        owner=owner, name="a.txt", mime_type="text/plain", data=b"saved",
    ))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    records = (await materials.search(owner, MaterialSearchArguments())).records
    assert len(records) == 1
    async with materials.open_content(records[0].id, owner=owner) as file:
        assert file.read() == b"saved"


@pytest.mark.asyncio
async def test_same_filename_with_different_urls_is_not_same_source(materials):
    """文件显示名称不代表附件身份，不得把另一份同名文件当作已接收原件。"""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from nonebot.adapters.onebot.v11 import MessageSegment

    from agent_bot.materials.receiver import MaterialReceiver

    files = SimpleNamespace(fetch=AsyncMock(side_effect=[(b"first", "text/plain"),
                                                        (b"second", "text/plain")]))
    receiver = MaterialReceiver(materials, file_downloader=files)
    owner = MediaOwner(entry="qq", session_id="a")
    bot = SimpleNamespace(self_id="10000")
    first = await receiver.register(bot, owner, MessageSegment("file", {
        "file": "计划.txt", "url": "https://files.qq.com/first",
    }), group_id=None)
    second = await receiver.register(bot, owner, MessageSegment("file", {
        "file": "计划.txt", "url": "https://files.qq.com/second",
    }), group_id=None)
    assert first.id != second.id
    async with materials.open_content(second.id, owner=owner) as file:
        assert file.read() == b"second"


def test_migration_effective_schema_matches_fresh_database(tmp_path):
    """实际执行增量，比较生效后的表、索引和外键，而不是只检查 SQL 文本。"""
    full = Path("sql/agent_bot_init.sqlite.sql").read_text(encoding="utf-8")
    migration = Path("sql/2026-10-17-material-memory.sqlite.sql").read_text(encoding="utf-8")
    baseline = full.removesuffix(migration)
    assert baseline != full
    fresh = sqlite3.connect(tmp_path / "fresh.db")
    upgraded = sqlite3.connect(tmp_path / "upgraded.db")
    try:
        fresh.executescript(full)
        upgraded.executescript(baseline)
        upgraded.executescript(migration)
        for table in ("chat_material", "chat_material_source", "chat_memory_material"):
            for pragma in ("table_info", "index_list", "foreign_key_list"):
                assert fresh.execute(f"PRAGMA {pragma}({table})").fetchall() == (
                    upgraded.execute(f"PRAGMA {pragma}({table})").fetchall()
                )
    finally:
        fresh.close()
        upgraded.close()
