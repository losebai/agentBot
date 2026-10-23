"""沿用 Comfy 固定客户端和请求期限读取唯一 MP4，不接受外部媒体 URL。"""

import time

import httpx

from agent_bot.media.video import MAX_VIDEO_BYTES, validate_mp4
from agent_bot.services.image_task_context import active_image_task
from agent_bot.services.workflow_execution import GeneratedWorkflowVideo


async def download_video(client, descriptor, *, deadline: float) -> GeneratedWorkflowVideo:
    """流式限制字节数，关闭响应后核验容器；取消不泄漏连接。"""
    from agent_bot.integrations.comfy import ComfyWorkflowError

    try:
        async with client._http.stream(
            "GET",
            "view",
            params=descriptor.model_dump(),
            timeout=max(0.001, deadline - time.monotonic()),
        ) as response:
            if response.status_code != 200:
                raise ComfyWorkflowError("UPSTREAM_ERROR", "视频读取失败", stage="video_download")
            mime = response.headers.get("Content-Type", "").partition(";")[0].lower()
            if mime != "video/mp4":
                raise ValueError("invalid video MIME")
            length = response.headers.get("Content-Length")
            if length is not None and not 0 < int(length) <= MAX_VIDEO_BYTES:
                raise ValueError("invalid video length")
            chunks, total = [], 0
            async for chunk in response.aiter_bytes():
                if time.monotonic() >= deadline:
                    raise TimeoutError
                if (execution := active_image_task.get()) is not None:
                    await execution.check_cancelled()
                total += len(chunk)
                if total > MAX_VIDEO_BYTES:
                    raise ComfyWorkflowError(
                        "LIMIT_EXCEEDED", "视频超过 100 MiB", stage="video_download"
                    )
                chunks.append(chunk)
            if length is not None and total != int(length):
                raise ValueError("video length differs")
            data = b"".join(chunks)
            validate_mp4(data)
            return GeneratedWorkflowVideo(media_type="video/mp4", data=data)
    except (httpx.TimeoutException, TimeoutError) as error:
        raise ComfyWorkflowError("TIMEOUT", "视频读取超时", stage="video_download") from error
    except httpx.RequestError as error:
        raise ComfyWorkflowError("UNAVAILABLE", "视频读取失败", stage="video_download") from error
    except ValueError as error:
        raise ComfyWorkflowError(
            "INVALID_RESPONSE", "视频格式或大小无效", stage="video_validate"
        ) from error
