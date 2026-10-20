"""引用资料的记忆保存保留真实来源，不能冒充本人的偏好。"""

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nonebot.adapters.onebot.v11 import Message
from pydantic import SecretStr
from test_tagged_memory import memory_store as _memory_store

from agent_bot.agent.contracts import ExecutionAuthority, ToolCall
from agent_bot.agent.tools.chat import ChatToolBridge
from agent_bot.integrations.mcp import ChatMcpServer
from agent_bot.multimodal import MultimodalInput
from agent_bot.quoted_summary import build_quoted_chat_input
from agent_bot.services.chat_tools import ChatToolContext, build_chat_tool_registry
from agent_bot.services.tagged_memory import MemoryArguments, MemoryIdentity, MemorySession
from agent_bot.services.tool_sessions import ToolSessionStore

memory_store = _memory_store


@pytest.fixture
def quoted_identity():
    """用户明确要求保存引用日志，原作者和当前请求人分别绑定。"""
    from agent_bot.memory.contracts import QuotedMemorySource

    return MemoryIdentity(
        bot_id="90001", user_id="10001", session_id="session",
        source_text="桃桃，最新的agentBot日志存一下", source_event_id="queue:90001:request",
        quoted_source=QuotedMemorySource(
            text=("被引用者: **v0.5.5 更新日志**\n"
                  "安装指令：irm https://example.test/install.ps1 | iex"),
            event_id="qq:90001:87", user_id="10086",
            time=datetime(2026, 10, 20, tzinfo=timezone.utc),
        ),
    )


def release_arguments(**changes):
    """模型必须逐字摘取引用资料，不能把总结后的正文当成来源。"""
    return MemoryArguments(
        **{
            "action": "save", "scope": "user", "key": "release", "content": "v0.5.5 更新日志",
            "tags": ["agentbot"], "kind": "fact", "source_excerpt": "**v0.5.5 更新日志**",
            "retention_reason": "explicit_request", **changes,
        },
    )


async def test_quoted_release_save_preserves_author_event_and_replay_dedup(
    memory_store, quoted_identity, tmp_path,
):
    """同一引用被再次保存不能虚增佐证，且不能把引用作者记成当前用户。"""
    saved = await MemorySession(memory_store, quoted_identity).execute(release_arguments())
    row = saved.records[0]
    assert row.source_kind == "third_party"
    assert row.scope == "user"
    assert row.sources[0].source_ref == "qq:90001:87"
    assert row.sources[0].excerpt == "**v0.5.5 更新日志**"
    assert (row.sources[0].source_time.replace(tzinfo=timezone.utc)
            == quoted_identity.quoted_source.time)
    again = await MemorySession(memory_store, replace(
        quoted_identity, source_event_id="queue:90001:second",
    )).execute(release_arguments())
    assert again.records[0].id == row.id
    with sqlite3.connect(tmp_path / "memory.db") as connection:
        evidence = connection.execute(
            "SELECT source_actor_id,source_event_id FROM chat_memory_evidence WHERE memory_id=?",
            (row.id,),
        ).fetchall()
    author = replace(quoted_identity, user_id="10086").owner("user", "test-secret")
    assert evidence == [(author, "qq:90001:87")]


@pytest.mark.parametrize("changes", [
    {"kind": "preference", "retention_reason": "stable_preference"},
    {"retention_reason": "reusable_fact"},
    {"source_excerpt": "v0.5.5更新日志"},
])
async def test_quote_requires_exact_fact_excerpt_and_explicit_request(
    memory_store, quoted_identity, changes,
):
    """拒绝改写来源、擅自保留引用以及用群友资料创建本人偏好。"""
    with pytest.raises(ValueError, match=r"引用|逐字"):
        await MemorySession(memory_store, quoted_identity).execute(release_arguments(**changes))


async def test_prompt_markers_cannot_manufacture_a_bound_quote(memory_store):
    """只有服务端绑定的引用可作来源，模型提示词中的引用标记不授予写入权限。"""
    identity = MemoryIdentity(bot_id="90001", user_id="10001", session_id="session",
                              source_text="请保存日志")
    with pytest.raises(ValueError, match="来源片段"):
        await MemorySession(memory_store, identity).execute(release_arguments())


async def test_automatic_job_cannot_write_even_with_a_quote(memory_store, quoted_identity):
    """派生任务没有本人原话时仍只读，携带引用也不能冒充真人指令。"""
    with pytest.raises(ValueError, match="只允许读取"):
        await MemorySession(memory_store, replace(quoted_identity, source_text=None)).execute(
            release_arguments(),
        )


@pytest.mark.parametrize("kind", ["preference", "relationship", "agreement"])
async def test_quote_cannot_confirm_an_existing_personal_memory(
    memory_store, quoted_identity, kind,
):
    """模型即使声明 fact，也不能通过复用原键把引用变成本人偏好或约定的佐证。"""
    own_identity = replace(quoted_identity, source_text="我喜欢清淡", source_event_id="qq:90001:1")
    saved = await MemorySession(memory_store, own_identity).execute(release_arguments(
        key="diet", content="喜欢清淡", kind=kind, source_excerpt="我喜欢清淡",
        retention_reason="stable_preference",
    ))
    quote = quoted_identity.quoted_source.model_copy(update={"text": "小明: 我喜欢清淡"})
    with pytest.raises(ValueError, match="引用不能作为本人偏好"):
        await MemorySession(memory_store, replace(quoted_identity, quoted_source=quote)).execute(
            release_arguments(key="diet", content="喜欢清淡", source_excerpt="我喜欢清淡"),
        )
    after = memory_store.execute(own_identity, MemoryArguments(
        action="recall", scope="user", tags=("agentbot",),
    )).records[0]
    assert after.revision == saved.records[0].revision
    assert after.source_kind == "self_report"
    assert len(after.sources) == 1


async def test_quote_material_matches_prompt_and_queue_roundtrip():
    """模型可见的有界引用正文独立入队，恢复后仍保留平台作者和消息来源。"""
    from agent_bot.integrations.onebot.memory_source import quoted_memory_source
    from agent_bot.services.conversation_queue import ConversationJob

    result = await build_quoted_chat_input(
        AsyncMock(), Message("**v0.5.5 更新日志**"),
        current_input=MultimodalInput(prompt="存一下", image_urls=()),
        quoted_sender="作者", image_recognition_enabled=True,
        max_images=4, max_videos=2, max_prompt_chars=2000,
    )
    assert result.quoted_text in result.prompt
    assert "存一下" not in result.quoted_text
    event = SimpleNamespace(reply=SimpleNamespace(
        message_id=87, time=1, sender=SimpleNamespace(user_id=10086),
    ))
    source = quoted_memory_source("90001", event, result.quoted_text)
    job = ConversationJob(jobId="example", requestId="qq-example", createdAt=2,
                          sessionId="session", botId="90001", userId="10001", scene="private",
                          prompt=result.prompt, memorySourceText="存一下",
                          memoryQuotedSource=source)
    restored = ConversationJob.model_validate_json(job.model_dump_json(by_alias=True))
    assert restored.memory_quoted_source == source
    assert source.event_id == "qq:90001:87"
    assert source.user_id == "10086"


async def test_local_memory_error_is_actionable_and_can_be_corrected(memory_store):
    """桥接层暴露固定来源校验提示，模型纠正逐字片段后可在同一会话保存。"""
    sessions = ToolSessionStore(ttl_seconds=30, max_sessions=2)
    token = await sessions.create(ChatToolContext(
        session_id="session", user_id="10001", allowed_tools=frozenset({"memory"}),
        memory_session=MemorySession(memory_store, MemoryIdentity(
            bot_id="90001", user_id="10001", session_id="session", source_text="我喜欢清淡",
        )),
    ))
    bridge = ChatToolBridge(ChatMcpServer(
        store=sessions, registry=build_chat_tool_registry(), allowed_hosts=["localhost:*"],
    ))
    authority = ExecutionAuthority(
        entry="qq", allowed_tools=("memory",), chat_token=SecretStr(token),
    )
    args = release_arguments(source_excerpt="喜欢清淡饮食", kind="preference",
                             retention_reason="stable_preference").model_dump(mode="json")
    failed = await bridge.call(ToolCall(call_id="bad", name="memory", arguments=args), authority)
    assert failed.is_error
    body = json.loads(failed.content)
    assert body["code"] == "MEMORY_SOURCE_INVALID"
    assert "逐字" in body["message"]
    args["source_excerpt"] = "我喜欢清淡"
    saved = await bridge.call(ToolCall(call_id="fixed", name="memory", arguments=args), authority)
    assert not saved.is_error
    assert json.loads(saved.content)["status"] == "saved"


@pytest.mark.parametrize("human", [True, False])
async def test_queued_tool_session_binds_quote_only_for_human_requests(
    memory_store, quoted_identity, monkeypatch, human,
):
    """真实入队身份恢复引用，自动发言即使携带同一 JSON 也不能获得写权限。"""
    from agent_bot.config import RuntimeConfig
    from agent_bot.integrations.onebot.tooling import ChatToolSessionManager
    from agent_bot.services.conversation_queue import ConversationJob

    config = RuntimeConfig(
        chat_mcp_enabled=True,
        onebot_v11_access_token="onebot-test-token-32-characters-long",
        chat_session_secret="memory-test-secret-32-characters-long",
    )
    sessions = ToolSessionStore(ttl_seconds=30, max_sessions=2)
    manager = ChatToolSessionManager(
        config_provider=lambda: config, store_provider=lambda: sessions,
        naraka_service_provider=lambda: None, group_member_refresher=AsyncMock(),
        memory_store_provider=lambda: memory_store,
    )
    monkeypatch.setattr(manager, "queued_bot", lambda _: SimpleNamespace(self_id="90001"))
    job = ConversationJob(
        jobId="quoted", requestId="qq-quoted", createdAt=1, sessionId="session",
        botId="90001", userId="10001", scene="private", prompt="存一下引用日志",
        memorySourceText=quoted_identity.source_text,
        memoryQuotedSource=quoted_identity.quoted_source, chatTools=("memory",),
        autoSpeakMaxReplyChars=None if human else 100,
    )
    restored = ConversationJob.model_validate_json(job.model_dump_json(by_alias=True))
    async with manager.open_queued_session(restored) as lease:
        memory = (await sessions.resolve(lease.token)).memory_session
        if human:
            saved = await memory.execute(release_arguments())
            assert saved.records[0].sources[0].source_ref == "qq:90001:87"
        else:
            assert memory.identity.quoted_source is None
            with pytest.raises(ValueError, match="只允许读取"):
                await memory.execute(release_arguments())


@pytest.mark.parametrize("code", ["MEMORY_SOURCE_INVALID", "INTERNAL"])
async def test_local_bridge_never_forwards_arbitrary_error_messages(code):
    """可公开的来源错误也只返回固定说明，其他内部异常仍统一遮蔽。"""
    from agent_bot.services.chat_tools import ChatToolDefinition, ChatToolError, ChatToolRegistry

    async def failure(arguments, context):
        """模拟内部服务意外把凭据放入异常消息。"""
        raise ChatToolError(code, "password=private-test-credential")

    registry = ChatToolRegistry([ChatToolDefinition(
        name="memory", description="记忆", input_schema=MemoryArguments.model_json_schema(),
        handler=failure,
    )])
    sessions = ToolSessionStore(ttl_seconds=30, max_sessions=2)
    token = await sessions.create(ChatToolContext(
        session_id="session", user_id="10001", allowed_tools=frozenset({"memory"}),
    ))
    result = await ChatToolBridge(ChatMcpServer(
        store=sessions, registry=registry, allowed_hosts=["localhost:*"],
    )).call(ToolCall(call_id="failure", name="memory", arguments={"action": "tags"}),
            ExecutionAuthority(entry="qq", allowed_tools=("memory",), chat_token=SecretStr(token)))
    assert result.is_error
    assert "private-test-credential" not in result.content
    assert json.loads(result.content)["code"] == (
        "MEMORY_SOURCE_INVALID" if code == "MEMORY_SOURCE_INVALID" else "AGENT_REMOTE_TOOL_FAILED"
    )
