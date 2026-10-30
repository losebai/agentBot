"""主动文件工具在隔离素材库和 OneBot 替身上验证真实发送、授权与幂等。"""

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from agent_bot.integrations.onebot.replies import OneBotReplyGateway
from agent_bot.integrations.sqlite_queue_database import initialize_queue_database
from agent_bot.integrations.sqlite_tool_operations import SqliteToolOperationStore
from agent_bot.integrations.tool_operations import RedisToolOperationStore
from agent_bot.materials.contracts import MaterialSearchArguments
from agent_bot.media.contracts import MediaOwner
from agent_bot.services.chat_tools import ChatToolContext, ChatToolError, build_chat_tool_registry
from materials.test_store import material_database as material_database
from materials.test_store import materials as materials


@pytest.fixture(params=["sqlite", "redis"])
async def operations(request, tmp_path):
    """双后端都执行真实占位与回执逻辑，不使用生产连接。"""
    if request.param == "sqlite":
        initialize_queue_database(str(tmp_path / "effects.sqlite3"))
        yield SqliteToolOperationStore(str(tmp_path / "effects.sqlite3"),
                                       key_prefix="files", ttl_seconds=60)
        return
    async with fakeredis.aioredis.FakeRedis(decode_responses=True) as redis:
        yield RedisToolOperationStore(redis, key_prefix="files", ttl_seconds=60)


def context(materials, operations, *, group=True, library=False):
    """实际网关绑定原群或私聊，模型不能指定接收人。"""
    bot = SimpleNamespace(self_id="90001", call_api=AsyncMock(return_value={"file_id": "f"}))
    gateway = OneBotReplyGateway(lambda: SimpleNamespace())
    value = ChatToolContext(
        session_id="destination", user_id="10001", bot=bot, bot_user_id="90001",
        group_id=12345 if group else None, allowed_tools=frozenset({"file_send"}),
        material_store=materials, material_library_access=library,
        job_id="job", operation_store=operations,
        file_sender=gateway.send_file,
    )
    return value, bot


async def call(context, **arguments):
    """注册表与参数校验都走真实路径。"""
    result = await build_chat_tool_registry().call("file_send", arguments, context)
    return json.loads(result.content)


@pytest.mark.parametrize("group", [False, True])
async def test_text_file_is_sent_to_current_chat_with_exact_utf8(materials, operations, group):
    """短文本也可主动作为文件发送，不能裁剪缩进、空白行或末尾换行。"""
    current, bot = context(materials, operations, group=group)
    text = "  第一行\n\nprint('你好')\n"
    result = await call(current, name="示例.py", content=text)
    assert result["status"] == "sent" and result["name"] == "示例.py"
    args = bot.call_api.await_args
    assert args.args == ("upload_group_file" if group else "upload_private_file",)
    assert args.kwargs["group_id" if group else "user_id"] == (12345 if group else 10001)
    assert args.kwargs["name"] == "示例.py"
    assert base64.b64decode(args.kwargs["file"].removeprefix("base64://")) == text.encode()
    saved = await materials.describe(result["material_id"], owner=MediaOwner(
        entry="qq", session_id="destination",
    ))
    assert "generated" in saved.categories and "sent" in saved.categories


async def test_multiple_distinct_files_and_repeated_call_reuse_receipt(materials, operations):
    """同任务同参数复用结果，任务可以继续发送另一个文件而不重复第一份。"""
    first, bot = context(materials, operations)
    sent = await call(first, name="一.txt", content="第一份")
    retry, _ = context(materials, operations)
    retry.bot = bot
    assert await call(retry, name="一.txt", content="第一份") == sent
    second = await call(retry, name="二.txt", content="第二份")
    assert second["material_id"] != sent["material_id"]
    assert bot.call_api.await_count == 2


@pytest.mark.parametrize("library,entry,allowed", [
    (False, "qq", False), (True, "qq", True), (True, "admin", False),
])
async def test_file_source_keeps_material_access_boundary(
    materials, operations, library, entry, allowed,
):
    """只使用宿主真实主人授权跨QQ读取；知道ID不能读取管理端或其他会话。"""
    source = await materials.put(owner=MediaOwner(entry=entry, session_id="other"),
                                 name="报告.pdf", mime_type="application/pdf", data=b"%PDF-1.7\n")
    current, bot = context(materials, operations, library=library)
    if not allowed:
        with pytest.raises(ChatToolError) as rejected:
            await call(current, material_id=source.id)
        assert rejected.value.code == "MATERIAL_NOT_FOUND"
        bot.call_api.assert_not_awaited()
        return
    result = await call(current, material_id=source.id)
    assert result["name"] == "报告.pdf"
    assert base64.b64decode(bot.call_api.await_args.kwargs["file"][9:]) == b"%PDF-1.7\n"
    copied = await materials.describe(result["material_id"], owner=MediaOwner(
        entry="qq", session_id="destination",
    ))
    assert copied.mime_type == "application/pdf" and "sent" in copied.categories


async def test_unknown_upload_outcome_is_not_repeated(materials, operations):
    """平台异常不返回成功，同任务恢复不能在结果不明时再次上传。"""
    current, bot = context(materials, operations)
    bot.call_api.side_effect = ConnectionError("private backend detail")
    with pytest.raises(ChatToolError):
        await call(current, name="记录.txt", content="内容")
    retry, _ = context(materials, operations)
    retry.bot = bot
    with pytest.raises(ChatToolError):
        await call(retry, name="记录.txt", content="内容")
    bot.call_api.assert_awaited_once()
    page = await materials.search(MediaOwner(entry="qq", session_id="destination"),
                                  MaterialSearchArguments())
    assert all("sent" not in item.categories for item in page.records)


async def test_file_upload_remains_authorized_through_real_mcp_bridge(materials, operations):
    """文件工具经真实桥接发现和校验；令牌撤销后不能再发送，即使先前已经成功。"""
    from pydantic import SecretStr

    from agent_bot.agent.contracts import ExecutionAuthority, ToolCall
    from agent_bot.agent.errors import AgentError
    from agent_bot.agent.tools.chat import ChatToolBridge
    from agent_bot.integrations.mcp import ChatMcpServer
    from agent_bot.services.tool_sessions import ToolSessionStore

    current, bot = context(materials, operations)
    sessions = ToolSessionStore(ttl_seconds=30, max_sessions=2)
    token = await sessions.create(current)
    bridge = ChatToolBridge(ChatMcpServer(store=sessions, registry=build_chat_tool_registry(),
                                        allowed_hosts=["localhost:*"]))
    authority = ExecutionAuthority(entry="qq", allowed_tools=("file_send",),
                                   chat_token=SecretStr(token))
    request = ToolCall(call_id="file", name="file_send", arguments={
        "name": "说明.md", "content": "# 说明\n正文",
    })
    result = await bridge.call(request, authority)
    assert not result.is_error and json.loads(result.content)["status"] == "sent"
    await sessions.revoke(token)
    with pytest.raises(AgentError, match="AGENT_TOOL_SESSION_EXPIRED"):
        await bridge.call(request, authority)
    bot.call_api.assert_awaited_once()


async def test_file_tool_is_bound_in_direct_and_queued_qq_sessions(
    materials, operations, chat_plugin, monkeypatch,
):
    """平台能力、素材库和令牌门禁共同开放工具，直接与队列上下文均绑定同一可信发送器。"""
    from test_tooling import _private_event

    from agent_bot.integrations.onebot.tooling import ChatToolSessionManager
    from agent_bot.services.conversation_queue import ConversationJob
    from agent_bot.services.tool_sessions import ToolSessionStore

    current, bot = context(materials, operations, group=False)
    sessions = ToolSessionStore(ttl_seconds=30, max_sessions=2)
    config = chat_plugin.plugin_config.model_copy(update={"chat_mcp_enabled": True})
    manager = ChatToolSessionManager(
        config_provider=lambda: config, store_provider=lambda: sessions,
        naraka_service_provider=lambda: None, group_member_refresher=lambda *_: None,
        material_store_provider=lambda: materials, file_sender=current.file_sender,
        operation_store_provider=lambda: operations,
    )
    assert "file_send" in manager.available_tools(_private_event())
    async with manager.open_chat_session(bot, _private_event(), "destination") as lease:
        bound = await sessions.resolve(lease.token)
        assert bound.material_store is materials and bound.file_sender == current.file_sender
    monkeypatch.setattr(manager, "queued_bot", lambda _: bot)
    job = ConversationJob(jobId="job", requestId="qq-file", createdAt=1,
                          sessionId="destination", botId="90001", scene="private",
                          userId="10001", prompt="发文件", chatTools=("file_send",))
    async with manager.open_queued_session(job) as lease:
        bound = await sessions.resolve(lease.token)
        assert bound.material_store is materials and bound.file_sender == current.file_sender
    config = config.model_copy(update={"chat_mcp_enabled": False})
    assert manager.available_tools(_private_event()) == ()


async def test_file_tool_rejects_missing_authority_and_expired_request(materials, operations):
    """已绑定发送器不等于执行授权，禁止绕过工具白名单与原请求期限。"""
    current, bot = context(materials, operations)
    current.allowed_tools = frozenset()
    with pytest.raises(ChatToolError, match="未授权"):
        await call(current, name="说明.txt", content="正文")
    current.allowed_tools = frozenset({"file_send"})
    current.deadline_at = 0
    with pytest.raises(ChatToolError, match="超时"):
        await call(current, name="说明.txt", content="正文")
    bot.call_api.assert_not_awaited()


async def test_files_share_generation_without_colliding_and_preserve_recorded_mime(
    materials, operations,
):
    """真实归档器记录同一Agent请求的多个文件，每份的回执键、素材与原件类型保持独立。"""
    from test_outgoing_context import Journal

    from agent_bot.integrations.onebot.outgoing import DeliveryScope, OutgoingRecorder

    current, bot = context(materials, operations)
    journal = Journal()

    async def scope(request_id):
        """测试只绑定当前会话，不使用真实OneBot事件。"""
        return DeliveryScope("destination", "generation", request_id)

    recorder = OutgoingRecorder(
        service_provider=lambda: None, store_provider=lambda: journal,
        materials_provider=lambda: materials, scope_provider=scope,
        receipts_provider=lambda: operations,
    )
    current.file_sender = OneBotReplyGateway(lambda: None, recorder=recorder).send_file
    source = await materials.put(owner=MediaOwner(entry="qq", session_id="destination"),
                                 name="报告.pdf", mime_type="application/pdf", data=b"%PDF-1.7\n")
    async with recorder.scope("destination", "request", key="generate",
                               generation_id="generation"):
        first = await call(current, name="说明.json", content='{"hello":"你好"}')
        second = await call(current, material_id=source.id)
    assert bot.call_api.await_count == 2 and len(journal.sent) == 2
    assert journal.sent[0].material_ids == (first["material_id"],)
    assert journal.sent[1].material_ids == (second["material_id"],)
    assert journal.sent[0].delivery_id != journal.sent[1].delivery_id
    metadata = await materials.describe(second["material_id"], owner=MediaOwner(
        entry="qq", session_id="destination",
    ))
    assert metadata.mime_type == "application/pdf"


async def test_confirmed_upload_repairs_failed_receipt_without_reupload(
    materials, operations, monkeypatch,
):
    """平台确认后回执写入失败时，同请求可补存结果，不再上传文件。"""
    current, bot = context(materials, operations)
    complete = operations.complete
    attempts = 0

    async def save(*args):
        """只注入一次持久化故障，后续仍使用真实后端保存回执。"""
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary local failure")
        await complete(*args)

    monkeypatch.setattr(operations, "complete", save)
    with pytest.raises(ChatToolError):
        await call(current, name="说明.txt", content="内容")
    result = await call(current, name="说明.txt", content="内容")
    replacement, _ = context(materials, operations)
    replacement.bot = bot
    assert await call(replacement, name="说明.txt", content="内容") == result
    bot.call_api.assert_awaited_once()


@pytest.mark.parametrize("fault", ["group_context", "category", "category_persistent"])
async def test_confirmed_upload_survives_post_upload_recording_error(
    materials, operations, monkeypatch, fault,
):
    """真实归档器在平台确认后失败，工具仍保存已发送回执并明确标记记录告警。"""
    from test_outgoing_context import Journal

    from agent_bot.integrations.onebot.outgoing import DeliveryScope, OutgoingRecorder
    from agent_bot.media.errors import MediaError
    from agent_bot.memory_store import ChatMemoryStoreError

    current, bot = context(materials, operations)
    journal = Journal()

    async def scope(request_id):
        """绑定当前群，不依赖NoneBot网络事件。"""
        return DeliveryScope("destination", "generation", request_id)

    recorder = OutgoingRecorder(
        service_provider=lambda: None, store_provider=lambda: journal,
        materials_provider=lambda: materials, scope_provider=scope,
        receipts_provider=lambda: operations,
        group_message_recorder=(AsyncMock(side_effect=ChatMemoryStoreError("offline"))
                                if fault == "group_context" else None),
    )
    recorder.service_provider = lambda: SimpleNamespace(
        history_generation=AsyncMock(return_value="generation"),
    )
    if fault.startswith("category"):
        add = materials.add_category
        failed = False

        async def add_category(id, category, **kwargs):
            """只在平台确认后的首次分类写入失败，后续可补记而不重传。"""
            nonlocal failed
            if category == "sent" and (not failed or fault == "category_persistent"):
                failed = True
                raise MediaError("AI_DATABASE_UNAVAILABLE")
            return await add(id, category, **kwargs)

        monkeypatch.setattr(materials, "add_category", add_category)
    current.file_sender = OneBotReplyGateway(lambda: None, recorder=recorder).send_file
    result = await call(current, name="说明.txt", content="已经上传的正文")
    assert result["status"] == "sent" and result["record_warning"] is True
    replacement, _ = context(materials, operations)
    replacement.bot = bot
    replacement.file_sender = current.file_sender
    assert await call(replacement, name="说明.txt", content="已经上传的正文") == result
    bot.call_api.assert_awaited_once()


async def test_file_tool_binds_scope_without_inheriting_nonebot_event(materials, operations):
    """HTTP MCP回调不继承事件上下文时，文件仍按令牌绑定的会话正确登记和发送。"""
    from test_outgoing_context import Journal

    from agent_bot.integrations.onebot.outgoing import OutgoingRecorder

    current, bot = context(materials, operations)
    journal = Journal()
    recorder = OutgoingRecorder(
        service_provider=lambda: SimpleNamespace(history_generation=AsyncMock(return_value="gen")),
        store_provider=lambda: journal, materials_provider=lambda: materials,
        scope_provider=AsyncMock(side_effect=RuntimeError("no NoneBot event")),
        receipts_provider=lambda: operations,
    )
    current.file_sender = OneBotReplyGateway(lambda: None, recorder=recorder).send_file
    result = await call(current, name="说明.txt", content="回调发送")
    assert result["status"] == "sent"
    assert len(journal.sent) == 1 and journal.sent[0].session_id == "destination"
    bot.call_api.assert_awaited_once()


@pytest.mark.parametrize("arguments", [
    {}, {"name": "a.txt"}, {"content": "正文"},
    {"name": "a.txt", "content": "正文", "material_id": "a" * 32},
    {"name": "../secret.txt", "content": "正文"},
    {"name": "C:\\secret.txt", "content": "正文"},
    {"name": "CON.txt", "content": "正文"},
    {"name": "a\n.txt", "content": "正文"},
    {"name": "a.txt", "content": "正文", "user_id": "12345"},
    {"name": "a.txt", "path": ".env"},
    {"name": "a.txt", "url": "https://example.test/a"},
])
async def test_invalid_file_arguments_never_read_or_send(materials, operations, arguments):
    """不接受路径、URL、接收人和不明确来源，非法参数不能触发上传。"""
    current, bot = context(materials, operations)
    with pytest.raises(ChatToolError) as rejected:
        await call(current, **arguments)
    assert rejected.value.code == "INVALID_ARGUMENT"
    bot.call_api.assert_not_awaited()
