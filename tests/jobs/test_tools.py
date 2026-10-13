"""通过真实 QQ 授权上下文验证任务工具及批次副作用边界。"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from tests.jobs.test_store import scope

from agent_bot.agent.contracts import AgentRequest, ToolCall
from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.runtime import BoundTools
from agent_bot.jobs.tools import JobTools
from agent_bot.models import ChatContext, ChatMessage
from agent_bot.services.chat_tools import ChatToolContext


def context(**changes):
    """认证层创建的完整 QQ 上下文，不由模型提供。"""
    return ChatToolContext(
        **{
            "session_id": "session-1",
            "user_id": "10002",
            "allowed_tools": frozenset({"job"}),
            "bot_user_id": "10001",
            "chat_context": ChatContext(
                botId="10001",
                senderId="10002",
                groupId="10003",
                scene="group",
            ),
            **changes,
        }
    )


def request():
    """构造没有预设的真实 Agent 请求，系统消息来自宿主。"""
    return AgentRequest(
        model="offline-model",
        session_id="session-1",
        request_id="request-1",
        prompt="稍后提醒",
        messages=(ChatMessage(role="system", content="角色快照"),),
    )


def snapshot():
    """这里只替换目录解析边界，工具仍使用真实实现。"""
    return SimpleNamespace(
        agent=None, resolved=SimpleNamespace(model=SimpleNamespace(alias="offline-model"))
    )


def call(command):
    """模型只能生成 command，不携带身份参数。"""
    return ToolCall(call_id="call-1", name="job", arguments={"command": command})


async def test_bound_job_uses_host_identity_and_idempotency(job_store):
    """不同工具调用编号重放同一请求时返回原任务。"""
    session = JobTools(job_store).bind(snapshot(), request(), context())
    tool = call({"action": "create", "prompt": "稍后提醒", "delaySeconds": 1})
    first = json.loads((await session.call(tool)).content)["jobs"][0]
    second = json.loads(
        (await session.call(tool.model_copy(update={"call_id": "call-2"}))).content
    )["jobs"][0]
    assert first["id"] == second["id"]
    assert first["botId"] == "10001" and first["senderId"] == "10002"
    assert "system_prompt" not in first and "model" not in first
    assert len(await job_store.list_owned(scope())) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"allowed_tools": frozenset()},
        {"session_id": "another"},
        {"user_id": "99999"},
        {"chat_context": None},
    ],
)
def test_job_context_cannot_cross_identity(job_store, changes):
    """绑定前确认令牌上下文、会话和 sender 一致。"""
    with pytest.raises(AgentError):
        JobTools(job_store).bind(snapshot(), request(), context(**changes))


async def test_batch_prevalidation_rejects_semantic_error_before_creation(job_store):
    """语法合法但动作混杂的后一项，在前一项创建任务之前失败。"""
    session = JobTools(job_store).bind(snapshot(), request(), context())
    bound = BoundTools(session.definitions, {"job": session.call})
    valid = call({"action": "create", "prompt": "有效", "delaySeconds": 1})
    invalid = call(
        {"action": "create", "prompt": "非法", "delaySeconds": 1, "runAt": "2026-10-13T10:00:00Z"}
    )
    with pytest.raises(AgentError, match="AGENT_TOOL_ARGUMENTS_INVALID"):
        for item in (valid, invalid):
            bound.validate(item)
    assert await job_store.list_owned(scope()) == ()


async def test_tool_get_bounds_full_result_with_explicit_truncation(job_store):
    """完整结果留在数据库，模型上下文只拿到明确标记的截断内容。"""
    session = JobTools(job_store).bind(snapshot(), request(), context())
    value = await session.call(call({"action": "create", "prompt": "正文", "delaySeconds": 1}))
    id = json.loads(value.content)["jobs"][0]["id"]
    job_store.clock = lambda: datetime(2026, 10, 13, tzinfo=timezone.utc)
    await job_store.claim_due()
    await job_store.finish(id, result="长正文" * 20000)
    result = await session.call(call({"action": "get", "jobId": id}))
    assert len(result.content) <= 32000
    assert json.loads(result.content)["truncated"] is True
    assert len((await job_store.get(scope(), id)).result) == 60000
