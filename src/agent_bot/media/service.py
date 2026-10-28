"""QQ 与管理端共用的直接媒体生成用例，负责预算、记录及附件发布。"""

import asyncio
import time
from uuid import uuid4

from agent_bot.ai.errors import AiError
from agent_bot.database_pool import finish_database_write
from agent_bot.media.contracts import (
    MediaGenerationResult,
    MediaRunFinish,
    MediaRunStart,
    MediaScope,
)
from agent_bot.media.errors import MediaError
from agent_bot.media.providers import validate_generation_request
from agent_bot.media.store import finish_cleanup, utc_now
from agent_bot.media.validation import validate_generated_media
from agent_bot.services.request_deadline import remaining_timeout, request_deadline


class MediaGenerationService:
    """准备和持久化都占用进程生成名额，保存失败不会重调上游。"""

    def __init__(self, catalog, credentials, provider, resources):
        """热刷新仅替换调用客户端，共享进程存储和容量。"""
        self.catalog, self.credentials, self.provider, self.resources = (
            catalog,
            credentials,
            provider,
            resources,
        )

    async def generate(self, request, owner, *, scope=None):
        """限制整个请求预算；Agent 内部可绑定当前执行，公开请求不能指定归属。"""
        self.resources.require_ready()
        if owner.session_id != request.session_id or (scope is not None and scope.owner != owner):
            raise MediaError("MEDIA_INPUT_UNAUTHORIZED", status_code=403)
        budget = min(
            120,
            request.timeout_seconds,
            request.request_timeout_millis / 1000 if request.request_timeout_millis else 120,
        )
        async with self.resources.generations.slot():
            with request_deadline(budget):
                try:
                    return await asyncio.wait_for(
                        self._generate(request, owner, scope=scope), remaining_timeout(budget)
                    )
                except asyncio.TimeoutError as error:
                    raise MediaError("MEDIA_TIMEOUT", status_code=504) from error

    async def _generate(self, request, owner, *, scope=None):
        """独立生成保存媒体记录；工具生成由 Agent 记录承载并直接关联原执行附件。"""
        scope = scope or MediaScope(owner=owner, execution_id=uuid4().hex, execution_kind="media")
        execution_id, started = scope.execution_id, time.monotonic()
        resolved = await self.catalog.resolve(request.model)
        validate_generation_request(request, resolved)
        credential = self.credentials.resolve(resolved.provider)
        saved, begun, status, error_code, text = True, False, "failed", None, ""
        try:
            if scope.execution_kind == "media":
                try:
                    await self.resources.records.begin(
                        MediaRunStart(
                            execution_id=execution_id,
                            entry=owner.entry,
                            session_id=owner.session_id,
                            request_id=request.request_id,
                            model=request.model,
                            output_type=request.output_type,
                            created_at=utc_now(),
                            prompt=request.prompt
                            or "\n".join(m.content for m in request.messages)[-32000:],
                        )
                    )
                    begun = True
                except Exception:
                    saved = False
            response = await self.provider.generate(request, resolved, credential)
            data = await finish_database_write(
                lambda: validate_generated_media(
                    response,
                    output_type=request.output_type,
                    format=request.image_format or request.audio_format,
                    max_bytes=request.max_bytes,
                )
            )
            remaining_timeout(request.timeout_seconds)
            try:
                asset = await self.resources.store.put(
                    owner=owner,
                    scope=scope,
                    purpose="output",
                    data=data,
                    mime_type=response.items[0].mime_type,
                )
                if request.output_type == "IMAGE":
                    # 生成原件先持久保存，管理端未发送、QQ 后续发送失败也不能丢失图片。
                    await self.resources.materials.put(
                        owner=owner, name="生成图片", data=data,
                        mime_type=response.items[0].mime_type, category="generated",
                    )
            except Exception as error:
                raise MediaError("MEDIA_SAVE_FAILED") from error
            text, status = response.text or "", "completed"
        except asyncio.CancelledError:
            status, error_code = "cancelled", "MEDIA_CANCELLED"
            raise
        except AiError as error:
            error_code = error.code
            raise
        except Exception as error:
            error_code = "MEDIA_GENERATION_FAILED"
            raise MediaError(error_code) from error
        finally:
            if begun:
                try:
                    await finish_cleanup(
                        self.resources.records.finish(
                            MediaRunFinish(
                                execution_id=execution_id,
                                status=status,
                                duration_ms=int((time.monotonic() - started) * 1000),
                                content=text,
                                error_code=error_code,
                            )
                        )
                    )
                except Exception:
                    saved = False
        return MediaGenerationResult(
            execution_id=execution_id,
            request_id=request.request_id,
            model=request.model,
            text=response.text,
            attachments=(asset,),
            duration_ms=int((time.monotonic() - started) * 1000),
            record_saved=saved,
            response=response,
        )

    async def aclose(self):
        """运行时只关闭供应商连接，进程资源留给 application 最后关闭。"""
        await self.provider.aclose()
