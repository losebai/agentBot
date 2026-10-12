"""Comfy 按任务取消及提交竞争的离线边界测试。"""

from __future__ import annotations

import httpx
import pytest

from agent_bot.integrations.comfy import ComfyClient, ComfyWorkflowError
from agent_bot.services.image_task_context import ImageTaskCancelled, active_image_task


@pytest.mark.parametrize(
    ("status_code", "payload", "expected"),
    [(200, {"cancelled": True}, "cancelled"),
     (200, {"cancelled": False}, "not_active"),
     (404, {}, "unsupported"), (405, {}, "unsupported")],
)
async def test_cancel_targets_one_prompt_and_never_globally_interrupts(
    status_code: int, payload: dict[str, object], expected: str,
) -> None:
    """旧服务不支持按 ID 取消时，不得回退到全局中断。"""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """记录 HTTP 边界用于检验指定任务路径。"""
        requests.append(request)
        return httpx.Response(status_code, json=payload)

    client = ComfyClient(
        base_url="http://comfy.invalid:8188", timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.cancel("prompt-123")
        assert result.status == expected
        assert [(r.method, r.url.path) for r in requests] == [
            ("POST", "/api/jobs/prompt-123/cancel"),
        ]
    finally:
        await client.aclose()


@pytest.mark.parametrize("payload", [{}, {"cancelled": "true"}, {"cancelled": 1}])
async def test_cancel_rejects_malformed_success(payload: dict[str, object]) -> None:
    """缺失或非布尔返回值不能被报告为取消成功。"""
    client = ComfyClient(
        base_url="http://comfy.invalid", timeout_seconds=10,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    )
    try:
        with pytest.raises(ComfyWorkflowError, match=r"取消.*返回无效"):
            await client.cancel("prompt-123")
    finally:
        await client.aclose()


@pytest.mark.parametrize("prompt_id", ["../other", "", "a/b", "x" * 129])
async def test_cancel_rejects_invalid_id_before_network(prompt_id: str) -> None:
    """任务 ID 不能改写上游请求路径。"""
    def handler(_: httpx.Request) -> httpx.Response:
        """参数校验失败必须完全不发送 HTTP 请求。"""
        pytest.fail("invalid ID reached network")

    client = ComfyClient(
        base_url="http://comfy.invalid", timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ComfyWorkflowError) as error:
            await client.cancel(prompt_id)
        assert error.value.code == "INVALID_ARGUMENT"
    finally:
        await client.aclose()


class _Execution:
    """模拟提交期间发生取消，记录客户端通知的真实上游 ID。"""

    def __init__(self) -> None:
        """初始化阶段记录及取消标记。"""
        self.cancelled = False
        self.events: list[tuple[str, ...]] = []

    async def check_cancelled(self) -> None:
        """取消后阻止继续轮询及下载。"""
        if self.cancelled:
            raise ImageTaskCancelled()

    async def submitting(self, endpoint: str) -> None:
        """提交前检查取消并记录目标。"""
        await self.check_cancelled()
        self.events.append(("submitting", endpoint))

    async def submitted(self, prompt_id: str, endpoint: str) -> None:
        """即使提交途中取消，也必须先收到上游返回的 ID。"""
        self.events.append(("submitted", prompt_id, endpoint))
        await self.check_cancelled()


async def test_cancel_during_submission_retains_prompt_without_polling() -> None:
    """取消发生于 POST 处理中，仍先通知持久化 ID，再终止下游动作。"""
    execution = _Execution()
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """提交期间注入用户取消，其他路径禁止调用。"""
        requests.append(request.url.path)
        assert request.url.path == "/prompt"
        execution.cancelled = True
        return httpx.Response(200, json={"prompt_id": "prompt-123"})

    client = ComfyClient(
        base_url="http://comfy.invalid:8188", timeout_seconds=10,
        transport=httpx.MockTransport(handler), poll_interval_seconds=0,
    )
    token = active_image_task.set(execution)
    try:
        with pytest.raises(ImageTaskCancelled):
            await client.generate({"7": {"class_type": "X", "inputs": {}}}, output_node_id="7")
        assert execution.events == [
            ("submitting", "http://comfy.invalid:8188/"),
            ("submitted", "prompt-123", "http://comfy.invalid:8188/"),
        ]
        assert requests == ["/prompt"]
    finally:
        active_image_task.reset(token)
        await client.aclose()


@pytest.mark.parametrize("failure", ["timeout", "connection", "rejected", "invalid_json"])
async def test_cancel_reports_safe_upstream_failures(failure: str) -> None:
    """取消不能将网络错误或供应商原文当作停止成功返回。"""
    def handler(request: httpx.Request) -> httpx.Response:
        """模拟不可靠或返回内部敏感内容的上游。"""
        if failure == "timeout":
            raise httpx.ReadTimeout("private-internal", request=request)
        if failure == "connection":
            raise httpx.ConnectError("private-internal", request=request)
        return httpx.Response(500 if failure == "rejected" else 200, text="private-internal")

    client = ComfyClient(
        base_url="http://comfy.invalid", timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ComfyWorkflowError) as error:
            await client.cancel("prompt-123")
        assert error.value.code == {
            "timeout": "TIMEOUT", "connection": "UNAVAILABLE",
            "rejected": "REJECTED", "invalid_json": "INVALID_RESPONSE",
        }[failure]
        assert "private-internal" not in str(error.value)
    finally:
        await client.aclose()


async def test_cancel_while_polling_stops_before_next_http_request() -> None:
    """轮询中取消后不得再次查询或下载图片。"""
    execution = _Execution()
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """首次 history 等待期间标记取消。"""
        requests.append(request.url.path)
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "prompt-123"})
        assert request.url.path == "/history/prompt-123"
        execution.cancelled = True
        return httpx.Response(200, json={})

    client = ComfyClient(
        base_url="http://comfy.invalid", timeout_seconds=10,
        transport=httpx.MockTransport(handler), poll_interval_seconds=0,
    )
    token = active_image_task.set(execution)
    try:
        with pytest.raises(ImageTaskCancelled):
            await client.generate({"7": {"class_type": "X", "inputs": {}}}, output_node_id="7")
        assert requests == ["/prompt", "/history/prompt-123"]
    finally:
        active_image_task.reset(token)
        await client.aclose()
