"""QQ主动发送受限文件，复用当前会话素材权限与持久发送回执。"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_bot.ai.errors import AiError
from agent_bot.database_pool import finish_database_write
from agent_bot.materials.contracts import MAX_FILE_BYTES
from agent_bot.media.contracts import MediaOwner
from agent_bot.media.errors import MediaError
from agent_bot.services.tool_operations import FileEffect, ToolOperationConflict

if TYPE_CHECKING:
    from agent_bot.services.chat_tools import ChatToolContext

logger = logging.getLogger(__name__)


class FileUploadConfirmedError(RuntimeError):
    """宿主已获得平台成功响应，但后续出站记录失败；不能把该状态当作上传未知。"""

    def __init__(self, error_type: str) -> None:
        """只携带安全异常类型，不向模型或日志泄漏原件、密钥和平台响应。"""
        super().__init__("file upload confirmed; recording incomplete")
        self.error_type = error_type


def validate_file_name(name: str) -> str:
    """只接受平台可用的单个文件名，不能用目录、控制字符或Windows设备名。"""
    if (not name or len(name) > 255 or name != name.strip() or name.endswith(".")
            or any(c in '/\\:*?"<>|' or unicodedata.category(c).startswith("C") for c in name)
            or name in {".", ".."}
            or re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", name.split(".")[0], re.I)):
        raise ValueError("invalid file name")
    return name


class FileSendArguments(BaseModel):
    """文本与素材二选一；接收人、文件路径和URL均不能由模型指定。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str | None = Field(default=None, min_length=1, max_length=255,
                             description="发送文件名，不含目录；文本生成时必填")
    content: str | None = Field(default=None, min_length=1, max_length=16000,
                                description="UTF-8文本或代码原文，保留换行；与material_id二选一")
    material_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$",
                                    description="本会话可访问的真实素材ID，先用material_search查询")

    @field_validator("name")
    @classmethod
    def safe_name(cls, value: str | None) -> str | None:
        """显式提供的名称必须是安全单文件名，省略时使用已授权素材的名称。"""
        return validate_file_name(value) if value is not None else None

    @model_validator(mode="after")
    def exactly_one_source(self) -> FileSendArguments:
        """拒绝来源缺失、冲突或无法确定名称的文本文件。"""
        if (self.content is None) == (self.material_id is None):
            raise ValueError("exactly one file source is required")
        if self.content is not None and (self.name is None or not self.content.strip()):
            raise ValueError("text file requires a name and visible content")
        return self


async def file_send(arguments: dict, context: ChatToolContext) -> dict:
    """平台确认后才返回sent；同任务同参数复用回执，未知结果不能再次上传。"""
    from agent_bot.services.chat_tools import ChatToolError

    args = FileSendArguments.model_validate(arguments)
    if context.bot is None or context.file_sender is None or context.material_store is None:
        raise ChatToolError("FORBIDDEN", "当前会话未开放文件发送")
    signature = hashlib.sha256(args.model_dump_json().encode()).hexdigest()
    operation_id = hashlib.sha256(
        f"{context.job_id or context.workflow_request_scope}:file:{signature}".encode(),
    ).hexdigest()
    owner = MediaOwner(entry="qq", session_id=context.session_id)
    async with context.file_lock:
        context.ensure_active()
        saved = context.file_operations.get(signature)
        if signature in context.file_operations and saved is None:
            raise ToolOperationConflict("文件发送结果尚未确认，不能重复上传")
        if saved is None and context.operation_store is not None and context.job_id is not None:
            saved = await context.operation_store.begin(operation_id, "file_send", signature)
        if saved is not None and not isinstance(saved, FileEffect):
            raise ValueError("invalid saved file result")
        if saved is None:
            try:
                if args.material_id is not None:
                    source = await context.material_store.describe(
                        args.material_id, owner=owner,
                        allow_qq_library=context.material_library_access,
                    )
                    name = validate_file_name(args.name or source.name)
                    mime = source.mime_type
                    async with context.material_store.open_content(
                        source.id, owner=owner, allow_qq_library=context.material_library_access,
                    ) as file:
                        data = await finish_database_write(lambda: file.read(MAX_FILE_BYTES + 1))
                    category = None
                else:
                    name = args.name
                    data = args.content.encode("utf-8")
                    mime = {"json": "application/json", "md": "text/markdown",
                            "csv": "text/csv", "py": "text/x-python"}.get(
                                name.rsplit(".", 1)[-1].lower(), "text/plain",
                            )
                    category = "generated"
                if not 0 < len(data) <= MAX_FILE_BYTES:
                    raise ValueError("file size is invalid")
                # 跨QQ读取仅由真人主人授权；复制到当前会话后，投递与后续读取均归当前会话。
                asset = await context.material_store.put(
                    owner=owner, name=name, mime_type=mime, data=data, category=category,
                    allow_animation=mime.startswith("image/"),
                )
            except MediaError as error:
                raise ChatToolError(error.code, "文件素材不可访问或不可用") from error
            context.ensure_active()
            context.file_operations[signature] = None
            record_warning = False
            try:
                await context.file_sender(
                    context.bot, scene="group" if context.group_id is not None else "private",
                    target_id=(str(context.group_id)
                               if context.group_id is not None else context.user_id),
                    name=name, data=data, mime_type=mime, request_id=operation_id,
                    key=f"file-send:{signature}", session_id=context.session_id,
                )
            except FileUploadConfirmedError as error:
                # 平台已成功，不重传；出站日志由现有回执补写链路修复，工具保留真实发送状态。
                record_warning = True
                logger.warning("文件已上传，出站记录待修复：operation_id=%s error=%s",
                               operation_id, error.error_type)
            saved = FileEffect(material_id=asset.id, name=name, size_bytes=len(data),
                               record_warning=record_warning)
            context.file_operations[signature] = saved
        # 本地已有平台确认时也补写回执，修复确认存储失败后同请求重试的持久状态。
        if context.operation_store is not None and context.job_id is not None:
            await context.operation_store.complete(operation_id, "file_send", signature, saved)
        try:
            await context.material_store.add_category(saved.material_id, "sent", owner=owner)
        except AiError as error:
            # 分类是上传后的元数据；记录告警不能否认平台确认，也不能触发再次上传。
            saved = saved.model_copy(update={"record_warning": True})
            if context.operation_store is not None and context.job_id is not None:
                await context.operation_store.complete(operation_id, "file_send", signature, saved)
            logger.warning("文件已上传，素材分类待保存：operation_id=%s error=%s",
                           operation_id, type(error).__name__)
        context.file_operations[signature] = saved
        return {"status": "sent", **saved.model_dump(exclude={"kind"})}
