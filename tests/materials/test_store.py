"""用真实临时 SQLite 验证素材持久化、会话隔离及失败回滚。"""

import sqlite3
from pathlib import Path

import pytest

from agent_bot.database_pool import create_database_pool
from agent_bot.materials.store import MaterialStore
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
async def test_materials_above_one_gib_can_store_and_read_new_file(
    materials: MaterialStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """持久素材没有管理员设置的总配额，超过旧上限仍可保存且不能清理旧原件。"""
    owner = MediaOwner(entry="qq", session_id="large-library")
    old = await materials.put(owner=owner, name="old.txt", mime_type="text/plain", data=b"old")
    # 仅替换文件大小统计，避免测试分配超过1GiB磁盘；读写、事务和归属校验保持真实。
    monkeypatch.setattr(materials.files, "entries", lambda: [(old.id, 2 * 1024**3, 0)])
    new = await materials.put(owner=owner, name="new.txt", mime_type="text/plain", data=b"new")
    async with materials.open_content(new.id, owner=owner) as file:
        assert file.read() == b"new"
    async with materials.open_content(old.id, owner=owner) as file:
        assert file.read() == b"old"


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


async def test_explicit_qq_library_access_excludes_admin_and_keeps_original_owner(materials):
    """主人可跨 QQ 会话读取，不能读取管理素材或改变原素材归属。"""
    from agent_bot.materials.contracts import MaterialSearchArguments
    from agent_bot.media.errors import MediaError

    original = MediaOwner(entry="qq", session_id="other-group")
    current = MediaOwner(entry="qq", session_id="current-group")
    admin = MediaOwner(entry="admin", session_id="admin-chat")
    asset = await materials.put(owner=original, name="shared.txt", mime_type="text/plain",
                                data=b"qq-image-description", tags=("topic",))
    private = await materials.put(owner=admin, name="shared.txt", mime_type="text/plain",
                                  data=b"private-admin-data", tags=("topic",))
    result = await materials.search(
        current, MaterialSearchArguments(query="shared", tag="topic"), allow_qq_library=True,
    )
    assert [item.id for item in result.records] == [asset.id]
    assert await materials.describe(asset.id, owner=current, allow_qq_library=True) == asset
    async with materials.open_content(asset.id, owner=current, allow_qq_library=True) as file:
        assert file.read() == b"qq-image-description"
    with pytest.raises(MediaError, match="MATERIAL_NOT_FOUND"):
        await materials.describe(private.id, owner=current, allow_qq_library=True)
    assert await materials.describe(asset.id, owner=original) == asset
    with pytest.raises(MediaError, match="MATERIAL_NOT_FOUND"):
        await materials.describe(asset.id, owner=current)
    # QQ 特权开关不能反过来授予管理端跨会话能力。
    assert (await materials.search(
        admin, MaterialSearchArguments(), allow_qq_library=True,
    )).records == (private,)


@pytest.mark.parametrize("privileged,entry,token_session", [
    (True, "qq", "destination"), (False, "qq", "destination"),
    (True, "admin", "destination"), (True, "qq", "unrelated"), (True, "qq", None),
])
async def test_material_runtime_cross_group_access_uses_real_token(
    materials, privileged, entry, token_session,
):
    """真实令牌决定跨群检索和图片投递，普通成员、管理入口和模型参数不能提权。"""
    import base64
    import json
    from contextlib import asynccontextmanager
    from io import BytesIO
    from types import SimpleNamespace

    from PIL import Image
    from pydantic import SecretStr

    from agent_bot.agent.contracts import AgentRequest, ExecutionAuthority, ToolCall
    from agent_bot.agent.errors import AgentError
    from agent_bot.agent.tools.chat import ChatToolBridge
    from agent_bot.agent.tools.runtime import ToolRuntime
    from agent_bot.media.contracts import MediaScope
    from agent_bot.media.errors import MediaError
    from agent_bot.services.chat_tools import ChatToolContext
    from agent_bot.services.tool_images import capture_tool_images
    from agent_bot.services.tool_sessions import ToolSessionStore

    class EmptyConnector:
        """真实授权链只省略无关远程 MCP 连接。"""

        @asynccontextmanager
        async def open(self, *_args, **_kwargs):
            """不联网且不提供任何远程工具。"""
            yield SimpleNamespace(tools=(), warnings=())

    output = BytesIO()
    Image.new("RGB", (2, 2), "red").save(output, "PNG")
    raw = output.getvalue()
    asset = await materials.put(owner=MediaOwner(entry="qq", session_id="source"),
                                name="image.png", mime_type="image/png", data=raw)
    names = ("material_search", "material_get")
    scope = MediaScope(owner=MediaOwner(entry=entry, session_id="destination"),
                       execution_id="a" * 32, execution_kind="agent")
    sessions = ToolSessionStore(ttl_seconds=30, max_sessions=2)
    context = ChatToolContext(session_id=token_session or "destination", user_id="10001",
                              bot_user_id="90001", allowed_tools=frozenset(),
                              material_library_access=privileged)
    bridge = ChatToolBridge(SimpleNamespace(resolve_context=sessions.resolve))
    runtime = ToolRuntime(EmptyConnector(), chat_bridge=bridge, material_store=materials)
    request = AgentRequest(prompt="发送此素材", session_id="destination", request_id="cross-group")
    snapshot = SimpleNamespace(mcps=(), agent=SimpleNamespace(tools=names))
    async with sessions.open(context) as token:
        authority = ExecutionAuthority(
            entry=entry, allowed_tools=names,
            chat_token=SecretStr(token) if token_session else None,
        )
        async with runtime.open(snapshot, request, authority, media_scope=scope) as bound:
            search = ToolCall(call_id="find", name="material_search", arguments={"ids": [asset.id]})
            if token_session == "unrelated":
                with pytest.raises(AgentError, match="AGENT_TOOL_UNAUTHORIZED"):
                    await bound.call(search)
                return
            result = json.loads((await bound.call(search)).content)
            can_read = privileged and entry == "qq" and token_session is not None
            assert bool(result["records"]) is can_read
            read = ToolCall(call_id="read", name="material_get", arguments={"id": asset.id})
            if can_read:
                with capture_tool_images() as captured:
                    await bound.call(read)
                assert base64.b64decode(captured.items[0].data) == raw
                await sessions.revoke(token)
                with pytest.raises(AgentError, match="AGENT_TOOL_SESSION_EXPIRED"):
                    await bound.call(read)
            else:
                with pytest.raises(MediaError, match="MATERIAL_NOT_FOUND"):
                    await bound.call(read)
            with pytest.raises(AgentError, match="AGENT_TOOL_ARGUMENTS_INVALID"):
                bound.validate(read.model_copy(update={
                    "arguments": {"id": asset.id, "allow_qq_library": True},
                }))


@pytest.mark.asyncio
async def test_categories_merge_without_replacing_original_or_restoring_tags(materials):
    """同图生成后发送保留两种类型，已清除的来源标签不随重复流转恢复。"""
    from agent_bot.media.errors import MediaError

    owner = MediaOwner(entry="qq", session_id="classified")
    first = await materials.put(owner=owner, name="first.txt", mime_type="text/plain",
                                data=b"same", category="generated", tags=(" \uff21\uff22\uff23 ",))
    assert first.categories == ("generated",)
    assert set(first.tags) == {"abc", "生成图片"}
    await materials.update_tags(first.id, ())
    repeated = await materials.put(owner=owner, name="renamed.txt", mime_type="text/plain",
                                   data=b"same", description="new", category="sent",
                                   source_key="c" * 64)
    assert repeated.id == first.id and repeated.name == "first.txt" and repeated.description == ""
    assert repeated.categories == ("generated", "sent") and repeated.tags == ()
    assert await materials.describe(first.id, owner=owner) == repeated
    assert await materials.find_source(owner, "c" * 64) == repeated
    async with materials.open_content(first.id, owner=owner) as file:
        assert file.read() == b"same"
    await materials.update_tags(first.id, tuple(f"主题{i}" for i in range(8)))
    received = await materials.put(owner=owner, name="same", mime_type="text/plain",
                                  data=b"same", category="received")
    assert received.categories == ("generated", "received", "sent")
    assert len(received.tags) == 8
    with pytest.raises(MediaError, match="MATERIAL_INVALID_TAGS"):
        await materials.put(owner=owner, name="same", mime_type="text/plain", data=b"same",
                            category="uploaded", tags=("第九个",))
    # 标签并集失败也要回滚刚插入的新分类，保留旧原件和完整标签。
    assert await materials.describe(first.id, owner=owner) == received


@pytest.mark.asyncio
async def test_add_category_uses_owner_and_preserves_full_tags_without_file_io(
    materials, monkeypatch,
):
    """旧素材追加来源仅改元数据，八个标签不阻塞流转，也不放宽其他会话权限。"""
    from agent_bot.media.errors import MediaError

    owner = MediaOwner(entry="qq", session_id="legacy-category")
    asset = await materials.put(owner=owner, name="old.txt", mime_type="text/plain", data=b"old",
                                tags=tuple(f"主题{i}" for i in range(8)))
    assert asset.categories == ()

    def forbid_file_io(*_args, **_kwargs):
        """分类登记不得读取、重写或重新发布素材原件。"""
        raise AssertionError("category registration must not access original bytes")

    monkeypatch.setattr(materials.files, "open", forbid_file_io)
    monkeypatch.setattr(materials.files, "write", forbid_file_io)
    received = await materials.add_category(asset.id, "received", owner=owner)
    assert received == asset.model_copy(update={"categories": ("received",)})
    assert await materials.add_category(asset.id, "received", owner=owner) == received
    sent = await materials.add_category(asset.id, "sent", owner=owner)
    assert sent.categories == ("received", "sent") and sent.tags == asset.tags
    for foreign in (MediaOwner(entry="qq", session_id="other"),
                    MediaOwner(entry="admin", session_id=owner.session_id)):
        with pytest.raises(MediaError, match="MATERIAL_NOT_FOUND"):
            await materials.add_category(asset.id, "uploaded", owner=foreign)
    assert await materials.describe(asset.id, owner=owner) == sent


@pytest.mark.asyncio
async def test_add_category_failure_rolls_back_existing_metadata(materials, material_database):
    """分类写入失败恢复原完整元数据，不能留下已发送标记或丢失八个标签。"""
    from agent_bot.ai.errors import AiError

    params, _pool = material_database
    owner = MediaOwner(entry="qq", session_id="category-transaction")
    asset = await materials.put(owner=owner, name="old.txt", mime_type="text/plain", data=b"old",
                                tags=tuple(f"主题{i}" for i in range(8)))
    received = await materials.add_category(asset.id, "received", owner=owner)
    with sqlite3.connect(params.sqlite_path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_sent_category AFTER INSERT ON chat_material_category "
            "WHEN NEW.category='sent' BEGIN SELECT RAISE(ABORT, 'offline failure'); END"
        )
    with pytest.raises(AiError):
        await materials.add_category(asset.id, "sent", owner=owner)
    assert await materials.describe(asset.id, owner=owner) == received


@pytest.mark.asyncio
async def test_metadata_filters_share_count_pages_and_keep_owner_boundary(materials):
    """多分类多标签不重复计数，标签规范和分页对齐，模型查询仍绑定当前会话。"""
    from agent_bot.materials.contracts import MaterialLibraryArguments, MaterialSearchArguments
    from agent_bot.media.errors import MediaError

    owner = MediaOwner(entry="qq", session_id="filter")
    ids = []
    for index in range(3):
        asset = await materials.put(owner=owner, name=f"{index}.txt", mime_type="text/plain",
                                    data=str(index).encode(), category="generated", tags=("Topic",))
        await materials.put(owner=owner, name="again", mime_type="text/plain",
                            data=str(index).encode(), category="sent", tags=("second",))
        ids.append(asset.id)
    await materials.put(owner=MediaOwner(entry="qq", session_id="other"), name="foreign",
                        mime_type="text/plain", data=b"private", category="sent", tags=("topic",))
    args = MaterialLibraryArguments(
        category="sent", tag=" \uff34\uff2f\uff30\uff29\uff23 ",
        session_id="filter", limit=2,
    )
    assert await materials.count_library(args) == 3
    first = await materials.list_library(args, page=1)
    second = await materials.list_library(args, page=2)
    assert {item.id for item in first.items + second.items} == set(ids)
    assert len(first.items) == 2 and len(second.items) == 1
    assert all(item.categories == ("generated", "sent") for item in first.items + second.items)
    result = await materials.search(owner, MaterialSearchArguments(category="sent", tag="TOPIC"))
    assert {item.id for item in result.records} == set(ids)
    assert all(set(item.tags) == {"topic", "second", "生成图片"} for item in result.records)
    with pytest.raises(MediaError, match="MATERIAL_INVALID_PAGE"):
        await materials.list_library(
            args.model_copy(update={"tag": "second"}), cursor=first.next_cursor,
        )


@pytest.mark.asyncio
async def test_tag_replacement_failure_rolls_back_full_group(materials, material_database):
    """真实 SQLite 插入约束失败发生在删除旧组后，事务仍恢复完整旧标签与分类。"""
    from agent_bot.ai.errors import AiError

    params, _pool = material_database
    owner = MediaOwner(entry="admin", session_id="tags")
    asset = await materials.put(owner=owner, name="a.txt", mime_type="text/plain", data=b"a",
                                category="uploaded", tags=("before",))
    with sqlite3.connect(params.sqlite_path) as connection:
        connection.execute("CREATE TRIGGER reject_material_tag BEFORE INSERT ON chat_material_tag "
                           "WHEN NEW.tag='reject' BEGIN "
                           "SELECT RAISE(ABORT, 'offline failure'); END")
    with pytest.raises(AiError):
        await materials.update_tags(asset.id, ("accepted", "reject"))
    assert await materials.describe(asset.id, owner=owner) == asset
    updated = await materials.update_tags(asset.id, (" \uff21 ", "a", "第二"))
    assert updated.tags == ("a", "第二") and updated.categories == ("uploaded",)
    assert (await materials.update_tags(asset.id, ())).tags == ()


@pytest.mark.parametrize("tags", [("",), ("x" * 33,), ("a\x00",), ("a\u200b",),
                                   tuple(str(i) for i in range(9))])
def test_material_tag_contract_rejects_invalid_full_group(tags):
    """管理输入拒绝超额、空白及控制字符主题，模型筛选使用相同单标签规范。"""
    from pydantic import ValidationError

    from agent_bot.materials.contracts import MaterialSearchArguments, MaterialTagsUpdate

    with pytest.raises(ValidationError):
        MaterialTagsUpdate(tags=tags)
    if len(tags) == 1:
        # 空字符串是筛选的缺省值；管理替换组中的空项仍属于无效标签。
        if tags[0]:
            with pytest.raises(ValidationError):
                MaterialSearchArguments(tag=tags[0])


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
    fresh = sqlite3.connect(tmp_path / "fresh.db")
    upgraded = sqlite3.connect(tmp_path / "upgraded.db")
    try:
        fresh.executescript(full)
        upgraded.executescript(full)
        # 旧素材迁移曾独立创建记忆关联表；当前最终结构把它合入事实表。
        upgraded.execute("DROP TABLE chat_material_source")
        upgraded.execute("DROP TABLE chat_material")
        upgraded.executescript(migration)
        upgraded.execute("CREATE TABLE chat_memory_scope (scope_id TEXT PRIMARY KEY,scope TEXT)")
        upgraded.execute("CREATE TABLE chat_memory_tag (memory_id INTEGER,tag TEXT)")
        upgraded.executescript(Path(
            "sql/2026-10-20-retire-legacy-tagged-memory.sqlite.sql"
        ).read_text(encoding="utf-8"))
        for table in ("chat_material", "chat_material_source"):
            for pragma in ("table_info", "index_list", "foreign_key_list"):
                assert fresh.execute(f"PRAGMA {pragma}({table})").fetchall() == (
                    upgraded.execute(f"PRAGMA {pragma}({table})").fetchall()
                )
    finally:
        fresh.close()
        upgraded.close()


@pytest.mark.asyncio
async def test_material_get_reads_bounded_utf8_without_private_metadata(materials):
    """文本预览按字符截断，原件位置和其他会话资料不能进入工具结果。"""
    import json

    from agent_bot.agent.contracts import ToolCall
    from agent_bot.agent.tools.materials import MaterialGetTool
    from agent_bot.agent.tools.schema import ToolSchema
    from agent_bot.media.contracts import MediaScope
    from agent_bot.media.errors import MediaError

    owner = MediaOwner(entry="qq", session_id="material-owner")
    asset = await materials.put(owner=owner, name="长文.txt", mime_type="text/plain",
                                data=("文" * 8001).encode())
    scope = MediaScope(owner=owner, execution_id="a" * 32, execution_kind="agent")
    tool = MaterialGetTool(materials, scope)
    ToolSchema.validate_definition(tool.definition)
    call = ToolCall(call_id="get-1", name="material_get", arguments={"id": asset.id})
    result = await tool.call(call)
    body = json.loads(result.content)
    assert body["text"] == "文" * 8000
    assert body["truncated"] is True
    assert body["material"]["id"] == asset.id
    assert not result.images and not result.attachments
    assert "material-owner" not in result.content and "storage_key" not in result.content
    assert str(materials.files.root) not in result.content
    foreign = scope.model_copy(update={"owner": MediaOwner(entry="qq", session_id="other")})
    with pytest.raises(MediaError, match="MATERIAL_NOT_FOUND"):
        await MaterialGetTool(materials, foreign).call(call)


@pytest.mark.asyncio
async def test_material_get_binary_and_invalid_utf8_are_metadata_only(materials):
    """二进制及非法 UTF-8 不返回损坏正文，也不把内容编码进模型结果。"""
    import json

    from agent_bot.agent.contracts import ToolCall
    from agent_bot.agent.tools.materials import MaterialGetTool
    from agent_bot.media.contracts import MediaScope

    owner = MediaOwner(entry="qq", session_id="a")
    scope = MediaScope(owner=owner, execution_id="a" * 32, execution_kind="agent")
    for index, mime in enumerate(("application/octet-stream", "text/plain")):
        asset = await materials.put(owner=owner, name="binary", mime_type=mime,
                                    data=b"\xff\x00private-content" + bytes([index]))
        result = await MaterialGetTool(materials, scope).call(ToolCall(
            call_id="binary", name="material_get", arguments={"id": asset.id},
        ))
        body = json.loads(result.content)
        assert body["text"] is None and body["truncated"] is False
        assert "private-content" not in result.content


@pytest.mark.asyncio
async def test_material_get_image_is_separate_qq_output_without_vision(materials):
    """历史原图进入请求图片收集器，不进入模型视觉输入或序列化日志。"""
    import base64
    from io import BytesIO

    from PIL import Image

    from agent_bot.agent.contracts import ToolCall
    from agent_bot.agent.tools.materials import MaterialGetTool
    from agent_bot.media.contracts import MediaScope
    from agent_bot.services.tool_images import capture_tool_images

    output = BytesIO()
    Image.new("RGB", (2, 2), "red").save(output, "PNG")
    raw = output.getvalue()
    owner = MediaOwner(entry="qq", session_id="a")
    asset = await materials.put(owner=owner, name="image.png", mime_type="image/png", data=raw)
    scope = MediaScope(owner=owner, execution_id="a" * 32, execution_kind="agent")
    with capture_tool_images() as captured:
        result = await MaterialGetTool(materials, scope).call(ToolCall(
            call_id="image", name="material_get", arguments={"id": asset.id},
        ))
    assert len(captured.items) == 1
    assert base64.b64decode(captured.items[0].data) == raw
    assert result.images == () and result.attachments == ()
    assert base64.b64encode(raw).decode() not in result.model_dump_json()


@pytest.mark.asyncio
async def test_material_get_admin_image_publishes_attachment_in_current_scope(
    materials, material_database, tmp_path,
):
    """管理附件属于本次运行及当前管理会话，QQ 收集器不会接收管理图片。"""
    from io import BytesIO

    from PIL import Image

    from agent_bot.agent.contracts import ToolCall
    from agent_bot.agent.tools.materials import MaterialGetTool
    from agent_bot.media.contracts import MediaScope
    from agent_bot.media.errors import MediaError
    from agent_bot.media.store import MediaStore
    from agent_bot.services.tool_images import capture_tool_images

    output = BytesIO()
    Image.new("RGB", (2, 2), "blue").save(output, "PNG")
    raw = output.getvalue()
    owner = MediaOwner(entry="admin", session_id="admin_a")
    asset = await materials.put(owner=owner, name="image.png", mime_type="image/png", data=raw)
    scope = MediaScope(owner=owner, execution_id="b" * 32, execution_kind="agent")
    params, pool = material_database
    files = MediaFiles(tmp_path / "attachments")
    files.start()
    store = MediaStore(params, connect=pool.connect, files=files)
    try:
        with capture_tool_images() as captured:
            result = await MaterialGetTool(materials, scope, store).call(ToolCall(
                call_id="image", name="material_get", arguments={"id": asset.id},
            ))
        assert len(result.attachments) == 1 and not result.images and not captured.items
        attachment = result.attachments[0]
        assert (await store.list_execution(scope)) == (attachment,)
        async with store.open_content(attachment.id, owner=owner) as file:
            assert file.read() == raw
        with pytest.raises(MediaError, match="MEDIA_NOT_FOUND"):
            await store.describe(attachment.id, owner=MediaOwner(entry="admin", session_id="other"))
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_material_get_escaped_text_respects_tool_result_limit(materials):
    """转义字符和长元数据不能让合法预览超出工具结果契约。"""
    import json

    from agent_bot.agent.contracts import ToolCall
    from agent_bot.agent.tools.materials import MaterialGetTool
    from agent_bot.media.contracts import MediaScope

    owner = MediaOwner(entry="qq", session_id="a")
    asset = await materials.put(
        owner=owner, name="\x01" * 255, mime_type="text/plain", data=b"\x01" * 8000,
        description="\x01" * 1000,
    )
    scope = MediaScope(owner=owner, execution_id="a" * 32, execution_kind="agent")
    result = await MaterialGetTool(materials, scope).call(ToolCall(
        call_id="escaped", name="material_get", arguments={"id": asset.id},
    ))
    body = json.loads(result.content)
    assert len(result.content) <= 32000 and 0 < len(body["text"]) <= 8000
    assert body["truncated"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["qq", "admin"])
@pytest.mark.parametrize("configured,authorized", [(True, True), (False, True), (True, False)])
async def test_material_get_runtime_intersects_preset_and_entry_permissions(
    materials, entry, configured, authorized,
):
    """只依赖可信当前作用域与预设和入口权限交集，模型不能指定另一会话。"""
    import json
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from agent_bot.agent.contracts import AgentRequest, ExecutionAuthority, ToolCall
    from agent_bot.agent.errors import AgentError
    from agent_bot.agent.tools.runtime import ToolRuntime
    from agent_bot.media.contracts import MediaScope

    class EmptyConnector:
        """离线无 MCP 的工具作用域，不替换生产权限或存储实现。"""

        @asynccontextmanager
        async def open(self, *_args, **_kwargs):
            """不启动外部连接，保持运行时真实的工具选择流程。"""
            yield SimpleNamespace(tools=(), warnings=())

    owner = MediaOwner(entry=entry, session_id="current")
    asset = await materials.put(owner=owner, name="a.txt", mime_type="text/plain", data=b"actual")
    scope = MediaScope(owner=owner, execution_id="a" * 32, execution_kind="agent")
    request = AgentRequest(prompt="读取", session_id=owner.session_id, request_id="read")
    snapshot = SimpleNamespace(
        mcps=(), search_mcp=None,
        agent=SimpleNamespace(tools=("material_get",) if configured else ()),
    )
    runtime = ToolRuntime(EmptyConnector(), material_store=materials)
    if entry == "admin":
        from agent_bot.admin.agent_chat import AgentChatProbe

        admin_runtime = SimpleNamespace(agent_service=SimpleNamespace(tool_runtime=runtime))
        authority = AgentChatProbe(admin_runtime).authority()
        if not authorized:
            authority = authority.model_copy(update={"allowed_tools": ()})
    else:
        authority = ExecutionAuthority(
            entry=entry, allowed_tools=("material_get",) if authorized else (),
        )
    call = ToolCall(call_id="read", name="material_get", arguments={"id": asset.id})
    async with runtime.open(snapshot, request, authority, media_scope=scope) as bound:
        if configured and authorized:
            assert json.loads((await bound.call(call)).content)["text"] == "actual"
            for arguments in ({"id": "../private"}, {"id": asset.id, "session_id": "other"}):
                with pytest.raises(AgentError, match="AGENT_TOOL_ARGUMENTS_INVALID"):
                    bound.validate(call.model_copy(update={"arguments": arguments}))
        else:
            with pytest.raises(AgentError, match="AGENT_TOOL_UNAUTHORIZED"):
                await bound.call(call)
