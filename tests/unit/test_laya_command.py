"""Laya 命令的上下文、专用请求与完整报告投递契约。"""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import httpx
import nonebot
import pytest
from nonebot.adapters.onebot.v11 import (
    Adapter,
    Bot,
    GroupMessageEvent,
    Message,
    PrivateMessageEvent,
)
from nonebot.adapters.onebot.v11.event import Reply, Sender
from nonebot.exception import FinishedException
from nonebug import App
from pydantic import ValidationError

from agent_bot.integrations.dispatch_ai import DispatchAiClient
from agent_bot.integrations.mcp import ChatMcpServer
from agent_bot.integrations.onebot.tooling import ChatToolSessionLease
from agent_bot.model_router import ContentModelRouter
from agent_bot.models import DispatchAiRequest
from agent_bot.services.chat import ChatService
from agent_bot.services.chat_tools import build_chat_tool_registry
from agent_bot.services.conversation_queue import ConversationJob
from agent_bot.services.tool_sessions import ToolSessionExpiredError, ToolSessionStore
from agent_bot.state import ConversationStore


def laya_event(argument: str, *, group: bool):
    """构造能被真实命令 Matcher 解析的群聊或私聊事件。"""
    text = f"/laya {argument}".strip()
    event_type = GroupMessageEvent if group else PrivateMessageEvent
    payload = {
        "time": 1, "self_id": 90001, "post_type": "message", "message_id": 1,
        "user_id": 10001, "message": Message(text), "raw_message": text, "font": 0,
        "sender": {"user_id": 10001, "nickname": "tester"},
        "message_type": "group" if group else "private",
        "sub_type": "normal" if group else "friend",
    }
    if group:
        payload["group_id"] = 20001
    return event_type.model_validate(payload)


@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("argument", ["", "判断是否应该退款", "画图一只猫是否适合这个方案"])
async def test_laya_matcher_enqueues_context_without_media_or_other_tools(
    app: App, chat_plugin, monkeypatch, group: bool, argument: str,
) -> None:
    """命令不依赖自然聊天触发词，携带引用和群上下文且只创建一个决策任务。"""
    monkeypatch.setattr(chat_plugin.plugin_config, "dispatch_chat_mcp_enabled", True)
    monkeypatch.setattr(chat_plugin.plugin_config, "dispatch_chat_laya_enabled", True)
    monkeypatch.setattr(
        chat_plugin._event_context, "recent_group_context_for_request",
        AsyncMock(return_value="群里讨论重复扣款" if group else None),
    )
    event = laya_event(argument, group=group)
    event.reply = Reply(
        time=1, message_type="group" if group else "private", message_id=9,
        real_id=9, sender=Sender(user_id=10001, nickname="tester"),
        message=Message("同一订单扣了两次款，请退款"),
    )
    async with app.test_matcher(chat_plugin.laya_command) as ctx:
        bot = ctx.create_bot(base=Bot, adapter=nonebot.get_adapter(Adapter), self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_finished(chat_plugin.laya_command)

    chat_plugin.conversation_queue.enqueue.assert_awaited_once()
    job = chat_plugin.conversation_queue.enqueue.await_args.args[0]
    assert job.laya_decision is True
    assert job.chat_tools == ()
    assert job.media_output is None
    assert job.chat_context.sender_id == "10001"
    assert "同一订单扣了两次款" in job.prompt
    assert "/laya" not in job.prompt
    assert job.recent_group_context == ("群里讨论重复扣款" if group else None)
    assert ConversationJob.model_validate_json(job.model_dump_json()).laya_decision is True


async def test_disabled_laya_command_uses_llm_explanation(chat_plugin, monkeypatch) -> None:
    """未启用功能时仍由统一回复模型解释，不入队也不使用固定正文。"""
    monkeypatch.setattr(chat_plugin.plugin_config, "dispatch_chat_laya_enabled", False)
    compose = AsyncMock(return_value="模型生成的 Laya 未启用说明")
    monkeypatch.setattr(chat_plugin.chat_service, "generate_reply_queued", compose)
    matcher = Mock(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException))
    with pytest.raises(FinishedException):
        await chat_plugin._conversation_handler.laya_command(
            Mock(self_id="90001"), laya_event("退款", group=False), Message("退款"),
            matcher=matcher,
        )
    matcher.send.assert_awaited_once_with(Message("模型生成的 Laya 未启用说明"))
    assert "Laya" in compose.await_args.kwargs["instruction"]
    chat_plugin.conversation_queue.enqueue.assert_not_awaited()


def decision_job() -> ConversationJob:
    """构造没有附加 Chat 工具的持久化决策命令。"""
    return ConversationJob(
        jobId="laya-job", requestId="laya-request", createdAt=1_800_000_000,
        sessionId="private:10001", botId="90001", scene="private", userId="10001",
        prompt="判断应该退款还是补发", recentGroupContext="订单重复扣款", layaDecision=True,
    )


async def test_laya_queue_uses_llm_history_and_preserves_entire_report(
    chat_plugin, monkeypatch,
) -> None:
    """从持久队列到实际 HTTP 的模式和上下文不丢失，超出普通聊天长度的报告完整分段。"""
    report = "目标：处理重复扣款。\n决策：退款，概率 0.95；补发，概率 0.05。\n" * 40
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """模拟模型已调用 Laya 后形成的最终报告。"""
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.url.path.endswith("/chat/chatAgentOne")
        assert payload["layaDecision"] is True
        assert payload["mcpServices"] == ["laya"]
        assert payload["tools"] == []
        assert payload["toolSessionToken"] == "a" * 40
        assert payload["maxSteps"] == 1
        assert payload["model"] == "chat-model"
        context = "\n".join(message["content"] for message in payload["messages"])
        assert "历史订单信息" in context and "订单重复扣款" in context
        return httpx.Response(200, json={"code": 200, "data": {"replayContext": report}})

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai", timeout_seconds=3,
        laya_enabled=True, web_search_max_steps=1, transport=httpx.MockTransport(handler),
    )
    store = ConversationStore(history_turns=2, max_sessions=10, ttl_seconds=60)
    store.commit_exchange("private:10001", "历史订单信息", "已知订单背景")
    service = ChatService(
        client=client, store=store, system_prompt="配置人设", max_input_chars=2000,
        cooldown_seconds=0, max_concurrent_requests=2,
        model_router=ContentModelRouter(
            text_model="chat-model", vision_model="vision",
            image_model="image", audio_model="audio",
        ),
    )

    @asynccontextmanager
    async def session(_job):
        """隔离 MCP 连接，仅替代短期授权令牌。"""
        yield ChatToolSessionLease(tools=(), token="a" * 40)

    monkeypatch.setattr(chat_plugin, "chat_service", service)
    monkeypatch.setattr(chat_plugin, "_open_queued_tool_session", session)
    monkeypatch.setattr(chat_plugin.plugin_config, "dispatch_chat_max_reply_chars", 200)
    monkeypatch.setattr(chat_plugin.plugin_config, "dispatch_chat_chunk_chars", 100)
    try:
        result = await chat_plugin._ConversationProcessor().generate(decision_job())
    finally:
        await service.aclose()
        await client.aclose()
    assert len(requests) == 1
    assert result.kind == "TEXT"
    assert result.reply.text == report.strip()
    assert "".join(result.delivery_parts) == report.strip()
    assert all(len(part) <= 100 for part in result.delivery_parts)
    assert "layaDecision" not in store.context("private:10001").messages[-1].content


async def test_laya_only_session_has_revocable_token(chat_plugin, monkeypatch) -> None:
    """不借用其他工具授权，空工具的决策会话也必须创建并撤销短期令牌。"""
    monkeypatch.setattr(chat_plugin.plugin_config, "dispatch_chat_mcp_enabled", True)
    monkeypatch.setattr(chat_plugin.plugin_config, "dispatch_chat_laya_enabled", True)
    manager = chat_plugin._tool_session_manager
    store = ToolSessionStore(ttl_seconds=30, max_sessions=3)
    monkeypatch.setattr(manager, "_store_provider", lambda: store)
    monkeypatch.setattr(manager, "queued_bot", lambda _: Mock(self_id="90001"))
    async with manager.open_queued_session(decision_job()) as session:
        assert session.tools == ()
        assert session.token
        context = await store.resolve(session.token)
        assert not context.allowed_tools
        server = ChatMcpServer(
            store=store, registry=build_chat_tool_registry(), allowed_hosts=["localhost"],
        )
        assert (await server.list_tools(session.token)).tools == []
    with pytest.raises(ToolSessionExpiredError):
        await server.list_tools(session.token)


@pytest.mark.parametrize("overrides", [
    {"toolSessionToken": None}, {"mcpServices": None}, {"tools": ["tts_generate"]},
])
def test_laya_decision_request_requires_own_authorization(overrides) -> None:
    """决策模式必须显式授权 Laya 和会话，禁止混入有副作用的 Chat 工具。"""
    payload = {
        "prompt": "退款", "model": "model", "sessionId": "s", "requestsId": "r",
        "messages": [], "layaDecision": True, "mcpServices": ["laya"],
        "tools": [], "toolSessionToken": "a" * 40,
    }
    DispatchAiRequest.model_validate(payload)
    with pytest.raises(ValidationError):
        DispatchAiRequest.model_validate({**payload, **overrides})
