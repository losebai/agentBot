"""独立模型的单次自动发言判断，不访问真实供应商或会话记忆。"""

import asyncio
import json

import httpx
import pytest
from tests.ai.test_service import make_service

from agent_bot.ai.call_records import CallRecordStore
from agent_bot.ai.errors import AiError


@pytest.mark.parametrize("choice", ["speak", "silent"])
async def test_llm_decision_is_fresh_ordinary_call(catalog, ai_params, ai_database, choice):
    """两次判断只发送独立指令，外部状态不能成为模型输入或会话历史。"""
    from agent_bot.ai.llm_auto_speak import LlmAutoSpeakDecisionService

    requests = []

    def respond(request):
        """拦截普通文本协议，不允许外部网络访问。"""
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "model": "exact/model", "done": True,
            "message": {"role": "assistant", "content": json.dumps({"choice": choice})},
        })

    ordinary = await make_service(catalog, ai_params, ai_database, respond)
    client = LlmAutoSpeakDecisionService(ordinary, model="local", prompt="独立决策提示词")
    try:
        for index in range(2):
            decision = await client.decide_auto_speak(
                state=f"当次输入{index}", session_id="group-session",
                request_id=f"auto-decision-{index}",
            )
            assert decision.should_speak is (choice == "speak")
        for request in requests:
            assert request["model"] == "exact/model"
            assert [item["role"] for item in request["messages"]] == ["system", "user"]
            assert request["messages"][1]["content"] == "独立决策提示词"
            assert "当次输入" not in json.dumps(request, ensure_ascii=False)
        records = CallRecordStore(ai_params, connect=ai_database.connect)
        page = await records.list_calls(category="system", system_kind="auto_speak")
        assert len(page.items) == 2
        assert all(row.type == "auto_speak" and row.model == "exact/model" for row in page.items)
        assert not (await records.list_calls(category="conversation")).items
        for row in page.items:
            detail = await records.get_call(row.id)
            assert detail.content == "独立决策提示词"
            archived = json.dumps(detail.model_dump(), ensure_ascii=False, default=str)
            assert "当次输入" not in archived
        saved = await ordinary.sessions.list_records(session_id="group-session")
        detail = await ordinary.sessions.get_record(saved.items[0].execution_id)
        assert detail.prompt == "独立决策提示词"
    finally:
        await ordinary.aclose()


@pytest.mark.parametrize("content", [
    'speak', '```json\n{"choice":"speak"}\n```', '{"choice":true}',
    '{"choice":"SPEAK"}', '{"choice":"speak","reason":"理由"}',
    '[{"choice":"speak"}]', '{"choice":"silent","choice":"speak"}',
])
async def test_invalid_llm_decision_never_grants_permission(
    catalog, ai_params, ai_database, content,
):
    """拒绝解释文本、强制转换及歧义结构，不再调用其他模型恢复结果。"""
    from agent_bot.ai.llm_auto_speak import LlmAutoSpeakDecisionService

    calls = []

    def respond(request):
        """错误输出仍是合法普通模型响应，由决策协议明确拒绝。"""
        calls.append(request)
        return httpx.Response(200, json={
            "model": "exact/model", "done": True,
            "message": {"role": "assistant", "content": content},
            "prompt_eval_count": 3, "eval_count": 2,
        })

    ordinary = await make_service(catalog, ai_params, ai_database, respond)
    try:
        with pytest.raises(AiError, match="AUTO_SPEAK_RESPONSE_INVALID"):
            await LlmAutoSpeakDecisionService(
                ordinary, model="local", prompt="独立判断",
            ).decide_auto_speak(state="本次输入", session_id="s", request_id="auto-decision-bad")
        assert len(calls) == 1
        page = await CallRecordStore(ai_params, connect=ai_database.connect).list_calls(
            category="system", system_kind="auto_speak", status="failed",
        )
        assert len(page.items) == 1
        assert page.items[0].model == "exact/model"
        assert page.items[0].usage.prompt_tokens == 3
        assert page.items[0].usage.completion_tokens == 2
        detail = await CallRecordStore(ai_params, connect=ai_database.connect).get_call(
            page.items[0].id,
        )
        assert detail.replay_context == content
    finally:
        await ordinary.aclose()


async def test_cancelled_llm_decision_releases_ordinary_request(catalog, ai_params, ai_database):
    """取消经普通服务清理并记录，不能吞掉取消后继续发言。"""
    from agent_bot.ai.llm_auto_speak import LlmAutoSpeakDecisionService

    entered = asyncio.Event()

    async def pause(request):
        """停在离线传输等待取消。"""
        entered.set()
        await asyncio.Event().wait()

    ordinary = await make_service(catalog, ai_params, ai_database, pause)
    client = LlmAutoSpeakDecisionService(ordinary, model="local", prompt="独立判断")
    task = asyncio.create_task(client.decide_auto_speak(
        state="本次输入", session_id="s", request_id="auto-decision-cancel",
    ))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ordinary.active_requests == 0
        page = await CallRecordStore(ai_params, connect=ai_database.connect).list_calls(
            category="system", status="cancelled",
        )
        assert len(page.items) == 1
    finally:
        await ordinary.aclose()
