"""本地工作流执行编排与请求级图片收集。"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from agent_bot.database_pool import finish_database_write
from agent_bot.multimodal import DownloadedImage
from agent_bot.services.workflow_inputs import WorkflowInputImage
from agent_bot.services.workflows import (
    ApiWorkflow,
    WorkflowDefinition,
    WorkflowType,
    prepare_workflow_prompt,
    validate_workflow_bindings,
)

MAX_WORKFLOW_CALLS = 1
MAX_WORKFLOW_IMAGES = 1
MAX_WORKFLOW_TOTAL_BYTES = 10_485_760

logger = logging.getLogger(__name__)


class WorkflowExecutionError(RuntimeError):
    """可安全返回给 Agent 的本地工作流错误。"""

    def __init__(self, code: str, message: str) -> None:
        """保存稳定错误码和不含上游细节的摘要。"""

        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class GeneratedWorkflowImage:
    """已经下载并验证的单张本地工作流图片。"""

    media_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class WorkflowSummary:
    """允许暴露给 Agent 的工作流最小元数据。"""

    key: str
    name: str
    description: str
    workflow_type: WorkflowType = "text_to_image"
    requires_image: bool = False


class WorkflowImageRequest(BaseModel):
    """后台绘图任务保存的受限工作流参数，不接受地址或任意工作流内容。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    workflow_key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,127}$")
    positive_prompt: str = Field(min_length=1, max_length=20_000)
    negative_prompt: str = Field(default="", max_length=2_000)
    input_image: WorkflowInputImage | None = None
    workflow_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    fixed_reference: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def validate_image_snapshot(self) -> WorkflowImageRequest:
        """用户原图和摘要配对；固定底图只保存摘要，文生图不需要。"""
        if self.fixed_reference:
            if self.input_image is not None or self.workflow_sha256 is None:
                raise ValueError("fixed reference requires only a workflow snapshot")
        elif (self.input_image is None) != (self.workflow_sha256 is None):
            raise ValueError("image reference requires workflow snapshot")
        return self

    @model_serializer(mode="wrap")
    def serialize_compatible_request(
        self, handler: SerializerFunctionWrapHandler,
    ) -> dict[str, object]:
        """旧任务不新增默认字段，保留持久化操作签名和滚动发布兼容性。"""
        data = handler(self)
        if not self.fixed_reference:
            data.pop("fixed_reference", None)
        return data


class WorkflowStorePort(Protocol):
    """工作流执行服务使用的同步数据库端口。"""

    def list_executable(
        self,
        *,
        limit: int = 20,
    ) -> tuple[WorkflowDefinition, ...]: ...

    def get_executable(self, workflow_key: str) -> WorkflowDefinition: ...


class ComfyClientPort(Protocol):
    """工作流执行服务使用的异步 Comfy 端口。"""

    async def upload_input_image(
        self, source_url: str, *, request_scope: str,
    ) -> WorkflowInputImage: ...

    async def upload_input_bytes(self, data: bytes, *, media_type: str,
                                 request_scope: str) -> WorkflowInputImage:
        """接收服务器已校验原图，不接受客户端存储路径。"""
        ...

    async def generate(
        self,
        workflow: ApiWorkflow,
        *,
        output_node_id: str,
    ) -> GeneratedWorkflowImage: ...


class WorkflowImageCollector:
    """并发安全地限制并保留一次 Agent 请求生成的图片。"""

    def __init__(
        self,
        *,
        max_calls: int = MAX_WORKFLOW_CALLS,
        max_images: int = MAX_WORKFLOW_IMAGES,
        max_total_bytes: int = MAX_WORKFLOW_TOTAL_BYTES,
    ) -> None:
        """创建不能超过全局硬上限的请求级收集器。"""

        if not 1 <= max_calls <= MAX_WORKFLOW_CALLS:
            raise ValueError("workflow call limit is invalid")
        if not 1 <= max_images <= MAX_WORKFLOW_IMAGES:
            raise ValueError("workflow image limit is invalid")
        if not 1 <= max_total_bytes <= MAX_WORKFLOW_TOTAL_BYTES:
            raise ValueError("workflow byte limit is invalid")
        self._max_calls = max_calls
        self._max_images = max_images
        self._max_total_bytes = max_total_bytes
        self._calls = 0
        self._total_bytes = 0
        self._images: list[GeneratedWorkflowImage] = []
        self._lock = asyncio.Lock()
        self.submit: Callable[[WorkflowImageRequest, str], Awaitable[str]] | None = None
        self.queued_task_id: str | None = None

    @property
    def images(self) -> tuple[GeneratedWorkflowImage, ...]:
        """按成功加入的调用顺序返回不可变图片快照。"""

        return tuple(self._images)

    async def reserve_call(self) -> int:
        """原子占用调用额度，提交后台任务也计入本次调用上限。"""

        async with self._lock:
            if self._calls >= self._max_calls:
                raise WorkflowExecutionError("LIMIT_EXCEEDED", "本次工作流调用次数已达上限")
            self._calls += 1
            call_index = self._calls
        return call_index

    async def add(self, image: GeneratedWorkflowImage) -> int:
        """校验并保存图片，返回从 1 开始的生成序号。"""

        if not image.data or image.media_type not in {
            "image/png",
            "image/jpeg",
            "image/webp",
            "image/gif",
        }:
            raise WorkflowExecutionError("INVALID_RESULT", "工作流返回的图片无效")
        async with self._lock:
            if len(self._images) >= self._max_images:
                raise WorkflowExecutionError("LIMIT_EXCEEDED", "本次生成图片数量已达上限")
            total_bytes = self._total_bytes + len(image.data)
            if total_bytes > self._max_total_bytes:
                raise WorkflowExecutionError("LIMIT_EXCEEDED", "本次生成图片总大小已达上限")
            self._images.append(image)
            self._total_bytes = total_bytes
            return len(self._images)


class WorkflowExecutionService:
    """从数据库取模板、写入提示词并调用固定 Comfy 客户端。"""

    def __init__(self, *, store: WorkflowStorePort, client: ComfyClientPort) -> None:
        """保存数据库和 Comfy 两个明确端口。"""

        self._store = store
        self._client = client

    async def list_workflows(self) -> tuple[WorkflowSummary, ...]:
        """异步读取可执行工作流的受控元数据。"""

        definitions = await finish_database_write(lambda: self._store.list_executable(limit=20))
        return tuple(
            WorkflowSummary(
                key=definition.workflow_key,
                name=definition.display_name,
                description=definition.description,
                workflow_type=definition.workflow_type,
                requires_image=definition.workflow_type == "image_to_image",
            )
            for definition in definitions
        )

    async def describe_workflow(self, workflow_key: str) -> WorkflowSummary:
        """只读确定工具操作的工作流类型，不触发上传或生成。"""
        definition = await finish_database_write(lambda: self._store.get_executable(workflow_key))
        validate_workflow_bindings(definition)
        return WorkflowSummary(
            key=definition.workflow_key, name=definition.display_name,
            description=definition.description, workflow_type=definition.workflow_type,
            requires_image=definition.workflow_type == "image_to_image",
        )

    async def prepare_request(
        self, *, workflow_key: str, positive_prompt: str, negative_prompt: str,
        image_urls: tuple[str, ...] = (), request_scope: str,
        input_image: DownloadedImage | None = None,
    ) -> WorkflowImageRequest:
        """两种图像来源互斥；图生图上传唯一原图，固定底图不被用户覆盖。"""
        if input_image is not None and image_urls:
            raise WorkflowExecutionError("INVALID_IMAGE", "图片来源不能混用")
        definition = await finish_database_write(lambda: self._store.get_executable(workflow_key))
        validate_workflow_bindings(definition)
        request = WorkflowImageRequest(
            workflow_key=workflow_key, positive_prompt=positive_prompt,
            negative_prompt=negative_prompt,
        )
        if definition.workflow_type == "text_to_image":
            return request
        if definition.workflow_type == "fixed_reference":
            return request.model_copy(update={
                "fixed_reference": True, "workflow_sha256": definition.execution_sha256,
            })
        urls = tuple(dict.fromkeys(image_urls))
        if (input_image is None and len(urls) != 1) or (input_image is not None and urls):
            raise WorkflowExecutionError("INVALID_IMAGE", "图生图需要提供或引用恰好一张原图")
        # 先校验摘要，避免无效配置触发外部上传。
        if len(definition.content_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in definition.content_sha256
        ):
            raise WorkflowExecutionError("INVALID_WORKFLOW", "工作流版本无效")
        uploaded = (
            await self._client.upload_input_bytes(
                input_image.data, media_type=input_image.media_type, request_scope=request_scope,
            )
            if input_image is not None else
            await self._client.upload_input_image(urls[0], request_scope=request_scope)
        )
        if uploaded.subfolder != f"chat-i2i/{request_scope}":
            raise WorkflowExecutionError("INVALID_IMAGE", "原图不属于本次请求")
        return WorkflowImageRequest(
            workflow_key=workflow_key, positive_prompt=positive_prompt,
            negative_prompt=negative_prompt, input_image=uploaded,
            workflow_sha256=definition.execution_sha256,
        )

    async def generate(
        self,
        *,
        workflow_key: str,
        positive_prompt: str,
        negative_prompt: str,
        input_image: WorkflowInputImage | None = None,
        workflow_sha256: str | None = None,
    ) -> GeneratedWorkflowImage:
        """执行一次数据库工作流；取消时等待查询线程结束再释放运行时。"""

        started_at = time.monotonic()
        definition: WorkflowDefinition | None = None
        output_node_id: str | None = None
        seed: int | None = None
        stage = "workflow_load"
        try:
            definition = await finish_database_write(
                lambda: self._store.get_executable(workflow_key)
            )
            if definition.workflow_type == "image_to_image":
                if input_image is None:
                    raise WorkflowExecutionError("INVALID_IMAGE", "图生图需要一张原图")
                if workflow_sha256 != definition.execution_sha256:
                    raise WorkflowExecutionError("WORKFLOW_CHANGED", "工作流已更新，请重新发起请求")
            elif definition.workflow_type == "fixed_reference":
                if input_image is not None or workflow_sha256 != definition.execution_sha256:
                    raise WorkflowExecutionError(
                        "WORKFLOW_CHANGED", "固定底图工作流已更新，请重新发起请求",
                    )
            elif input_image is not None or workflow_sha256 is not None:
                raise WorkflowExecutionError("WORKFLOW_CHANGED", "工作流类型已变更，请重新发起请求")
            seed = secrets.randbits(63)
            stage = "workflow_prepare"
            prompt = prepare_workflow_prompt(
                definition,
                positive_prompt=positive_prompt,
                negative_prompt=negative_prompt,
                seed=seed,
                input_image=input_image,
            )
            output_node_id = definition.output_node_id
            if output_node_id is None:
                raise WorkflowExecutionError("INVALID_WORKFLOW", "工作流输出节点未配置")
            stage = "comfy_generate"
            return await self._client.generate(prompt, output_node_id=output_node_id)
        except Exception as error:
            error_code = (
                error.code if isinstance(error, WorkflowExecutionError) else "INTERNAL"
            )
            logger.warning(
                "本地工作流执行失败：stage=%s workflow_key=%s workflow_sha256=%s "
                "output_node_id=%s seed=%s positive_chars=%s negative_chars=%s "
                "code=%s error=%s elapsed_ms=%s",
                stage,
                workflow_key,
                definition.content_sha256 if definition is not None else "-",
                output_node_id or "-",
                seed if seed is not None else "-",
                len(positive_prompt),
                len(negative_prompt),
                error_code,
                type(error).__name__,
                round((time.monotonic() - started_at) * 1000),
            )
            raise
