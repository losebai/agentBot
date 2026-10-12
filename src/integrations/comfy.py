"""受限 ComfyUI API 客户端。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import uuid
from collections.abc import Mapping
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_bot.multimodal import (
    DownloadedImage,
    InvalidImageAttachmentError,
    fetch_image_attachment,
)
from agent_bot.services.image_task_context import active_image_task
from agent_bot.services.request_deadline import remaining_timeout
from agent_bot.services.workflow_execution import (
    GeneratedWorkflowImage,
    WorkflowExecutionError,
)
from agent_bot.services.workflow_inputs import WorkflowInputImage
from agent_bot.services.workflows import ApiWorkflow

_SUPPORTED_MEDIA_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
}

logger = logging.getLogger(__name__)


class ComfyCancellationResult(BaseModel):
    """按任务取消的上游结果，不将不支持或空操作误报为停止成功。"""

    status: Literal["cancelled", "not_active", "unsupported"]


class _CancelResponse(BaseModel):
    """官方单任务取消返回的严格布尔标志。"""

    model_config = ConfigDict(extra="ignore", strict=True)
    cancelled: bool


class _PromptResponse(BaseModel):
    """Comfy `/prompt` 成功响应。"""

    model_config = ConfigDict(extra="ignore")
    prompt_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class _ImageDescriptor(BaseModel):
    """Comfy history 中的图片引用。"""

    model_config = ConfigDict(extra="ignore")
    filename: str = Field(min_length=1, max_length=255)
    subfolder: str = Field(default="", max_length=512)
    type: str = Field(pattern=r"^(?:temp|output)$")


class _UploadResponse(BaseModel):
    """只接受 Comfy input 上传响应，字段缺失或类型错误即拒绝。"""

    model_config = ConfigDict(extra="ignore", strict=True)
    name: str
    subfolder: str
    type: str = Field(pattern=r"^input$")


class ComfyWorkflowError(WorkflowExecutionError):
    """不暴露 Comfy 原始响应的稳定执行错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str,
        status_code: int | None = None,
    ) -> None:
        """保存用于脱敏诊断的执行阶段和 HTTP 状态。"""

        super().__init__(code, message)
        self.stage = stage
        self.status_code = status_code


class ComfyClient:
    """提交完整 API 工作流并下载单张输出图片。"""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        max_image_bytes: int = 10_485_760,
        poll_interval_seconds: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """创建关闭代理、重定向和自动重试的异步客户端。"""

        if not 10 <= timeout_seconds <= 500:
            raise ValueError("Comfy timeout is invalid")
        if not 1 <= max_image_bytes <= 10_485_760:
            raise ValueError("Comfy image limit is invalid")
        if not 0 <= poll_interval_seconds <= 5:
            raise ValueError("Comfy poll interval is invalid")
        self._timeout_seconds = timeout_seconds
        self._max_image_bytes = max_image_bytes
        self._poll_interval_seconds = poll_interval_seconds
        self._http = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/",
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )

    async def cancel(self, prompt_id: str) -> ComfyCancellationResult:
        """使用官方按 ID 取消接口；旧版本不回退到全局 interrupt。"""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", prompt_id):
            raise ComfyWorkflowError(
                "INVALID_ARGUMENT", "绘图任务标识无效", stage="job_cancel",
            )
        try:
            response = await self._http.post(
                f"api/jobs/{prompt_id}/cancel",
                timeout=remaining_timeout(self._timeout_seconds),
            )
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise ComfyWorkflowError(
                "TIMEOUT", "绘图取消请求超时", stage="job_cancel",
            ) from exc
        except httpx.RequestError as exc:
            raise ComfyWorkflowError(
                "UNAVAILABLE", "绘图取消服务不可用", stage="job_cancel",
            ) from exc
        if response.status_code in {404, 405}:
            return ComfyCancellationResult(status="unsupported")
        if not 200 <= response.status_code < 300:
            raise ComfyWorkflowError(
                "REJECTED", "绘图取消请求被拒绝", stage="job_cancel",
                status_code=response.status_code,
            )
        try:
            result = _CancelResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise ComfyWorkflowError(
                "INVALID_RESPONSE", "绘图取消返回无效", stage="job_cancel",
            ) from exc
        return ComfyCancellationResult(status="cancelled" if result.cancelled else "not_active")

    async def upload_input_image(
        self, source_url: str, *, request_scope: str,
    ) -> WorkflowInputImage:
        """在同一剩余预算内校验、上传单张原图，不重试、不提交生成。"""
        if not re.fullmatch(r"[a-f0-9]{32}", request_scope):
            raise ComfyWorkflowError("INVALID_ARGUMENT", "原图请求标识无效", stage="image_validate")
        started = time.monotonic()
        try:
            try:
                return await asyncio.wait_for(
                    self._upload_input_image(source_url, request_scope=request_scope),
                    timeout=remaining_timeout(self._timeout_seconds),
                )
            except (TimeoutError, httpx.TimeoutException) as exc:
                raise ComfyWorkflowError(
                    "TIMEOUT", "原图准备超时", stage="image_upload",
                ) from exc
        except ComfyWorkflowError as error:
            logger.warning(
                "Comfy 原图准备失败：stage=%s code=%s status_code=%s elapsed_ms=%s",
                error.stage, error.code, error.status_code,
                round((time.monotonic() - started) * 1000),
            )
            raise

    async def _upload_input_image(
        self, source_url: str, *, request_scope: str,
    ) -> WorkflowInputImage:
        """下载原图并校验本次 multipart 上传响应的确切目录与文件名。"""
        try:
            image = await fetch_image_attachment(
                self._http, source_url, max_bytes=self._max_image_bytes,
            )
            image.validate_static()
        except InvalidImageAttachmentError as exc:
            raise ComfyWorkflowError(
                "INVALID_IMAGE", "用户原图无法读取或格式无效", stage="image_fetch",
            ) from exc
        return await self._upload_validated_image(image, request_scope=request_scope)

    async def upload_input_bytes(
        self, data: bytes, *, media_type: str, request_scope: str,
    ) -> WorkflowInputImage:
        """内部原图实际解码后复用受限上传，不接受文件路径或上游引用。"""
        from agent_bot.media.errors import MediaError
        from agent_bot.media.inputs import ImagePreparer

        if not re.fullmatch(r"[a-f0-9]{32}", request_scope) or len(data) > self._max_image_bytes:
            raise ComfyWorkflowError("INVALID_ARGUMENT", "原图请求无效", stage="image_validate")
        try:
            prepared = await ImagePreparer().prepare_async(data, media_type)
        except MediaError as error:
            raise ComfyWorkflowError(
                "INVALID_IMAGE", "原图格式无效", stage="image_validate",
            ) from error
        image = DownloadedImage(prepared.media_type, prepared.data)
        try:
            image.validate_static()
        except InvalidImageAttachmentError as error:
            raise ComfyWorkflowError(
                "INVALID_IMAGE", "原图格式无效", stage="image_validate",
            ) from error
        return await asyncio.wait_for(
            self._upload_validated_image(image, request_scope=request_scope),
            remaining_timeout(self._timeout_seconds),
        )

    async def _upload_validated_image(
        self, image: DownloadedImage, *, request_scope: str,
    ) -> WorkflowInputImage:
        """URL 与内部字节共用摘要命名、请求目录和上传响应核验。"""
        extensions = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
        if image.media_type not in extensions:
            raise ComfyWorkflowError(
                "INVALID_IMAGE", "图生图仅支持 PNG、JPEG、WebP 静态图片", stage="image_validate",
            )
        digest = hashlib.sha256(image.data).hexdigest()
        reference = WorkflowInputImage(
            filename=f"{digest}.{extensions[image.media_type]}",
            subfolder=f"dispatch-chat-i2i/{request_scope}", media_type=image.media_type,
            size_bytes=len(image.data), sha256=digest,
        )
        try:
            response = await self._http.post(
                "upload/image", files={"image": (reference.filename, image.data, image.media_type)},
                data={"type": "input", "subfolder": reference.subfolder, "overwrite": "false"},
                timeout=remaining_timeout(self._timeout_seconds),
            )
        except httpx.TimeoutException:
            raise
        except httpx.RequestError as exc:
            raise ComfyWorkflowError("UNAVAILABLE", "原图上传不可用", stage="image_upload") from exc
        if not 200 <= response.status_code < 300:
            raise ComfyWorkflowError(
                "REJECTED", "原图上传被拒绝", stage="image_upload",
                status_code=response.status_code,
            )
        try:
            uploaded = _UploadResponse.model_validate(response.json())
            if uploaded.name != reference.filename or uploaded.subfolder != reference.subfolder:
                raise ValueError("upload reference differs")
        except (ValueError, ValidationError) as exc:
            raise ComfyWorkflowError(
                "INVALID_RESPONSE", "原图上传返回无效", stage="image_upload_response",
            ) from exc
        return reference

    async def generate(
        self,
        workflow: ApiWorkflow,
        *,
        output_node_id: str,
    ) -> GeneratedWorkflowImage:
        """执行一次工作流；`/prompt` 不重试，history 只轮询同一任务。"""

        started_at = time.monotonic()
        deadline = time.monotonic() + self._timeout_seconds
        prompt_id: str | None = None
        execution = active_image_task.get()
        try:
            if execution is not None:
                await execution.submitting(str(self._http.base_url))
            try:
                response = await self._http.post(
                    "prompt",
                    json={"client_id": str(uuid.uuid4()), "prompt": workflow},
                )
            except httpx.TimeoutException as exc:
                raise ComfyWorkflowError(
                    "TIMEOUT",
                    "本地工作流提交超时",
                    stage="prompt_submit",
                ) from exc
            except httpx.RequestError as exc:
                raise ComfyWorkflowError(
                    "UNAVAILABLE",
                    "本地工作流服务不可用",
                    stage="prompt_submit",
                ) from exc
            if response.status_code < 200 or response.status_code >= 300:
                raise ComfyWorkflowError(
                    "REJECTED",
                    "本地工作流拒绝了请求",
                    stage="prompt_submit",
                    status_code=response.status_code,
                )
            try:
                prompt_id = _PromptResponse.model_validate(response.json()).prompt_id
            except (ValueError, ValidationError) as exc:
                raise ComfyWorkflowError(
                    "INVALID_RESPONSE",
                    "本地工作流返回无效",
                    stage="prompt_response",
                    status_code=response.status_code,
                ) from exc

            if execution is not None:
                await execution.submitted(prompt_id, str(self._http.base_url))
            descriptor = await self._wait_for_image(
                prompt_id,
                output_node_id=output_node_id,
                deadline=deadline,
            )
            if execution is not None:
                await execution.check_cancelled()
            return await self._download_image(descriptor, deadline=deadline)
        except ComfyWorkflowError as error:
            cause = type(error.__cause__).__name__ if error.__cause__ is not None else "-"
            logger.warning(
                "Comfy 工作流调用失败：stage=%s code=%s status_code=%s "
                "prompt_id=%s output_node_id=%s elapsed_ms=%s cause=%s",
                error.stage,
                error.code,
                error.status_code if error.status_code is not None else "-",
                prompt_id or "-",
                output_node_id,
                round((time.monotonic() - started_at) * 1000),
                cause,
            )
            raise

    async def _wait_for_image(
        self,
        prompt_id: str,
        *,
        output_node_id: str,
        deadline: float,
    ) -> _ImageDescriptor:
        """在总超时内轮询单个 prompt 的 history。"""

        while time.monotonic() < deadline:
            execution = active_image_task.get()
            if execution is not None:
                await execution.check_cancelled()
            try:
                response = await self._http.get(
                    f"history/{prompt_id}",
                    timeout=max(0.001, deadline - time.monotonic()),
                )
            except httpx.TimeoutException as exc:
                raise ComfyWorkflowError(
                    "TIMEOUT",
                    "本地工作流执行超时",
                    stage="history_poll",
                ) from exc
            except httpx.RequestError as exc:
                raise ComfyWorkflowError(
                    "UNAVAILABLE",
                    "本地工作流服务不可用",
                    stage="history_poll",
                ) from exc
            if response.status_code < 200 or response.status_code >= 300:
                raise ComfyWorkflowError(
                    "UPSTREAM_ERROR",
                    "本地工作流状态查询失败",
                    stage="history_poll",
                    status_code=response.status_code,
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise ComfyWorkflowError(
                    "INVALID_RESPONSE",
                    "本地工作流返回无效",
                    stage="history_response",
                    status_code=response.status_code,
                ) from exc
            descriptor = self._history_image(
                payload,
                prompt_id=prompt_id,
                output_node_id=output_node_id,
            )
            if descriptor is not None:
                return descriptor
            if self._history_failed(payload, prompt_id=prompt_id):
                raise ComfyWorkflowError(
                    "EXECUTION_FAILED",
                    "本地工作流执行失败",
                    stage="workflow_execution",
                )
            await asyncio.sleep(self._poll_interval_seconds)
        raise ComfyWorkflowError(
            "TIMEOUT",
            "本地工作流执行超时",
            stage="history_poll",
        )

    def _history_image(
        self,
        payload: object,
        *,
        prompt_id: str,
        output_node_id: str,
    ) -> _ImageDescriptor | None:
        """从完成记录的指定输出节点提取唯一图片。"""

        if not isinstance(payload, Mapping):
            raise ComfyWorkflowError(
                "INVALID_RESPONSE",
                "本地工作流返回无效",
                stage="history_response",
            )
        record = payload.get(prompt_id)
        if record is None:
            return None
        if not isinstance(record, Mapping):
            raise ComfyWorkflowError(
                "INVALID_RESPONSE",
                "本地工作流返回无效",
                stage="history_response",
            )
        outputs = record.get("outputs")
        if not isinstance(outputs, Mapping):
            return None
        output = outputs.get(output_node_id)
        if not isinstance(output, Mapping):
            return None
        images = output.get("images")
        if not isinstance(images, list) or len(images) != 1:
            raise ComfyWorkflowError(
                "INVALID_RESPONSE",
                "工作流必须返回单张图片",
                stage="history_response",
            )
        try:
            descriptor = _ImageDescriptor.model_validate(images[0])
        except ValidationError as exc:
            raise ComfyWorkflowError(
                "INVALID_RESPONSE",
                "工作流图片引用无效",
                stage="history_response",
            ) from exc
        _validate_image_descriptor(descriptor)
        return descriptor

    def _history_failed(self, payload: object, *, prompt_id: str) -> bool:
        """只读取有限状态字段判断 Comfy 是否明确失败。"""

        if not isinstance(payload, Mapping):
            return False
        record = payload.get(prompt_id)
        if not isinstance(record, Mapping):
            return False
        status = record.get("status")
        if not isinstance(status, Mapping):
            return False
        status_text = status.get("status_str")
        return isinstance(status_text, str) and status_text.casefold() in {
            "error",
            "failed",
        }

    async def _download_image(
        self,
        descriptor: _ImageDescriptor,
        *,
        deadline: float,
    ) -> GeneratedWorkflowImage:
        """有界流式下载并核验 MIME 与真实文件签名。"""

        try:
            async with self._http.stream(
                "GET",
                "view",
                params=descriptor.model_dump(),
                timeout=max(0.001, deadline - time.monotonic()),
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise ComfyWorkflowError(
                        "UPSTREAM_ERROR",
                        "工作流图片读取失败",
                        stage="image_download",
                        status_code=response.status_code,
                    )
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        if int(content_length) > self._max_image_bytes:
                            raise ComfyWorkflowError(
                                "LIMIT_EXCEEDED",
                                "工作流图片过大",
                                stage="image_download",
                            )
                    except ValueError as exc:
                        raise ComfyWorkflowError(
                            "INVALID_RESPONSE",
                            "图片长度无效",
                            stage="image_download",
                        ) from exc
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > self._max_image_bytes:
                        raise ComfyWorkflowError(
                            "LIMIT_EXCEEDED",
                            "工作流图片过大",
                            stage="image_download",
                        )
                    chunks.append(chunk)
                data = b"".join(chunks)
                media_type = response.headers.get("Content-Type", "").partition(";")[0]
        except ComfyWorkflowError:
            raise
        except httpx.TimeoutException as exc:
            raise ComfyWorkflowError(
                "TIMEOUT",
                "工作流图片读取超时",
                stage="image_download",
            ) from exc
        except httpx.RequestError as exc:
            raise ComfyWorkflowError(
                "UNAVAILABLE",
                "本地工作流服务不可用",
                stage="image_download",
            ) from exc
        if media_type not in _SUPPORTED_MEDIA_TYPES or not _matches_signature(data, media_type):
            raise ComfyWorkflowError(
                "INVALID_RESPONSE",
                "工作流图片格式无效",
                stage="image_validate",
            )
        return GeneratedWorkflowImage(media_type=media_type, data=data)

    async def aclose(self) -> None:
        """关闭 Comfy HTTP 连接池。"""

        await self._http.aclose()


def _validate_image_descriptor(descriptor: _ImageDescriptor) -> None:
    """拒绝路径穿越和含控制字符的 Comfy 图片引用。"""

    if (
        descriptor.filename in {".", ".."}
        or "/" in descriptor.filename
        or "\\" in descriptor.filename
        or any(ord(character) < 32 for character in descriptor.filename)
    ):
        raise ComfyWorkflowError(
            "INVALID_RESPONSE",
            "工作流图片引用无效",
            stage="history_response",
        )
    if descriptor.subfolder:
        normalized = descriptor.subfolder.replace("\\", "/")
        if normalized.startswith("/") or any(
            part in {"", ".", ".."} for part in normalized.split("/")
        ):
            raise ComfyWorkflowError(
                "INVALID_RESPONSE",
                "工作流图片引用无效",
                stage="history_response",
            )
        if not re.fullmatch(r"[^\x00-\x1f]+", normalized):
            raise ComfyWorkflowError(
                "INVALID_RESPONSE",
                "工作流图片引用无效",
                stage="history_response",
            )


def _matches_signature(data: bytes, media_type: str) -> bool:
    """核对允许的图片 MIME 与魔数。"""

    if media_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if media_type == "image/webp":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    return data.startswith((b"GIF87a", b"GIF89a"))
