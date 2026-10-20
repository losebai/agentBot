"""智能体执行配额、配置快照、输入租约、取消和尽力保存的记录。"""

import asyncio
import time
import uuid
from contextlib import AsyncExitStack, aclosing
from datetime import datetime, timezone

import anyio

from agent_bot.agent.contracts import (
    AgentCompletion,
    AgentEvent,
    AgentModelCall,
    RunFinish,
    RunStart,
    StepWrite,
    TurnMessage,
)
from agent_bot.agent.engine import AgentEngine, PreparedRun
from agent_bot.agent.errors import PARTIAL_REPLY_ERRORS, AgentError
from agent_bot.agent.model_routing import AgentModelRouter
from agent_bot.agent.tools.policy import LEGACY_TOOLS
from agent_bot.ai.contracts import TokenUsage
from agent_bot.ai.errors import AiError
from agent_bot.database_pool import finish_database_write
from agent_bot.media.contracts import MediaOwner, MediaScope
from agent_bot.media.inputs import ImagePreparer
from agent_bot.media.store import finish_cleanup
from agent_bot.models import LlmRequestOptions
from agent_bot.services.request_deadline import (
    clear_request_deadline,
    remaining_timeout,
    request_deadline,
)


class AgentService:
    """入口间共用执行限制，媒体资源由外层进程持有。"""

    def __init__(
        self,
        catalog,
        records,
        model_credentials,
        providers,
        tool_runtime,
        *,
        search_code=None,
        media_store=None,
        material_store=None,
        image_downloader=None,
        maximum_images=4,
    ):
        """注入媒体边界以便离线验证，不在构造时下载或读文件。"""
        self.catalog, self.records, self.credentials = catalog, records, model_credentials
        self.providers, self.tool_runtime, self.search_code = providers, tool_runtime, search_code
        self.active = 0
        self.media_store, self.image_downloader = media_store, image_downloader
        self.material_store = material_store
        self.maximum_images = min(maximum_images, 8)

    @property
    def active_requests(self) -> int:
        """暴露实际活动请求数，完成和取消沿用既有 finally 归还计数。"""
        return self.active

    async def readiness(self, agent_code=None):
        return await self.catalog.readiness(agent_code)

    async def _prepare(self, request, authority, execution_id, inputs):
        """先固定并检查能力，再准备凭据和媒体，禁止静默移除未实现的预设工具。"""
        snapshot = await self.catalog.snapshot(
            request,
            search_code=(self.search_code if "web_search" in authority.allowed_tools else None),
        )
        if (snapshot.agent and set(snapshot.agent.tools) & LEGACY_TOOLS) or any(
            s.code == "laya" for s in snapshot.mcps
        ):
            raise AgentError("AGENT_CAPABILITY_NOT_MIGRATED", status_code=422)
        if authority.entry == "admin":
            # 管理入口只接受可信快照授予的远端能力，QQ 继续使用原消息授权交集。
            authority = authority.model_copy(
                update={"allowed_mcp_services": tuple(s.code for s in snapshot.mcps)}
            )
        model = snapshot.resolved
        scope = MediaScope(
            owner=MediaOwner(entry=authority.entry, session_id=request.session_id),
            execution_id=execution_id,
            execution_kind="agent",
        )
        if (authority.entry == "admin" and request.image_urls) or (
            authority.entry == "qq" and request.attachment_ids
        ):
            raise AgentError("MEDIA_INPUT_UNAUTHORIZED", status_code=403)
        has_images = bool(
            request.image_urls or request.attachment_ids or request.image_material_ids
        )
        if (
            has_images
            and request.tools is not None
            and not set(request.tools) <= set(authority.allowed_tools)
        ):
            raise AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403)
        options = LlmRequestOptions(
            **{
                **(snapshot.agent.options.model_dump(exclude_none=True) if snapshot.agent else {}),
                **request.options.model_dump(exclude_none=True),
            }
        )
        router = AgentModelRouter(
            snapshot, self.providers, self.credentials, options,
            has_images=has_images, timeout_seconds=request.timeout_seconds,
        )
        model, provider, credential = router.resolved, router.provider, router.credential
        maximum = snapshot.agent.max_steps if snapshot.agent else 3
        if request.max_steps is not None:
            if snapshot.agent and request.max_steps > maximum:
                raise AgentError("AGENT_TOOL_LIMIT", status_code=422)
            maximum = request.max_steps
        messages = [TurnMessage(role=m.role, text=m.content) for m in request.messages]
        if not messages or messages[-1].role != "user":
            messages.append(TurnMessage(role="user", text=request.prompt))
        if snapshot.agent:
            messages.insert(0, TurnMessage(role="system", text=snapshot.agent.system_prompt))
        images = ()
        if has_images:
            if len(request.image_urls or request.attachment_ids or request.image_material_ids
                   ) > self.maximum_images:
                raise AgentError("MEDIA_TOO_MANY_IMAGES", status_code=422)
            if request.image_material_ids:
                if self.material_store is None:
                    raise AgentError("MATERIAL_NOT_INITIALIZED", status_code=503)
                prepared = []
                for id in request.image_material_ids:
                    async with self.material_store.open_content(id, owner=scope.owner) as file:
                        data = await finish_database_write(file.read)
                    prepared.append(await ImagePreparer().prepare_async(data, None))
                    ImagePreparer().prepare_many(prepared, maximum_count=self.maximum_images)
                images = tuple(prepared)
            elif request.attachment_ids:
                if self.media_store is None:
                    raise AgentError("MEDIA_NOT_READY", status_code=503)
                images = await inputs.enter_async_context(
                    self.media_store.bind_inputs(request.attachment_ids, scope)
                )
            else:
                if self.image_downloader is None:
                    raise AgentError("MEDIA_NOT_READY", status_code=503)
                downloaded = []
                for url in request.image_urls:
                    downloaded.append(await self.image_downloader.fetch(url))
                    ImagePreparer().prepare_many(downloaded, maximum_count=self.maximum_images)
                images = tuple(downloaded)
            model_images = ImagePreparer().prepare_many(images, maximum_count=self.maximum_images)
            messages[-1] = messages[-1].model_copy(update={"images": model_images})
        call = AgentModelCall(
            resolved=model,
            credential=credential,
            messages=tuple(messages),
            options=options,
            timeout_seconds=remaining_timeout(request.timeout_seconds),
        )
        return PreparedRun(
            execution_id, request, snapshot, authority, call, maximum, scope, images, router
        ), provider

    async def complete(self, request, authority):
        """QQ 超时可携带当前轮正文，调用仍抛错并保存失败终态。"""
        text, result = "", None
        async with aclosing(self.stream(request, authority)) as events:
            async for event in events:
                if event.kind == "model_start":
                    text = ""
                elif event.kind == "text_delta":
                    text += event.delta or ""
                elif event.kind == "error":
                    raise AgentError(
                        event.code or "AGENT_FAILED", status_code=event.status_code or 502,
                        partial_content=text if (
                            authority.entry == "qq" and event.code in PARTIAL_REPLY_ERRORS
                        ) else "",
                    )
                elif event.kind == "done":
                    result = AgentCompletion(content=text, **event.metadata.model_dump())
        if result is None:
            raise AgentError("AGENT_FAILED")
        return result

    async def stream(self, request, authority):
        """活动计数包含图片准备；退出前先归还所有输入租约。"""
        if self.active >= 8:
            raise AgentError("AGENT_BUSY", status_code=429)
        self.active += 1
        execution_id, started = uuid.uuid4().hex, time.monotonic()
        sequence, text, saved, prepared, pending_tool = 0, "", True, None, None
        pending_started = None
        begun, final, error_code, status = False, None, None, "failed"
        inputs = AsyncExitStack()
        artifacts = []

        def event(kind, **values):
            """统一分配执行内事件序号。"""
            nonlocal sequence
            sequence += 1
            return AgentEvent(kind=kind, execution_id=execution_id, sequence=sequence, **values)

        async def save(method, value):
            """记录故障只影响保存标记，不能触发重复执行。"""
            nonlocal saved
            try:
                timeout = remaining_timeout(min(request.timeout_seconds, 10))
                await asyncio.wait_for(method(value), timeout)
                return True
            except Exception:
                saved = False
                return False

        try:
            with request_deadline(request.timeout_seconds):
                try:
                    timeout = remaining_timeout(request.timeout_seconds)
                    prepared, provider = await asyncio.wait_for(
                        self._prepare(request, authority, execution_id, inputs), timeout
                    )
                    begun = await save(
                        self.records.begin,
                        RunStart(
                            execution_id=execution_id,
                            session_id=request.session_id,
                            request_id=request.request_id,
                            entry=authority.entry,
                            model=prepared.call.resolved.model.upstream_model,
                            prompt=request.prompt,
                            revisions=prepared.snapshot.revisions,
                            request_context={
                                'agent_code': request.agent_code,
                                'model_routes': {
                                    'primary': prepared.snapshot.resolved.model.reference,
                                    'fallback': prepared.snapshot.fallback.model.reference
                                    if prepared.snapshot.fallback else None,
                                },
                                'model': prepared.call.resolved.model.upstream_model,
                                'messages': [
                                    {'role': message.role, 'content': message.text}
                                    for message in prepared.call.messages
                                ],
                                'options': prepared.call.options.model_dump(exclude_none=True),
                                'tools': list(prepared.authority.allowed_tools),
                                'mcp_services': list(prepared.authority.allowed_mcp_services),
                                'max_steps': prepared.max_steps,
                            },
                            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                        ),
                    )
                    yield event("start")

                    async def execute():
                        """工具上下文和模型循环共用本次请求的资源期限。"""
                        async with self.tool_runtime.open(
                            prepared.snapshot,
                            request,
                            prepared.authority,
                            media_scope=prepared.media_scope,
                            images=prepared.images,
                        ) as tools:
                            if prepared.model_router.initial_warning:
                                yield AgentEvent(
                                    kind="warning", execution_id=execution_id, sequence=0,
                                    code="AGENT_MODEL_FORWARDED",
                                    message=prepared.model_router.initial_warning,
                                )
                            for warning in tools.warnings:
                                yield AgentEvent(
                                    kind="warning",
                                    execution_id=execution_id,
                                    sequence=0,
                                    code=warning.code,
                                    message=warning.service_code,
                                )
                            async with aclosing(
                                AgentEngine(provider).run(prepared, tools)
                            ) as events:
                                async for item in events:
                                    yield item

                    async with aclosing(execute()) as stream:
                        while True:
                            try:
                                timeout = remaining_timeout(request.timeout_seconds)
                                item = await asyncio.wait_for(anext(stream), timeout)
                            except StopAsyncIteration:
                                break
                            if item.kind == "done":
                                final = item.metadata
                                continue
                            sequence += 1
                            item = item.model_copy(update={"sequence": sequence})
                            if item.kind == "model_start":
                                text = ""
                            if item.kind == "text_delta":
                                text += item.delta or ""
                            if item.kind == "artifact":
                                artifacts.append(item.attachment)
                            if item.kind == "tool_start":
                                pending_tool = item
                                pending_started = time.monotonic()
                            elif item.kind == "tool_result":
                                pending_tool = None
                            if begun and item.kind in {
                                "model_start",
                                "tool_start",
                                "tool_result",
                                "warning",
                            }:
                                await save(
                                    self.records.append_step,
                                    StepWrite(
                                        execution_id=execution_id,
                                        sequence=sequence,
                                        turn=item.turn,
                                        kind=item.kind,
                                        tool=item.tool,
                                        call_id=item.call_id,
                                        duration_ms=item.duration_ms,
                                        status="failed"
                                        if item.code
                                        else "completed"
                                        if item.kind == "tool_result"
                                        else "started",
                                        summary=item.message
                                        if item.code == "AGENT_MODEL_FORWARDED"
                                        else item.code or "",
                                        arguments=item.arguments,
                                        result=item.result,
                                    ),
                                )
                            yield item
                    if final is None:
                        raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
                    status = "completed"
                except (asyncio.CancelledError, GeneratorExit):
                    status, error_code = "cancelled", "AGENT_CANCELLED"
                    raise
                except asyncio.TimeoutError:
                    error_code = "AGENT_TIMEOUT"
                    yield event("error", code=error_code, message="Agent 执行超时", status_code=504)
                except AiError as error:
                    error_code = error.code
                    yield event(
                        "error",
                        code=error.code,
                        message="Agent 未能完成请求",
                        status_code=error.status_code,
                    )
                except Exception:
                    error_code = "AGENT_INTERNAL_ERROR"
                    yield event(
                        "error", code=error_code, message="Agent 未能完成请求", status_code=500
                    )
                finally:
                    # 模型预算已耗尽时仍用 save 的最多十秒完成归档，不能重启模型或工具。
                    with anyio.CancelScope(shield=True), clear_request_deadline():
                        if begun:
                            if pending_tool is not None:
                                sequence += 1
                                await save(
                                    self.records.append_step,
                                    StepWrite(
                                        execution_id=execution_id,
                                        sequence=sequence,
                                        turn=pending_tool.turn,
                                        kind="tool_result",
                                        tool=pending_tool.tool,
                                        call_id=pending_tool.call_id,
                                        status="unknown",
                                        summary="AGENT_TOOL_OUTCOME_UNKNOWN",
                                        arguments=pending_tool.arguments,
                                        result={
                                            "error_code": (
                                                error_code or "AGENT_TOOL_OUTCOME_UNKNOWN"
                                            ),
                                            "outcome": "unknown",
                                        },
                                        duration_ms=int(
                                            (time.monotonic() - pending_started) * 1000
                                        ),
                                    ),
                                )
                            await save(
                                self.records.finish,
                                RunFinish(
                                    execution_id=execution_id,
                                    status=status,
                                    content=text,
                                    duration_ms=int((time.monotonic() - started) * 1000),
                                    model=final.model if final else None,
                                    usage=final.usage if final else TokenUsage(),
                                    error_code=error_code,
                                ),
                            )
                if status == "completed":
                    final = final.model_copy(
                        update={
                            "record_saved": saved,
                            "duration_ms": int((time.monotonic() - started) * 1000),
                            "attachments": tuple(artifacts),
                        }
                    )
                    yield event("done", metadata=final)
        finally:
            try:
                await finish_cleanup(inputs.aclose())
            finally:
                self.active -= 1

    async def aclose(self):
        for provider in self.providers.values():
            await provider.aclose()
