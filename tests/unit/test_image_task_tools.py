"""Image queue tools preserve authenticated ownership and bounded arguments."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import get_args
from unittest.mock import AsyncMock

import pytest

from agent_bot.models import AgentToolName, ChatToolName
from agent_bot.services.chat.routing import ChatRouteSelector
from agent_bot.services.chat_tools import (
    ChatToolContext,
    ChatToolError,
    build_chat_tool_registry,
)
from agent_bot.services.image_tasks import ImageTask, ImageTaskCancellation, ImageTaskList


def _task() -> ImageTask:
    """返回包含完整取消确认信息的公开任务摘要。"""

    return ImageTask(
        task_id="image-test", created_at=1, status="cancelled",
        cancellation_requested=True, upstream_cancellation="unsupported",
    )


def test_management_intent_routes_to_agent_without_generation_tools() -> None:
    """列出和取消意图无需触发生图路由也能进入 Agent。"""

    route = ChatRouteSelector().select(
        "取消我上一张图片的任务",
        available_tools=("local_workflow_tasks", "local_workflow_cancel"),
        web_search_enabled=False,
    )
    assert route.chat_tools == ("local_workflow_tasks", "local_workflow_cancel")
    assert route.use_agent


def _context(service: object | None, **overrides: object) -> ChatToolContext:
    """Bind task management to the authenticated conversation identity."""

    return ChatToolContext(
        session_id="group-session", user_id="10001", bot_user_id="90001",
        allowed_tools=frozenset({"local_workflow_tasks", "local_workflow_cancel"}),
        image_tasks=service, **overrides,
    )


def test_task_tools_are_advertised_as_bounded_ownerless_schemas() -> None:
    """Missing schemas or model-provided ownership would break the MCP contract."""

    names = {"local_workflow_tasks", "local_workflow_cancel"}
    definitions = build_chat_tool_registry().list_tools(frozenset(names))
    assert {definition.name for definition in definitions} == names
    assert names <= set(get_args(ChatToolName))
    assert names <= set(get_args(AgentToolName))
    schemas = {definition.name: definition.input_schema for definition in definitions}
    listing = schemas["local_workflow_tasks"]
    assert listing["additionalProperties"] is False
    assert set(listing["properties"]) == {"limit"}
    assert listing["properties"]["limit"]["maximum"] == 20
    cancellation = schemas["local_workflow_cancel"]
    assert cancellation["additionalProperties"] is False
    assert set(cancellation["properties"]) == {"task_id"}


@pytest.mark.parametrize("limit", [None, 1, 20])
async def test_listing_uses_authenticated_owner_and_serializes_safe_result(limit) -> None:
    """The service must receive the real owner and a bounded, defaulted result limit."""

    service = SimpleNamespace(list_tasks=AsyncMock(return_value=ImageTaskList(tasks=[_task()])))
    result = await build_chat_tool_registry().call(
        "local_workflow_tasks", {} if limit is None else {"limit": limit}, _context(service),
    )
    assert json.loads(result.content) == {"tasks": [{
        "task_id": "image-test", "created_at": 1, "status": "cancelled",
        "cancellation_requested": True, "upstream_cancellation": "unsupported",
        "delivered_parts": 0,
    }]}
    owner = service.list_tasks.call_args.args[0]
    assert owner.model_dump() == {
        "bot_id": "90001", "session_id": "group-session", "user_id": "10001",
    }
    assert service.list_tasks.call_args.kwargs == {"limit": 20 if limit is None else limit}


async def test_cancellation_uses_authenticated_owner_and_is_repeatable() -> None:
    """Cancellation can be repeated without consuming generation quotas."""

    service = SimpleNamespace(cancel=AsyncMock(return_value=ImageTaskCancellation(task=_task())))
    context = _context(service)
    registry = build_chat_tool_registry()
    for _ in range(2):
        result = await registry.call("local_workflow_cancel", {"task_id": "image-test"}, context)
        task = json.loads(result.content)["task"]
        assert task["status"] == "cancelled"
        assert task["upstream_cancellation"] == "unsupported"
    owner, task_id = service.cancel.call_args.args
    assert owner.model_dump() == {
        "bot_id": "90001", "session_id": "group-session", "user_id": "10001",
    }
    assert task_id == "image-test"


@pytest.mark.parametrize("name,arguments", [
    ("local_workflow_tasks", {"limit": 0}),
    ("local_workflow_tasks", {"limit": 21}),
    ("local_workflow_tasks", {"limit": True}),
    ("local_workflow_tasks", {"user_id": "other-user"}),
    ("local_workflow_cancel", {"task_id": "../private"}),
    ("local_workflow_cancel", {"task_id": "a" * 129}),
    ("local_workflow_cancel", {"task_id": "image-test", "session_id": "other-session"}),
])
async def test_management_rejects_unbounded_arguments_or_owner_overrides(name, arguments) -> None:
    """Unsafe model arguments never reach the queue service."""

    service = SimpleNamespace(list_tasks=AsyncMock(), cancel=AsyncMock())
    with pytest.raises(ChatToolError) as failed:
        await build_chat_tool_registry().call(name, arguments, _context(service))
    assert failed.value.code == "INVALID_ARGUMENT"
    service.list_tasks.assert_not_awaited()
    service.cancel.assert_not_awaited()


async def test_management_requires_service_and_active_deadline() -> None:
    """Disabled services and expired chat tokens cannot touch queue tasks."""

    registry = build_chat_tool_registry()
    with pytest.raises(ChatToolError) as unavailable:
        await registry.call("local_workflow_tasks", {}, _context(None))
    assert unavailable.value.code == "UNAVAILABLE"
    service = SimpleNamespace(cancel=AsyncMock())
    with pytest.raises(ChatToolError) as expired:
        await registry.call(
            "local_workflow_cancel", {"task_id": "image-test"},
            _context(service, deadline_at=time.monotonic() - 1),
        )
    assert expired.value.code == "DEADLINE_EXCEEDED"
    service.cancel.assert_not_awaited()


async def test_management_rejects_missing_authenticated_bot() -> None:
    """缺少机器人身份时不得形成不完整的所有权范围。"""

    service = SimpleNamespace(cancel=AsyncMock())
    context = _context(service)
    context.bot_user_id = None
    with pytest.raises(ChatToolError) as failed:
        await build_chat_tool_registry().call(
            "local_workflow_cancel", {"task_id": "image-test"}, context,
        )
    assert failed.value.code == "FORBIDDEN"
    service.cancel.assert_not_awaited()


async def test_management_preserves_safe_service_errors_and_hides_unexpected_failures() -> None:
    """队列稳定错误可供模型判断，未经处理的底层连接信息不得外泄。"""

    from agent_bot.services.image_tasks import ImageTaskError

    registry = build_chat_tool_registry()
    service = SimpleNamespace(cancel=AsyncMock(
        side_effect=ImageTaskError("NOT_FOUND", "未找到图片任务"),
    ))
    with pytest.raises(ChatToolError) as missing:
        await registry.call("local_workflow_cancel", {"task_id": "image-test"}, _context(service))
    assert missing.value.code == "NOT_FOUND"
    assert str(missing.value) == "未找到图片任务"
    service.cancel.side_effect = RuntimeError("redis://internal/PRIVATE_TOKEN")
    with pytest.raises(ChatToolError) as unexpected:
        await registry.call("local_workflow_cancel", {"task_id": "image-test"}, _context(service))
    assert unexpected.value.code == "INTERNAL"
    assert "PRIVATE_TOKEN" not in str(unexpected.value)
