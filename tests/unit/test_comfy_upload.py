"""只从受信任素材下载并上传当前原图，不提交生图。"""

import hashlib

import httpx
import pytest

from agent_bot.integrations.comfy import ComfyClient, ComfyWorkflowError

PNG = b"\x89PNG\r\n\x1a\n" + b"source-image"
SCOPE = "a" * 32
FOLDER = "chat-i2i/" + SCOPE
NAME = hashlib.sha256(PNG).hexdigest() + ".png"


@pytest.mark.asyncio
async def test_upload_transmits_source_bytes_and_returns_request_scoped_reference():
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        assert request.url == "http://comfy.example.invalid/upload/image"
        assert PNG in request.content
        assert NAME.encode() in request.content
        assert FOLDER.encode() in request.content
        assert b'false' in request.content
        return httpx.Response(200, json={"name": NAME, "subfolder": FOLDER, "type": "input"})

    client = ComfyClient(base_url="http://comfy.example.invalid", timeout_seconds=30,
                         transport=httpx.MockTransport(handler))
    try:
        uploaded = await client.upload_input_image(
            "https://gchat.qpic.cn/image?private=SECRET", request_scope=SCOPE,
        )
        assert uploaded.comfy_path == FOLDER + "/" + NAME
        assert uploaded.size_bytes == len(PNG)
        assert len(requests) == 2
        assert "SECRET" not in uploaded.model_dump_json()
    finally:
        await client.aclose()


@pytest.mark.parametrize("problem", [
    "empty", "gif", "wrong_mime", "oversized", "redirect", "401", "timeout",
    "invalid_json", "type", "path", "other_filename", "other_folder", "apng", "animated_webp",
])
@pytest.mark.asyncio
async def test_invalid_source_or_upload_never_submits_prompt(problem, caplog):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.method == "GET":
            if problem == "redirect":
                return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
            content = {"empty": b"", "gif": b"GIF89a-data"}.get(problem, PNG)
            if problem == "apng":
                content = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x08acTL" + b"\x00" * 12
            if problem == "animated_webp":
                content = b"RIFF\x0e\x00\x00\x00WEBPANIM\x06\x00\x00\x00" + b"\x00" * 6
            mime = "text/html" if problem == "wrong_mime" else (
                "image/gif" if problem == "gif" else "image/png"
            )
            headers = {"content-type": mime}
            if problem == "animated_webp":
                headers["content-type"] = "image/webp"
            if problem == "oversized":
                headers["content-length"] = "10485761"
            return httpx.Response(200, content=content, headers=headers)
        if problem == "401":
            return httpx.Response(401, text="PRIVATE_SERVER_TOKEN")
        if problem == "timeout":
            raise httpx.ReadTimeout("PRIVATE_SERVER_TOKEN", request=request)
        if problem == "invalid_json":
            return httpx.Response(200, text="not-json")
        payload = {"name": NAME, "subfolder": FOLDER, "type": "input"}
        if problem == "type":
            payload["type"] = "output"
        if problem == "path":
            payload["name"] = "../private.png"
        if problem == "other_filename":
            payload["name"] = "b" * 64 + ".png"
        if problem == "other_folder":
            payload["subfolder"] = "chat-i2i/" + "b" * 32
        return httpx.Response(200, json=payload)

    client = ComfyClient(base_url="http://comfy.example.invalid", timeout_seconds=30,
                         transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ComfyWorkflowError) as error:
            await client.upload_input_image("https://gchat.qpic.cn/source", request_scope=SCOPE)
        assert "PRIVATE_SERVER_TOKEN" not in str(error.value)
        assert "PRIVATE_SERVER_TOKEN" not in caplog.text
        assert "/prompt" not in paths
        assert paths.count("/upload/image") <= 1
        if problem in {"apng", "animated_webp"}:
            assert "/upload/image" not in paths
    finally:
        await client.aclose()
