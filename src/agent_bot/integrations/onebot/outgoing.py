"""成功出站消息的统一登记、持久素材发布和会话代际隔离。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any

from nonebot.adapters.onebot.v11 import Message

from agent_bot.ai.chat_errors import ChatAiUnavailableError
from agent_bot.ai.errors import AiError
from agent_bot.materials.contracts import MAX_FILE_BYTES
from agent_bot.media.contracts import MediaAssetView, MediaOwner
from agent_bot.media.errors import MediaError
from agent_bot.memory_store import ChatMemoryStoreError
from agent_bot.services.tool_operations import DeliveryEffect


@dataclass(frozen=True)
class DeliveryScope:
    """真实事件或队列固化的会话与群目标；身份不能来自模型正文。"""

    session_id: str
    generation_id: str
    request_id: str | None
    key: str = ""
    bot_id: str | None = None
    group_id: str | None = None


_scope: ContextVar[DeliveryScope | None] = ContextVar("outgoing_scope", default=None)
_destination: ContextVar[tuple[str | None, str | None] | None] = ContextVar(
    "outgoing_destination", default=None,
)


class OutgoingRecorder:
    """先占位、再投递、最后确认上下文；未知副作用不自动重发。"""

    def __init__(
        self, *, service_provider: Callable[[], Any], store_provider: Callable[[], Any],
        materials_provider: Callable[[], Any],
        scope_provider: Callable[[str | None], Awaitable[DeliveryScope]],
        receipts_provider: Callable[[], Any] | None = None,
        group_message_recorder: Callable[[DeliveryScope, str, str], Awaitable[None]] | None = None,
    ) -> None:
        """按当前运行时解析依赖；可选群快照写入器只在真实投递确认后调用。"""
        self.service_provider = service_provider
        self.store_provider = store_provider
        self.materials_provider = materials_provider
        self.scope_provider = scope_provider
        self.receipts_provider = receipts_provider
        self.group_message_recorder = group_message_recorder
        self._confirmed: dict[str, DeliveryEffect] = {}

    @property
    def confirmed_receipts(self) -> dict[str, DeliveryEffect]:
        """向日志读取器交付尚未落库的确认；补记后由读取器移除，不触发发送。"""
        return self._confirmed

    @property
    def current_scope(self) -> DeliveryScope | None:
        """供创建后台子任务时固化当前代际，不从任务文本推断归属。"""
        return _scope.get()

    @contextmanager
    def destination(self, bot_id: str | None, group_id: str | None) -> Iterator[None]:
        """以网关实际发送目标覆盖事件来源，显式私聊不能投影回原事件群。"""
        token = _destination.set((bot_id, group_id))
        try:
            yield
        finally:
            _destination.reset(token)

    @asynccontextmanager
    async def scope(
        self, session_id: str, request_id: str, *, key: str,
        generation_id: str | None = None,
        bot_id: str | None = None,
        group_id: str | None = None,
    ) -> AsyncIterator[DeliveryScope]:
        """队列固化会话与群目标，结束时还原；群目标只能由宿主任务提供。"""
        generation = generation_id or await self.service_provider().history_generation(session_id)
        value = DeliveryScope(session_id, generation, request_id, key, bot_id, group_id)
        token = _scope.set(value)
        try:
            yield value
        finally:
            _scope.reset(token)

    async def send(
        self, message: Message, sender: Callable[[], Awaitable[Any]], *, key: str,
        request_id: str | None = None,
    ) -> Any:
        """归档媒体原件后调用平台，只有确认送达的分段可以进入模型上下文。"""
        scope = _scope.get() or await self.scope_provider(request_id)
        parts, ids = [], []
        for segment in message:
            if segment.type == "text":
                parts.append(str(segment.data["text"]))
            elif segment.type == "at":
                parts.append(f"[AT {segment.data['qq']}]")
            elif segment.type in {"image", "record", "video"}:
                data = self._inline_bytes(segment.data.get("file"))
                mime, suffix = self._media_type(segment.type, data)
                asset = await self._material(scope, data, mime, f"{segment.type}.{suffix}")
                parts.append(self._reference(asset))
                ids.append(asset.id)
            elif segment.type == "reply":
                parts.append(f"[引用消息 {segment.data['id']}]")
            else:
                raise MediaError("OUTGOING_SEGMENT_UNSUPPORTED", status_code=422)
        return await self._deliver(
            scope, key, "".join(parts), tuple(ids), sender,
        )

    async def send_video_asset(
        self, asset: MediaAssetView, sender: Callable[[], Awaitable[Any]], *, key: str,
        request_id: str | None = None,
    ) -> Any:
        """媒体已由调用方按会话授权并校验，记录附件引用而不复制到素材库。"""
        if asset.mime_type != "video/mp4" or asset.status != "ready" or asset.purpose != "output":
            raise MediaError("MATERIAL_INVALID_FILE", status_code=422)
        scope = _scope.get() or await self.scope_provider(request_id)
        text = f"[视频 附件ID={asset.id} 大小={asset.size_bytes}]"
        return await self._deliver(scope, key, text, (), sender)

    async def send_file(
        self, *, name: str, data: bytes, text: str, request_id: str,
        sender: Callable[[], Awaitable[Any]],
    ) -> Any:
        """生成文件以真实内容入库，长回复正文和附件引用共同进入上下文。"""
        scope = _scope.get() or await self.scope_provider(request_id)
        asset = await self._material(scope, data, "text/plain", name)
        return await self._deliver(
            scope, "file", f"{text}\n{self._reference(asset)}", (asset.id,), sender,
        )

    async def _material(
        self, scope: DeliveryScope, data: bytes, mime: str, name: str,
    ) -> Any:
        """后台绘图原件归原聊天会话，素材不受临时执行附件过期影响。"""
        return await self.materials_provider().put(
            owner=MediaOwner(entry="qq", session_id=scope.session_id),
            name=name, mime_type=mime, data=data,
        )

    async def _deliver(
        self, scope: DeliveryScope, key: str, text: str, ids: tuple[str, ...],
        sender: Callable[[], Awaitable[Any]],
    ) -> Any:
        """先确认出站回执再补群快照；任一上下文补写失败都不能再次发送。"""
        destination = _destination.get()
        if destination is not None:
            scope = replace(scope, bot_id=destination[0], group_id=destination[1])
        # 投递身份不随会话过期或重置变化；旧任务重试只能补记原占位，不能再次发送。
        identity = "\n".join((scope.session_id, scope.request_id or "", scope.key, key))
        delivery_id = hashlib.sha256(identity.encode()).hexdigest()
        store = self.store_provider()
        receipts = self.receipts_provider() if self.receipts_provider else None
        signature = hashlib.sha256(json.dumps(
            (identity, text, ids), ensure_ascii=False,
        ).encode()).hexdigest()
        cached = self._confirmed.get(delivery_id)
        effect = cached if cached else (
            await receipts.completed(delivery_id, "outgoing_message") if receipts else None
        )
        if effect is not None:
            result = await self._finish_confirmed(
                scope, delivery_id, signature, effect, None, receipts,
            )
        else:
            try:
                result = await self._claim_and_send(
                    store, receipts, scope, delivery_id, signature, text, ids, sender,
                )
            except AiError as error:
                if error.code == "AI_DATABASE_UNAVAILABLE":
                    raise ChatAiUnavailableError(
                        "出站消息记录暂不可用", error_code="OUTGOING_HISTORY_UNAVAILABLE",
                        status_code=503,
                    ) from error
                raise
        if self.group_message_recorder is not None and scope.bot_id and scope.group_id and text:
            try:
                # 主出站回执已持久化，补写失败重试只能复用回执，不能重发平台消息。
                await self.group_message_recorder(scope, delivery_id, text)
            except ChatMemoryStoreError as error:
                raise ChatAiUnavailableError(
                    "已发送消息的群聊上下文等待保存",
                    error_code="OUTGOING_GROUP_CONTEXT_UNAVAILABLE", status_code=503,
                ) from error
        return result

    async def _claim_and_send(
        self, store: Any, receipts: Any, scope: DeliveryScope, delivery_id: str,
        signature: str, text: str, ids: tuple[str, ...], sender: Callable[[], Awaitable[Any]],
    ) -> Any:
        """只为新的投递执行平台副作用，已有的成功回执不得再次发送。"""
        previous = await store.claim_delivery(
            session_id=scope.session_id, generation_id=scope.generation_id,
            delivery_id=delivery_id, request_id=scope.request_id, message_id=None,
            content=text, material_ids=ids,
        )
        if previous is not None:
            return {"message_id": previous.message_id} if previous.message_id else {}
        result = await sender()
        reference = None
        if isinstance(result, dict):
            value = result.get("message_id", result.get("file_id"))
            reference = str(value) if value is not None else None
        effect = DeliveryEffect(signature=signature, message_id=reference)
        # 内存回执在第一次持久化失败时保留，恢复后再删除；取消不能引起二次平台发送。
        self._confirmed[delivery_id] = effect
        return await self._finish_confirmed(scope, delivery_id, signature, effect, result, receipts)

    async def _finish_confirmed(
        self, scope: DeliveryScope, delivery_id: str, signature: str,
        effect: Any, result: Any, receipts: Any,
    ) -> Any:
        """已确认的回执先持久化，再完成上下文；正文变更不能冒用旧回执。"""
        if not isinstance(effect, DeliveryEffect) or effect.signature != signature:
            raise ValueError("outgoing delivery receipt conflicts")
        try:
            if receipts is not None:
                await receipts.complete(delivery_id, "outgoing_message", signature, effect)
            await self.store_provider().finish_delivery(
                delivery_id, effect.message_id, session_id=scope.session_id,
            )
        except AiError as error:
            if error.code == "AI_DATABASE_UNAVAILABLE":
                raise ChatAiUnavailableError(
                    "已发送消息的上下文等待保存", error_code="OUTGOING_HISTORY_UNAVAILABLE",
                    status_code=503,
                ) from error
            raise
        self._confirmed.pop(delivery_id, None)
        return result if result is not None else (
            {"message_id": effect.message_id} if effect.message_id is not None else {}
        )

    @staticmethod
    def _inline_bytes(value: object) -> bytes:
        """仅接收服务已生成的内联文件，禁止把任意本地路径当作素材读取。"""
        if not isinstance(value, str) or not value.startswith("base64://"):
            raise MediaError("OUTGOING_MEDIA_SOURCE_UNSUPPORTED", status_code=422)
        encoded = value.removeprefix("base64://")
        if len(encoded) > 4 * ((MAX_FILE_BYTES + 2) // 3):
            raise MediaError("MEDIA_TOO_LARGE", status_code=413)
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise MediaError("MATERIAL_INVALID_FILE", status_code=422) from error
        if not data or len(data) > MAX_FILE_BYTES:
            raise MediaError("MATERIAL_INVALID_FILE", status_code=422)
        return data

    @staticmethod
    def _reference(asset: Any) -> str:
        """上下文只保留可授权查询的素材 ID 和名称，不保存 Base64 或临时地址。"""
        kind = '图片' if asset.kind == 'image' else '文件'
        return f"[{kind} 素材ID={asset.id} 名称={asset.name}]"

    @staticmethod
    def _media_type(kind: str, data: bytes) -> tuple[str, str]:
        """图片依据实际签名声明 MIME，再由素材存储完整解码验证。"""
        if kind == "image":
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                return "image/png", "png"
            if data.startswith(b"\xff\xd8\xff"):
                return "image/jpeg", "jpg"
            if data.startswith((b"GIF87a", b"GIF89a")):
                return "image/gif", "gif"
            if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
                return "image/webp", "webp"
            raise MediaError("MATERIAL_INVALID_FILE", status_code=422)
        if kind == "record" and data.startswith(b"RIFF") and data[8:12] == b"WAVE":
            return "audio/wav", "wav"
        return "application/octet-stream", "bin"
