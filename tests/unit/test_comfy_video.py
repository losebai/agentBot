"""视频下载严格沿用单任务预算，拒绝伪装、越界与多输出。"""

import httpx
import pytest

from agent_bot.integrations.comfy import ComfyClient, ComfyWorkflowError


def box(kind, payload):
    """创建仅供容器结构测试的 ISO BMFF box，不声称它可解码。"""
    return (len(payload) + 8).to_bytes(4, "big") + kind + payload


MP4 = (
    box(b"ftyp", b"isom\x00\x00\x00\x00isom")
    + box(b"moov", box(b"trak", box(b"mdia", box(b"hdlr", b"\0" * 8 + b"vide" + b"\0" * 12))))
    + box(b"mdat", b"frame")
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["success", "truncated", "html", "traversal", "multiple", "failed"]
)
async def test_video_history_download_and_validation(mode):
    """真实 SaveVideo 使用 images 字段，不能误取输入视频预览或重复提交。"""
    requests = []

    def handler(request):
        """仅模拟当前任务的输出和固定 view，不访问外部服务。"""
        requests.append(request)
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "owned-prompt"})
        if request.url.path.startswith("/history/"):
            descriptor = {
                "filename": "../bad.mp4" if mode == "traversal" else "h3.mp4",
                "subfolder": "video",
                "type": "output",
            }
            return httpx.Response(
                200,
                json={
                    "owned-prompt": {
                        "status": {
                            "completed": True,
                            "status_str": "error" if mode == "failed" else "success",
                        },
                        "outputs": {
                            "15": {"images": [descriptor] * (2 if mode == "multiple" else 1)}
                        },
                    }
                },
            )
        return httpx.Response(
            200,
            content=b"<html>bad</html>"
            if mode == "html"
            else MP4[:-3]
            if mode == "truncated"
            else MP4,
            headers={"Content-Type": "video/mp4"},
        )

    client = ComfyClient(
        base_url="http://comfy.invalid",
        timeout_seconds=30,
        transport=httpx.MockTransport(handler),
        poll_interval_seconds=0,
    )
    try:
        if mode == "success":
            result = await client.generate_video(
                {"15": {"class_type": "SaveVideo", "inputs": {}}}, output_node_id="15"
            )
            assert result.media_type == "video/mp4"
            assert result.data == MP4
        else:
            with pytest.raises(ComfyWorkflowError):
                await client.generate_video(
                    {"15": {"class_type": "SaveVideo", "inputs": {}}}, output_node_id="15"
                )
        assert sum(r.url.path == "/prompt" for r in requests) == 1
        if mode in {"traversal", "multiple", "failed"}:
            assert not any(r.url.path == "/view" for r in requests)
    finally:
        await client.aclose()
