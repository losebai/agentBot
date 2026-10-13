"""普通模型调用共享并发预算、取消和归档边界，并暴露当前对话活动计数。"""

import asyncio
import logging
import time
import uuid
from collections.abc import Mapping
from contextlib import aclosing, asynccontextmanager
from datetime import datetime, timezone

from agent_bot.ai.catalog import AiCatalog
from agent_bot.ai.contracts import (
    AiCompletion,
    AiEvent,
    CatalogReadiness,
    ProviderCall,
    ProviderProtocol,
    SessionWrite,
    TextRequest,
    TokenUsage,
)
from agent_bot.ai.credentials import CredentialResolver
from agent_bot.ai.errors import AiError
from agent_bot.ai.model_discovery import discover_models
from agent_bot.ai.providers.base import TextProvider, require_text
from agent_bot.ai.sessions import AiSessionStore
from agent_bot.services.request_deadline import remaining_timeout, request_deadline

logger = logging.getLogger(__name__)


class AiService:
    """普通模型调用共用并发名额，独立统计对话活动并保留取消和归档边界。"""

    def __init__(
        self,
        catalog: AiCatalog,
        sessions: AiSessionStore,
        credentials: CredentialResolver,
        providers: Mapping[ProviderProtocol, TextProvider],
        *,
        max_concurrent_requests: int = 8,
    ):
        """注入目录、记录与协议供应商，模型发现与生成共用并发预算。"""
        self.catalog, self.sessions, self.credentials = catalog, sessions, credentials
        self.providers = providers
        self._maximum = max_concurrent_requests
        self._active = 0
        self._active_requests = 0
        self._closed = False

    @property
    def active_requests(self) -> int:
        """当前普通对话请求数，不将模型目录发现请求算作正在生成的对话。"""
        return self._active_requests

    def credential_available(self, provider) -> bool:
        """供应商就绪状态只检查数据库快照中的凭据。"""
        return self.credentials.is_available(provider)

    async def upstream_models(self, provider_id, expected_revision):
        """模型发现与生成共用活动配额和协议传输，退出时归还名额。"""
        async with self._slot(conversation=False):
            with request_deadline(15):
                provider = await self.catalog.get_provider(provider_id, expected_revision)
                result = await discover_models(
                    provider, self.providers[provider.protocol].transport
                )
                # 拉取期间配置发生变化时拒绝旧结果，前端重新刷新后才能导入。
                await self.catalog.get_provider(provider_id, expected_revision)
                return result

    async def readiness(self, model: str | None = None) -> CatalogReadiness:
        result = await self.catalog.readiness(model)
        if result.code or model is None:
            return result
        resolved = await self.catalog.resolve(model)
        available = self.credential_available(resolved.provider)
        return result.model_copy(
            update={
                "credential_available": available,
                "code": None if available else "AI_CREDENTIAL_MISSING",
            }
        )

    @asynccontextmanager
    async def _slot(self, *, conversation: bool = True):
        """在同一事件循环内占用名额，所有终态和取消都归还活动计数。"""
        if self._closed or self._active >= self._maximum:
            raise AiError("AI_BUSY", status_code=429)
        self._active += 1
        if conversation:
            self._active_requests += 1
        try:
            yield
        finally:
            self._active -= 1
            if conversation:
                self._active_requests -= 1

    async def _prepare(self, request: TextRequest):
        """从数据库固定本次调用的模型、地址和凭据，不跨请求缓存配置。"""
        resolved = await self.catalog.resolve(request.model)
        provider = self.providers.get(resolved.provider.protocol)
        if provider is None:
            raise AiError("AI_PROTOCOL_UNSUPPORTED", status_code=422)
        provider.validate_options(request.options)
        credential = self.credentials.resolve(resolved.provider)
        return provider, ProviderCall(request=request, resolved=resolved, credential=credential)

    async def _record(
        self,
        request: TextRequest,
        execution_id: str,
        started: float,
        *,
        status: str,
        content: str,
        model: str,
        stats: TokenUsage,
        error_code: str | None = None,
    ) -> bool:
        record = SessionWrite(
            execution_id=execution_id,
            session_id=request.session_id,
            request_id=request.request_id,
            configured_model=request.model,
            model=model,
            status=status,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            usage=stats,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            error_code=error_code,
            prompt=request.prompt,
            content=content,
            request_context={
                'model': request.model,
                'messages': [message.model_dump() for message in request.messages],
                'options': request.options.model_dump(exclude_none=True),
            },
        )
        try:
            timeout = remaining_timeout(request.timeout_seconds)
            await asyncio.wait_for(self.sessions.save(record), timeout)
            return True
        except Exception as error:
            logger.warning("AI record unavailable: %s", type(error).__name__)
            return False

    async def complete(self, request: TextRequest) -> AiCompletion:
        started, execution_id = time.monotonic(), uuid.uuid4().hex
        async with self._slot():
            with request_deadline(request.timeout_seconds):
                try:

                    async def invoke():
                        provider, call = await self._prepare(request)
                        result = await provider.complete(call)
                        require_text(result.content)
                        return result

                    timeout = remaining_timeout(request.timeout_seconds)
                    result = await asyncio.wait_for(invoke(), timeout)
                except asyncio.CancelledError:
                    await self._record(
                        request,
                        execution_id,
                        started,
                        status="cancelled",
                        content="",
                        model=request.model,
                        stats=TokenUsage(),
                    )
                    raise
                except Exception as original:
                    error = (
                        original
                        if isinstance(original, AiError)
                        else AiError(
                            "AI_TIMEOUT"
                            if isinstance(original, TimeoutError)
                            else "AI_RESPONSE_INVALID",
                            status_code=504 if isinstance(original, TimeoutError) else 502,
                        )
                    )
                    await self._record(
                        request,
                        execution_id,
                        started,
                        status="failed",
                        content="",
                        model=request.model,
                        stats=TokenUsage(),
                        error_code=error.code,
                    )
                    raise error from original
                saved = await self._record(
                    request,
                    execution_id,
                    started,
                    status="completed",
                    content=result.content,
                    model=result.model,
                    stats=result.usage,
                )
                return AiCompletion(
                    **result.model_dump(),
                    execution_id=execution_id,
                    request_id=request.request_id,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    record_saved=saved,
                )

    async def stream(self, request: TextRequest):
        started, execution_id = time.monotonic(), uuid.uuid4().hex
        parts, size, terminal = [], 0, False
        model, stats = request.model, TokenUsage()
        try:
            async with self._slot():
                with request_deadline(request.timeout_seconds):
                    try:
                        timeout = remaining_timeout(request.timeout_seconds)
                        provider, call = await asyncio.wait_for(self._prepare(request), timeout)
                        model = call.resolved.model.upstream_model
                        yield AiEvent(kind="start", request_id=request.request_id)
                        async with aclosing(provider.stream(call)) as iterator:
                            while True:
                                try:
                                    timeout = remaining_timeout(request.timeout_seconds)
                                    event = await asyncio.wait_for(anext(iterator), timeout)
                                except StopAsyncIteration as error:
                                    raise AiError("AI_RESPONSE_INVALID") from error
                                if event.kind == "content":
                                    size += len(event.content.encode())
                                    if size > 1048576:
                                        raise AiError("AI_RESPONSE_TOO_LARGE")
                                    parts.append(event.content)
                                    yield AiEvent(
                                        kind="delta",
                                        request_id=request.request_id,
                                        delta=event.content,
                                    )
                                else:
                                    content = require_text("".join(parts))
                                    model, stats = event.model or model, event.usage
                                    saved = await self._record(
                                        request,
                                        execution_id,
                                        started,
                                        status="completed",
                                        content=content,
                                        model=model,
                                        stats=stats,
                                    )
                                    result = AiCompletion(
                                        content=content,
                                        model=model,
                                        usage=stats,
                                        finish_reason=event.finish_reason,
                                        execution_id=execution_id,
                                        request_id=request.request_id,
                                        record_saved=saved,
                                        duration_ms=int((time.monotonic() - started) * 1000),
                                    )
                                    terminal = True
                                    yield AiEvent(
                                        kind="done",
                                        request_id=request.request_id,
                                        metadata=result.metadata(),
                                    )
                                    return
                    except (asyncio.CancelledError, GeneratorExit):
                        if not terminal:
                            await self._record(
                                request,
                                execution_id,
                                started,
                                status="cancelled",
                                content="".join(parts),
                                model=model,
                                stats=stats,
                            )
                        raise
                    except Exception as original:
                        error = (
                            original
                            if isinstance(original, AiError)
                            else AiError(
                                "AI_TIMEOUT"
                                if isinstance(original, TimeoutError)
                                else "AI_RESPONSE_INVALID"
                            )
                        )
                        await self._record(
                            request,
                            execution_id,
                            started,
                            status="failed",
                            content="".join(parts),
                            model=model,
                            stats=stats,
                            error_code=error.code,
                        )
                        terminal = True
                        yield AiEvent(
                            kind="error",
                            request_id=request.request_id,
                            code=error.code,
                            message="AI 请求未完成，请检查模型配置或服务状态",
                        )
        except AiError as error:
            if not terminal:
                yield AiEvent(
                    kind="error",
                    request_id=request.request_id,
                    code=error.code,
                    message="AI 服务当前无法接受请求",
                )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        for provider in self.providers.values():
            await provider.aclose()
