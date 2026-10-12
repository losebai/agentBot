from __future__ import annotations

import json
import logging

import httpx
import pytest

from agent_bot.integrations.comfy import ComfyClient, ComfyWorkflowError
from agent_bot.services.workflow_execution import GeneratedWorkflowImage

PNG = b"\x89PNG\r\n\x1a\n" + b"image-data"


@pytest.mark.asyncio
async def test_comfy_waits_for_queued_prompt_without_resubmitting_then_downloads() -> None:
    """Comfy 排队时 history 为空，必须继续等待同一 prompt，不能判失败或重复提交。"""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "prompt-123"})
        if request.url.path == "/history/prompt-123":
            if sum(r.url.path == "/history/prompt-123" for r in requests) < 3:
                return httpx.Response(200, json={})
            return httpx.Response(
                200,
                json={
                    "prompt-123": {
                        "status": {"completed": True, "status_str": "success"},
                        "outputs": {
                            "7": {
                                "images": [
                                    {
                                        "filename": "ComfyUI_temp.png",
                                        "subfolder": "",
                                        "type": "temp",
                                    }
                                ]
                            }
                        },
                    }
                },
            )
        if request.url.path == "/view":
            assert dict(request.url.params) == {
                "filename": "ComfyUI_temp.png",
                "subfolder": "",
                "type": "temp",
            }
            return httpx.Response(200, content=PNG, headers={"Content-Type": "image/png"})
        raise AssertionError(request.url)

    client = ComfyClient(
        base_url="http://comfy.example.invalid:8188",
        timeout_seconds=500,
        transport=httpx.MockTransport(handler),
        poll_interval_seconds=0,
    )
    workflow = {"7": {"class_type": "PreviewImage", "inputs": {}}}

    image = await client.generate(workflow, output_node_id="7")

    submitted = json.loads(requests[0].content)
    assert set(submitted) == {"client_id", "prompt"}
    assert submitted["prompt"] == workflow
    assert requests[0].extensions["timeout"]["read"] == 500
    assert sum(r.url.path == "/prompt" for r in requests) == 1
    assert sum(r.url.path == "/history/prompt-123" for r in requests) == 3
    assert image == GeneratedWorkflowImage(media_type="image/png", data=PNG)
    await client.aclose()


@pytest.mark.asyncio
async def test_comfy_rejection_logs_stage_without_upstream_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="agent_bot.integrations.comfy")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="internal node failed at http://10.0.8.5/private")

    client = ComfyClient(
        base_url="http://comfy.example.invalid:8188",
        timeout_seconds=30,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ComfyWorkflowError) as exc_info:
        await client.generate({"1": {"class_type": "X", "inputs": {}}}, output_node_id="1")

    assert exc_info.value.code == "REJECTED"
    assert "10.0.8.5" not in str(exc_info.value)
    assert "private" not in str(exc_info.value)
    diagnostic = next(
        record.getMessage()
        for record in caplog.records
        if record.name == "agent_bot.integrations.comfy"
    )
    assert "stage=prompt_submit" in diagnostic
    assert "code=REJECTED" in diagnostic
    assert "status_code=400" in diagnostic
    assert "output_node_id=1" in diagnostic
    assert "prompt_id=-" in diagnostic
    assert "elapsed_ms=" in diagnostic
    assert "10.0.8.5" not in diagnostic
    assert "private" not in diagnostic
    await client.aclose()
