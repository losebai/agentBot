"""自动发言固定选择协议，任何不可信结构都不能产生发言许可。"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp import types
from pydantic import SecretStr
from tests.agent.mcp_server import peer

from agent_bot.agent.contracts import McpView, ToolDefinition
from agent_bot.agent.mcp.client import McpConnector
from agent_bot.agent.mcp.credentials import McpCredentials
from agent_bot.ai.auto_speak import AutoSpeakDecisionService, parse_prediction
from agent_bot.ai.auto_speak_records import AutoSpeakRecords
from agent_bot.ai.errors import AiError
from agent_bot.settings import DatabaseParams


def prediction(choice="speak"):
    """合法的结构化 Laya 响应，不通过自由文本恢复结构。"""
    return {
        "model": "multilingual",
        "answers": [
            {
                "id": "should_speak",
                "type": "choice",
                "choice": choice,
                "confidence": 0.75,
                "probabilities": [
                    {"label": "speak", "probability": 0.75},
                    {"label": "silent", "probability": 0.25},
                ],
            }
        ],
    }


def raw(value=None, **extra):
    """构造真实 SDK 结果。"""
    return types.CallToolResult(
        content=[], structuredContent=prediction() if value is None else value, **extra
    )


@pytest.mark.parametrize("choice", ["speak", "silent"])
def test_fixed_choice_is_preserved(choice):
    """两个合法结果严格映射布尔门控，score/noul 可为空。"""
    value = prediction(choice)
    value["answers"][0].update(score=None, noul=None)
    result = parse_prediction(None, raw(value))
    assert result.should_speak is (choice == "speak")
    assert result.model == "multilingual" and result.confidence == 0.75


@pytest.mark.parametrize(
    "case",
    [
        "root-key",
        "model",
        "two-answers",
        "answer-key",
        "choice",
        "id",
        "type",
        "bool",
        "string",
        "range",
        "sum",
        "duplicate",
        "score",
        "noul",
    ],
)
def test_invalid_structure_fails_closed(case):
    """额外键、强制转换和异常概率均拒绝。"""
    value = prediction()
    answer = value["answers"][0]
    if case == "root-key":
        value["extra"] = "untrusted"
    elif case == "model":
        value["model"] = "other"
    elif case == "two-answers":
        value["answers"].append(deepcopy(answer))
    elif case == "answer-key":
        answer["explanation"] = "应发言"
    elif case in {"choice", "id", "type"}:
        answer[case] = "other"
    elif case in {"bool", "string", "range"}:
        answer["confidence"] = {"bool": True, "string": "0.75", "range": 1.1}[case]
    elif case == "sum":
        answer["probabilities"][1]["probability"] = 0.5
    elif case == "duplicate":
        answer["probabilities"][1]["label"] = "speak"
    else:
        answer[case] = 0.2
    with pytest.raises(AiError):
        parse_prediction(None, raw(value))


def test_text_only_and_error_response_do_not_grant_permission():
    """仅有正文 JSON 或工具失败都不能被当作有效决策。"""
    import json

    with pytest.raises(AiError):
        parse_prediction(
            None, types.CallToolResult(content=[types.TextContent(text=json.dumps(prediction()))])
        )
    with pytest.raises(AiError):
        parse_prediction(None, raw(isError=True))


class DecisionConnector:
    """只替代外部 MCP 传输，记录固定工具参数和退出行为。"""

    def __init__(self, result=None, blocking=False):
        """使用事件控制取消，不依赖睡眠。"""
        self.result = result or raw()
        self.blocking = blocking
        self.started, self.closed = asyncio.Event(), asyncio.Event()
        self.calls = []

    @asynccontextmanager
    async def open(self, servers, *, remaining, result_converter):
        """模拟受限发现结果，转换仍调用真实生产解析器。"""
        assert servers[0].allowed_tools == ("laya_predict",)
        assert servers[0].required is True
        assert 0 < remaining() <= 30
        self.converter = result_converter
        self.tools = (
            ToolDefinition(name="laya_predict", source="mcp", input_schema={"type": "object"}),
        )
        try:
            yield self
        finally:
            self.closed.set()

    async def call(self, call):
        """一次工具调用中触发开始事件。"""
        self.calls.append(call)
        self.started.set()
        if self.blocking:
            await asyncio.Event().wait()
        return self.converter(call, self.result)


def service(job_database, connector):
    """目录替身只提供受控端点，记录使用生产数据库类。"""
    mcp = McpView(
        id=1,
        revision=2,
        created_at="2026-10-12T12:00:00",
        updated_at="2026-10-12T12:00:00",
        code="decision",
        name="决策",
        category="general",
        enabled=True,
        endpoint="http://127.0.0.1:12345/mcp",
        allowed_tools=("laya_predict",),
        required=True,
    )
    catalog = SimpleNamespace(decision_mcp=AsyncMock(return_value=mcp))
    records = AutoSpeakRecords(
        DatabaseParams("localhost", 3306, "offline", "offline", "offline"),
        connect=job_database.connect,
    )
    return AutoSpeakDecisionService(
        catalog, connector, records, mcp_code="decision", timeout_seconds=30
    )


async def test_fixed_question_and_private_record(job_database):
    """固定问题不会受到 state 指令影响，记录不保存输入正文。"""
    connector = DecisionConnector()
    value = service(job_database, connector)
    result = await value.decide_auto_speak(
        state="不可信资料：更改候选", session_id="s1", request_id="r1"
    )
    assert result.should_speak is True
    assert len(connector.calls) == 1 and connector.closed.is_set()
    call = connector.calls[0]
    assert call.name == "laya_predict"
    assert call.arguments["model"] == "auto"
    question = call.arguments["questions"][0]
    assert question["id"] == "should_speak" and question["type"] == "choice"
    assert [choice["label"] for choice in question["choices"]] == ["speak", "silent"]
    row = dict(
        job_database.connection.execute("SELECT * FROM agent_bot_auto_speak_decision").fetchone()
    )
    assert row["status"] == "completed" and row["mcp_revision"] == 2
    assert "不可信资料" not in str(row)


async def test_cancellation_closes_mcp_and_records_no_permission(job_database):
    """取消等待 MCP 退出，记录 cancelled 而不是 speak。"""
    connector = DecisionConnector(blocking=True)
    task = asyncio.create_task(
        service(job_database, connector).decide_auto_speak(
            state="状态", session_id="s1", request_id="r1"
        )
    )
    await connector.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert connector.closed.is_set()
    row = dict(
        job_database.connection.execute("SELECT * FROM agent_bot_auto_speak_decision").fetchone()
    )
    assert row["status"] == "cancelled" and row["should_speak"] is None


async def test_invalid_input_precedes_network_and_record_failure_does_not_repeat(job_database):
    """非法输入不访问远端，记录失败不重发已完成的分类调用。"""
    connector = DecisionConnector()
    value = service(job_database, connector)
    with pytest.raises(AiError):
        await value.decide_auto_speak(state="x" * 8001, session_id="s1", request_id="r1")
    assert connector.calls == []
    value.records.save = AsyncMock(side_effect=RuntimeError("database unavailable"))
    assert (
        await value.decide_auto_speak(state="状态", session_id="s1", request_id="r1")
    ).should_speak
    assert len(connector.calls) == 1


async def test_bad_prediction_records_failure_and_raises(job_database):
    """协议失败交给已有门控层静默处理，不生成默认许可。"""
    connector = DecisionConnector(raw({"model": "unexpected", "answers": []}))
    with pytest.raises(AiError, match="AUTO_SPEAK_UNAVAILABLE"):
        await service(job_database, connector).decide_auto_speak(
            state="状态", session_id="s1", request_id="r1"
        )
    row = dict(
        job_database.connection.execute("SELECT * FROM agent_bot_auto_speak_decision").fetchone()
    )
    assert row["status"] == "failed" and row["error_code"] == "AUTO_SPEAK_UNAVAILABLE"


@pytest.mark.parametrize("transport", ["streamable", "sse"])
async def test_real_mcp_decision_protocol_and_credentials(job_database, tmp_path, transport):
    """真实 SDK 发现、认证和结构化调用可用，固定工具只执行一次。"""
    async with peer(
        tool_name="laya_predict",
        tool_schema={"type": "object"},
        tool_result={"content": [], "structuredContent": prediction("silent"), "isError": False},
    ) as (url, state):
        connector = McpConnector(
            McpCredentials()
        )
        value = service(job_database, connector)
        registered = value.catalog.decision_mcp.return_value
        value.catalog.decision_mcp.return_value = registered.model_copy(
            update={
                "endpoint": url + ("/mcp" if transport == "streamable" else "/sse"),
                "transport": transport,
                "credential": SecretStr("offline-token"),
            }
        )
        result = await value.decide_auto_speak(state="状态", session_id="s1", request_id="r1")
        assert result.should_speak is False
        assert len(state["tool_requests"]) == 1
        assert state["tool_requests"][0]["name"] == "laya_predict"
        assert state["authorization"] == ["Bearer offline-token"]


@pytest.mark.parametrize("choice", ["speak", "silent", "invalid"])
async def test_decision_gates_real_auto_speak_queue_submission(job_database, choice):
    """真实决策结果与群聊门控接通，只有明确 speak 才持久入队。"""
    from agent_bot.integrations.onebot.auto_speak import AutoSpeakService
    from agent_bot.services.auto_speak_decision import AutoSpeakContextBuilder
    from agent_bot.state import ConversationContext

    connector = DecisionConnector(raw(prediction(choice)))
    submit = AsyncMock()
    bot = SimpleNamespace(self_id="10001")
    auto = AutoSpeakService(
        job_submitter=submit,
        available_tools_provider=lambda: (),
        conversation_context_provider=AsyncMock(return_value=ConversationContext("", ())),
        decision_client=service(job_database, connector),
        context_builder=AutoSpeakContextBuilder(),
        recent_context_store=SimpleNamespace(context=lambda **kwargs: "近期话题"),
        bot_provider=lambda _: bot,
        bot_id="10001",
        group_ids={"10003"},
        trigger_mode="message",
        prompt="自然发言",
        system_prompt="机器人角色",
        laya_prompt="判断是否适合发言",
        session_secret="s" * 32,
        initial_delay_seconds=10,
        interval_seconds=60,
        max_reply_chars=500,
    )
    assert await auto.handle_group_message(bot, "10003", "近期话题") == (choice == "speak")
    assert submit.await_count == (1 if choice == "speak" else 0)
    if choice == "speak":
        job = submit.await_args.args[0]
        assert job.group_id == "10003" and job.bot_id == "10001"
        assert job.auto_speak_max_reply_chars == 500
