"""持久化会话队列的模型来源、投递与失败解释。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from nonebot.adapters.onebot.v11 import Bot, MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed, ApiNotAvailable, NetworkError
from websockets.exceptions import ConnectionClosedError

from agent_bot.ai.chat_errors import (
    ChatAiError,
    ChatAiProtocolError,
    ChatAiRejectedError,
    ChatAiUnavailableError,
)
from agent_bot.config import RuntimeConfig
from agent_bot.database_pool import finish_database_write
from agent_bot.integrations.onebot.memory_source import has_user_memory_source, queued_memory_source
from agent_bot.integrations.onebot.outgoing import OutgoingRecorder
from agent_bot.integrations.onebot.replies import OneBotReplyGateway
from agent_bot.integrations.workflow_store import WorkflowNotFoundError
from agent_bot.media.contracts import MediaOwner, MediaScope
from agent_bot.model_guard import (
    ModelGuardUnavailableError,
    model_configuration_error_code,
)
from agent_bot.models import ChatMediaItem, ChatMediaResponse, ChatToolName
from agent_bot.services.chat import ChatService
from agent_bot.services.chat.errors import ChatRateLimitedError, is_chat_busy_error
from agent_bot.services.conversation_queue import (
    ConversationJob,
    ConversationJobExpiredError,
    ConversationQueueFullError,
    ConversationResult,
    PersistedGeneratedReply,
    WorkflowImageArtifact,
)
from agent_bot.services.image_task_context import active_image_task
from agent_bot.services.multi_reply import (
    build_multi_reply_prompt,
    join_reply_array,
    parse_multi_reply,
)
from agent_bot.services.replies import (
    FeatureUnavailableFacts,
    GeneratedReply,
    LlmReplyComposer,
    ReplyIntent,
    TaskFailedFacts,
    WorkflowStatusFacts,
)
from agent_bot.services.reply_text import clean_internal_reply_text, contradicts_image_completion
from agent_bot.services.tool_images import capture_tool_images, current_tool_images
from agent_bot.services.tool_operations import (
    ImageEffect,
    ReplyEffect,
    ToolOperationStore,
    VideoEffect,
)
from agent_bot.services.workflow_execution import WorkflowExecutionService, WorkflowImageRequest
from agent_bot.state import AccessPolicy, split_reply

RuntimeLease = Callable[[], AbstractAsyncContextManager[None]]
LegacyToolSessionValue = tuple[Sequence[ChatToolName], str | None]
ToolSession = AbstractAsyncContextManager[Any]

logger = logging.getLogger(__name__)


_SILENT_FAILURES = (
    ChatRateLimitedError, ConversationQueueFullError, ConversationJobExpiredError,
)


def generated_reply_from_model(
    text: str, request_id: str, *, as_file: bool = False, preserve_format: bool = False,
) -> GeneratedReply:
    """集中验证模型正文来源，文件及明确分条保留格式，拒绝空白结果。"""

    normalized = text if as_file or preserve_format else text.strip()
    if not normalized.strip():
        raise ChatAiProtocolError("model reply is empty")
    return GeneratedReply.model_validate(
        {"text": normalized, "requestId": request_id},
        context={"file_reply": as_file, "preserve_format": preserve_format},
    )


class ConversationQueueProcessor:
    """执行队列任务，并把结果安全地投递到对应 OneBot 会话。"""

    def __init__(
        self,
        *,
        config_provider: Callable[[], RuntimeConfig],
        chat_service_provider: Callable[[], ChatService],
        runtime_lease: RuntimeLease,
        tool_session_provider: Callable[[ConversationJob], ToolSession],
        bot_provider: Callable[[str], Bot],
        reply_composer: LlmReplyComposer,
        reply_gateway: OneBotReplyGateway,
        operation_store_provider: Callable[[], ToolOperationStore],
        image_submitter: Callable[[ConversationJob], Awaitable[str]],
        workflow_service_provider: Callable[[], WorkflowExecutionService | None],
        media_store_provider: Callable[[], Any] = lambda: None,
        material_store_provider: Callable[[], Any] = lambda: None,
        outgoing_recorder: OutgoingRecorder | None = None,
        receive_queued: Callable[..., Awaitable[None]] | None = None,
        finish_received: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        """保存热更新依赖和统一发送记录器，队列与命令使用同一上下文边界。"""

        self._config_provider = config_provider
        self._chat_service_provider = chat_service_provider
        self._runtime_lease = runtime_lease
        self._tool_session_provider = tool_session_provider
        self._bot_provider = bot_provider
        self._reply_composer = reply_composer
        self._reply_gateway = reply_gateway
        self._operation_store_provider = operation_store_provider
        self._image_submitter = image_submitter
        self._workflow_service_provider = workflow_service_provider
        self._media_store_provider = media_store_provider
        self._material_store_provider = material_store_provider
        self._outgoing_recorder = outgoing_recorder
        self._receive_queued = receive_queued
        self._finish_received_callback = finish_received

    @asynccontextmanager
    async def _delivery_scope(self, job: ConversationJob, key: str):
        """每个持久投递片段使用稳定键，后台任务保留提交时的会话代际。"""
        if self._outgoing_recorder is None:
            yield
        else:
            async with self._outgoing_recorder.scope(
                job.session_id, job.request_id, key=key,
                generation_id=job.history_generation_id,
                bot_id=job.bot_id, group_id=job.group_id if job.scene == "group" else None,
            ):
                yield

    @property
    def retryable_errors(self) -> tuple[type[Exception], ...]:
        """返回可按策略重试的异常，包含 OneBot 发送阶段直接抛出的 WebSocket 断连。"""

        return (
            ChatAiUnavailableError,
            ModelGuardUnavailableError,
            ConnectionError,
            ConnectionClosedError,
            TimeoutError,
            ActionFailed,
            ApiNotAvailable,
            NetworkError,
        )

    async def generate(self, job: ConversationJob) -> ConversationResult:
        """捕获素材读取和模型附件；开画通知也登记到当前请求的出站历史。"""
        async with self._delivery_scope(job, "generate"):
            with capture_tool_images() as images:
                result = await self._generate(job)
        if images.items:
            result = ConversationResult.model_validate(result.model_copy(update={
                "tool_images": images.items,
            }).model_dump(by_alias=True))
        return result

    async def _generate(self, job: ConversationJob) -> ConversationResult:
        """绑定直接生图和工作流提交器，生成结果与投递检查点共用原队列。"""

        async with self._runtime_lease():
            # 模型提示和最终解析必须来自同一快照，热更新不能在生成中途切换协议。
            reply_config = self._config_provider().model_copy()
            multi_reply = reply_config.chat_multi_reply_enabled and not job.laya_decision
            prompt_append = job.system_prompt_append
            if multi_reply:
                prompt_append = "\n\n".join(filter(None, (
                    prompt_append, build_multi_reply_prompt(reply_config),
                )))
            await self._receive_original(job)
            if (execution := active_image_task.get()) is not None:
                await execution.check_cancelled()
            if job.ai_job_reply is not None:
                # 已生成的任务也先走回复格式校验，确保持久化正文与发送分段相同。
                text = job.ai_job_reply.text
                if not reply_config.chat_multi_reply_enabled:
                    text = join_reply_array(text)
                generated = generated_reply_from_model(text, job.request_id)
                return ConversationResult(
                    kind="TEXT",
                    reply=PersistedGeneratedReply.model_validate(
                        generated.model_dump(by_alias=True),
                    ),
                    deliveryParts=tuple(split_reply(
                        generated.text,
                        chunk_chars=self._config_provider().chat_chunk_chars,
                        max_reply_chars=len(generated.text),
                    )),
                )
            if job.background_image and job.workflow_request is not None:
                return await self._generate_workflow(job)
            if job.media_output == "IMAGE" and not job.background_image:
                await self._submit_image(job)
                return await self._queued_image_result(job)
            if job.media_output is not None:
                response = await self._generate_media(job)
                return ConversationResult(kind="MEDIA", media=response)

            async with self._tool_session_provider(job) as session:
                if isinstance(session, tuple):
                    tools, token = session
                    collector = None
                    speech_collector = None
                    reply_collector = None
                else:
                    tools = session.tools
                    token = session.token
                    collector = session.collector
                    speech_collector = getattr(session, "speech_collector", None)
                    reply_collector = getattr(session, "reply_collector", None)
                if collector is not None:
                    collector.submit = lambda request, text: self._submit_image(
                        job, request, start_reply=text,
                    )
                    collector.submit_image = lambda prompt, text: self._submit_image(
                        job.model_copy(update={"prompt": prompt}), start_reply=text,
                    )
                try:
                    if collector is not None and collector.queued_task_id is not None:
                        answer = None
                    elif reply_collector is not None and reply_collector.already_delivered:
                        answer = ""
                    else:
                        answer = await self._chat_service_provider().generate_queued(
                            job.session_id,
                            job.prompt,
                            request_id=job.request_id,
                            user=job.user,
                            user_name=job.user_name,
                            chat_context=job.chat_context,
                            image_urls=job.image_urls,
                            **({"image_material_ids": job.image_material_ids}
                               if job.image_material_ids else {}),
                            video_urls=job.video_urls,
                            chat_tools=tools,
                            tool_session_token=token,
                            recent_group_context=job.recent_group_context,
                            **({"laya_decision": True} if job.laya_decision else {}),
                            **(
                                {"system_prompt_append": prompt_append}
                                if prompt_append is not None
                                else {}
                            ),
                        )
                except Exception as exc:
                    if collector is not None and collector.queued_task_id is not None:
                        return await self._queued_image_result(job)
                    collected_speech = (
                        speech_collector.speech
                        if speech_collector is not None
                        else None
                    )
                    if "tts_generate" in job.chat_tools and collected_speech is not None:
                        logger.warning(
                            "语音工具收尾失败但已有可投递结果：stage=agent_generate "
                            "job_id=%s request_id=%s session_id=%s error=%s",
                            job.job_id,
                            job.request_id,
                            job.session_id,
                            type(exc).__name__,
                        )
                        answer = None
                    elif {"local_workflow_generate_image", "local_workflow_generate_video"} & set(
                        job.chat_tools
                    ):
                        # 只记录本地稳定错误码，不输出可能含凭据的异常正文。
                        logger.warning(
                            "Agent 对话处理失败：stage=agent_generate job_id=%s "
                            "request_id=%s session_id=%s collected_images=%s error=%s code=%s",
                            job.job_id,
                            job.request_id,
                            job.session_id,
                            len(collector.images) if collector is not None else 0,
                            type(exc).__name__,
                            exc.error_code if isinstance(exc, ChatAiError) else None,
                        )
                        if collector is not None and collector.images:
                            answer = None
                        else:
                            raise
                    else:
                        raise
            if collector is not None and collector.queued_task_id is not None:
                return await self._queued_image_result(job)
            images = tuple(
                {
                    "mimeType": image.media_type,
                    "data": base64.b64encode(image.data).decode("ascii"),
                }
                for image in collector.images[:1]
            ) if collector is not None else ()
            if images:
                return ConversationResult(
                    kind="WORKFLOW_IMAGES", workflowImages=images, workflowDeliveryVersion=2,
                )
            collected_speech = (
                speech_collector.speech if speech_collector is not None else None
            )
            if collected_speech is not None:
                speech = collected_speech.speech
                audio = collected_speech.audio
                if audio is None:
                    if speech is None:
                        raise ChatAiProtocolError("tts audio artifact is missing")
                    audio = await asyncio.to_thread(speech.path.read_bytes)
                if (
                    (speech is not None and (
                        speech.media_type != "audio/wav" or len(audio) != speech.bytes
                    ))
                    or len(audio) > 10_485_760
                    or len(audio) < 12
                    or not audio.startswith(b"RIFF")
                    or audio[8:12] != b"WAVE"
                ):
                    raise ChatAiProtocolError("tts audio artifact is invalid")
                return ConversationResult(
                    kind="MEDIA",
                    media=ChatMediaResponse(
                        text=collected_speech.text,
                        items=(
                            ChatMediaItem(
                                type="AUDIO",
                                mimeType="audio/wav",
                                data=base64.b64encode(audio).decode("ascii"),
                            ),
                        ),
                    ),
                )

            config = self._config_provider()
            max_reply_chars = self._reply_limit(job, config.chat_max_reply_chars)
            if job.laya_decision:
                if answer is None:
                    raise ChatAiProtocolError("Laya decision report is empty")
                generated = generated_reply_from_model(answer, job.request_id)
                return ConversationResult(
                    kind="TEXT",
                    reply=PersistedGeneratedReply.model_validate(generated.model_dump(by_alias=True)),
                    deliveryParts=tuple(split_reply(
                        generated.text, chunk_chars=config.chat_chunk_chars,
                        max_reply_chars=len(generated.text),
                    )),
                )
            if not answer and current_tool_images():
                return ConversationResult(kind="TOOL_IMAGES", toolImages=current_tool_images())
            if multi_reply:
                if reply_collector is not None and reply_collector.already_delivered:
                    return ConversationResult(kind="SILENT")
                if answer is None:
                    raise ChatAiProtocolError("多条回复正文不能为空")
                return self._multi_reply_result(job, answer, reply_config, tools, reply_collector)
            if answer is not None:
                # 单条 JSON 字符串与数组均先解码，再按实际正文决定长度、文件和工具发送。
                answer = clean_internal_reply_text(join_reply_array(answer))
            # 普通聊天超过文字上限改为完整文件；自动发言继续遵循专用长度上限。
            if (
                answer is not None and job.auto_speak_max_reply_chars is None
                and len(answer.strip()) > max_reply_chars
            ):
                generated = generated_reply_from_model(answer, job.request_id, as_file=True)
                selected = reply_collector.reply if reply_collector is not None else None
                if reply_collector is not None and reply_collector.already_delivered:
                    return ConversationResult(kind="SILENT")
                return ConversationResult(
                    kind="TOOL_REPLY" if tools else "TEXT", replyAsFile=True,
                    reply=PersistedGeneratedReply.model_validate(generated.model_dump(by_alias=True)),
                    deliveryParts=(generated.text,),
                    mentionTargetId=(
                        str(selected.target_user_id)
                        if selected is not None and selected.target_user_id is not None
                        else job.mention_target_id
                    ) if tools else None,
                )
            if tools:
                selected = reply_collector.reply if reply_collector is not None else None
                if reply_collector is not None and reply_collector.already_delivered:
                    return ConversationResult(kind="SILENT")
                if answer is not None:
                    generated = generated_reply_from_model(
                        answer.strip()[:max_reply_chars], job.request_id,
                    )
                    return ConversationResult(
                        kind="TOOL_REPLY",
                        reply=PersistedGeneratedReply.model_validate(
                            generated.model_dump(by_alias=True),
                        ),
                        deliveryParts=(generated.text,),
                        mentionTargetId=(
                            str(selected.target_user_id)
                            if selected is not None and selected.target_user_id is not None
                            else job.mention_target_id
                        ),
                    )
            if answer is None:
                raise ChatAiProtocolError("model reply is empty")
            generated = generated_reply_from_model(
                answer.strip()[:max_reply_chars], job.request_id,
            )
            persisted = PersistedGeneratedReply.model_validate(generated.model_dump(by_alias=True))
            parts = tuple(
                split_reply(
                    generated.text,
                    chunk_chars=config.chat_chunk_chars,
                    max_reply_chars=max_reply_chars,
                )
            )
            return ConversationResult(kind="TEXT", reply=persisted, deliveryParts=parts)

    def _multi_reply_result(
        self, job: ConversationJob, answer: str, config: RuntimeConfig,
        tools: Sequence[ChatToolName], reply_collector: Any,
    ) -> ConversationResult:
        """先解析模型边界再应用正文预算，文件与历史均不保留 JSON 包装。"""
        parts = tuple(part.strip() for part in parse_multi_reply(
            answer, config.chat_multi_reply_max_messages,
        ))
        limit = self._reply_limit(job, config.chat_max_reply_chars)
        text = "\n".join(parts)
        as_file = job.auto_speak_max_reply_chars is None and len(text) > limit
        if job.auto_speak_max_reply_chars is not None:
            # 专用主动发言预算按可见正文计数，不截断协议或发送空白尾消息。
            bounded = []
            remaining = limit
            for part in parts:
                clipped = part[:remaining].rstrip()
                if not clipped:
                    break
                bounded.append(clipped)
                remaining -= len(clipped) + 1
                if remaining <= 0:
                    break
            parts = tuple(bounded)
            text = "\n".join(parts)
        reply = generated_reply_from_model(text, job.request_id, preserve_format=True)
        selected = reply_collector.reply if reply_collector is not None else None
        return ConversationResult(
            kind="TOOL_REPLY" if tools else "TEXT",
            reply=PersistedGeneratedReply.model_validate(reply.model_dump(by_alias=True)),
            deliveryParts=(text,) if as_file else parts,
            multiReply=True, replyAsFile=as_file,
            deliveryIntervalSeconds=0 if as_file else config.chat_multi_reply_interval_seconds,
            mentionTargetId=(
                str(selected.target_user_id)
                if selected is not None and selected.target_user_id is not None
                else job.mention_target_id
            ) if tools else None,
        )

    async def _submit_image(
        self, job: ConversationJob, workflow_request: WorkflowImageRequest | None = None,
        *, start_reply: str | None = None,
    ) -> str:
        """答复送达后独立提交绘图，子任务只保留准备好的原图，不继承接收素材引用。"""

        if (
            workflow_request is None
            and not self._config_provider().chat_image_generation_enabled
        ):
            raise RuntimeError("image generation is unavailable")
        if workflow_request is not None and start_reply is None:
            raise ValueError("workflow submission requires the agent start reply")
        await self._send_image_start(job, text=start_reply)
        task_id = "image-" + hashlib.sha256(job.job_id.encode("utf-8")).hexdigest()[:32]
        scope = self._outgoing_recorder.current_scope if self._outgoing_recorder else None
        task = ConversationJob.model_validate(job.model_copy(update={
            "job_id": task_id, "request_id": f"{job.request_id[:120]}-image",
            "background_image": True, "workflow_request": workflow_request,
            "media_output": "IMAGE" if workflow_request is None else None,
            "chat_tools": (), "image_urls": (), "video_urls": (),
            "material_ids": (), "image_material_ids": (),
            "image_start_reply": None, "image_start_parts": None,
            "history_generation_id": scope.generation_id if scope else job.history_generation_id,
        }).model_dump(by_alias=True))
        await self._image_submitter(task)
        return task_id

    async def _queued_image_result(self, job: ConversationJob) -> ConversationResult:
        """结束父对话并保存已发送答复，后台完成时不再回写该聊天轮次。"""

        if job.image_start_reply is not None:
            reply = job.image_start_reply
        else:
            saved = await self._operation_store_provider().completed(
                job.job_id, "image_start_reply",
            )
            if not isinstance(saved, ReplyEffect):
                raise ChatAiProtocolError("image start reply is missing")
            reply = PersistedGeneratedReply(text=saved.text, requestId=job.request_id)
        return ConversationResult(kind="IMAGE_QUEUED", reply=reply, deliveryParts=(reply.text,))

    async def _generate_workflow(self, job: ConversationJob) -> ConversationResult:
        """在独立工作队列生成图片，结果不明时禁止重新提交相同工作流。"""

        request = job.workflow_request
        if request is not None and request.output_kind == "video":
            return await self._generate_video_workflow(job)
        if request is None:
            raise ValueError("workflow request is missing")
        service = self._workflow_service_provider()
        if service is None or not self._config_provider().chat_local_workflow_enabled:
            raise RuntimeError("background workflow is unavailable")
        store = self._operation_store_provider()
        signature = hashlib.sha256(
            request.model_dump_json(exclude_none=True).encode("utf-8"),
        ).hexdigest()
        saved = await store.begin(job.job_id, "local_workflow_generate_image", signature)
        if saved is None:
            if request.workflow_sha256 is None:
                image = await service.generate(
                    workflow_key=request.workflow_key, positive_prompt=request.positive_prompt,
                    negative_prompt=request.negative_prompt,
                )
            else:
                image = await service.generate(
                    workflow_key=request.workflow_key, positive_prompt=request.positive_prompt,
                    negative_prompt=request.negative_prompt, input_image=request.input_image,
                    workflow_sha256=request.workflow_sha256,
                )
            artifact = WorkflowImageArtifact(
                mimeType=image.media_type, data=base64.b64encode(image.data).decode("ascii"),
            )
            saved = ImageEffect(
                workflow_key=request.workflow_key, media_type=artifact.mime_type,
                data=artifact.data,
            )
            await store.complete(job.job_id, "local_workflow_generate_image", signature, saved)
        if not isinstance(saved, ImageEffect):
            raise ChatAiProtocolError("background workflow result is invalid")
        materials = self._material_store_provider()
        if materials is not None:
            # 成功回执已保存，素材保存失败后重试只复用原图，不能再次调用 Comfy。
            await materials.put(
                owner=MediaOwner(entry="qq", session_id=job.session_id),
                name="工作流生成图片", mime_type=saved.media_type,
                data=base64.b64decode(saved.data, validate=True), category="generated",
            )
        return ConversationResult(kind="WORKFLOW_IMAGES", workflowImages=(WorkflowImageArtifact(
            mimeType=saved.media_type, data=saved.data,
        ),))

    async def _generate_video_workflow(self, job: ConversationJob) -> ConversationResult:
        """先落盘后保存成功回执；重试只能读取同一个附件，不能再次提交生成。"""
        request = job.workflow_request
        service, media = self._workflow_service_provider(), self._media_store_provider()
        if (
            request is None
            or service is None
            or media is None
            or not self._config_provider().chat_local_workflow_enabled
        ):
            raise RuntimeError("video workflow storage or service is unavailable")
        operations = self._operation_store_provider()
        signature = hashlib.sha256(request.model_dump_json(exclude_none=True).encode()).hexdigest()
        saved = await operations.begin(job.job_id, "local_workflow_generate_video", signature)
        if saved is None:
            video = await service.generate_video(
                workflow_key=request.workflow_key, positive_prompt=request.positive_prompt,
                negative_prompt=request.negative_prompt, input_image=request.input_image,
                workflow_sha256=request.workflow_sha256,
            )
            if (execution := active_image_task.get()) is not None:
                await execution.check_cancelled()
            owner = MediaOwner(entry="qq", session_id=job.session_id)
            scope = MediaScope(owner=owner, execution_kind="media",
                               execution_id=hashlib.sha256(job.job_id.encode()).hexdigest()[:32])
            asset = await media.put(owner=owner, scope=scope, purpose="output",
                                    mime_type=video.media_type, data=video.data)
            saved = VideoEffect(workflow_key=request.workflow_key, asset_id=asset.id)
            await operations.complete(job.job_id, "local_workflow_generate_video", signature, saved)
        if not isinstance(saved, VideoEffect):
            raise ChatAiProtocolError("background video result is invalid")
        return ConversationResult(kind="WORKFLOW_VIDEO", workflowVideoId=saved.asset_id)

    async def _send_image_start(self, job: ConversationJob, *, text: str | None = None) -> None:
        """先占位再投递开画答复；工具直接采用父模型正文，重试复用发送记录。"""

        if job.image_start_reply is not None:
            return  # 旧队列任务的开画答复已经由队列投递阶段处理。
        store = self._operation_store_provider()
        signature = hashlib.sha256(job.request_id.encode("utf-8")).hexdigest()
        saved = await store.begin(job.job_id, "image_start_reply", signature)
        if saved is not None:
            return
        answer = text
        if answer is None:
            answer = await self._chat_service_provider().generate_image_start_queued(
                job.session_id, job.prompt, request_id=job.request_id,
                user=job.user, user_name=job.user_name, chat_context=job.chat_context,
            )
        config = self._config_provider()
        answer = clean_internal_reply_text(join_reply_array(answer))
        reply = generated_reply_from_model(
            answer[:min(config.chat_max_reply_chars, 4000)], job.request_id,
        )
        await self._reply_gateway.send_bot_exact_part(
            self._bot_provider(job.bot_id), scene=job.scene,
            target_id=job.group_id or job.user_id, reply=reply,
        )
        await store.complete(
            job.job_id, "image_start_reply", signature, ReplyEffect(text=reply.text),
        )

    async def _deliver_image_completion(self, job: ConversationJob) -> None:
        """图片送达后通知原创建人；重用前复核缓存文案，错误通知不能变成任务失败。"""

        store = self._operation_store_provider()
        signature = hashlib.sha256(job.request_id.encode("utf-8")).hexdigest()
        saved = await store.begin(job.job_id, "image_completion_reply", signature)
        request_id = f"{job.request_id[:118]}-completed"
        if saved is None:
            async with self._runtime_lease():
                reply = await self._reply_composer.compose(ReplyIntent(
                    requestId=request_id, sessionId=f"image:{job.job_id}",
                    code="WORKFLOW_STATUS", facts=WorkflowStatusFacts(status="completed"),
                    originalPrompt=job.prompt, user=job.user, userName=job.user_name,
                    chatContext=job.chat_context,
                ))
                if reply is None:
                    return
                limit = min(self._config_provider().chat_max_reply_chars, 4000)
                saved = ReplyEffect(text=reply.text[:limit])
            await store.complete(job.job_id, "image_completion_reply", signature, saved)
        if not isinstance(saved, ReplyEffect):
            raise ChatAiProtocolError("image completion reply is invalid")
        # 升级前可能已经缓存了错误文案；跳过通知，不重画、不重发图片或改写完成状态。
        if contradicts_image_completion(saved.text):
            logger.warning(
                "绘图完成通知跳过：request_id=%s category=contradictory_text", request_id,
            )
            return
        if (execution := active_image_task.get()) is not None:
            await execution.check_cancelled()
        await self._reply_gateway.send_bot_exact_part(
            self._bot_provider(job.bot_id), scene=job.scene,
            target_id=job.group_id or job.user_id,
            reply=generated_reply_from_model(saved.text, request_id),
            prefix=(
                MessageSegment.at(job.user_id)
                if job.scene == "group" else None
            ),
        )

    async def start_delivery_count(self, job: ConversationJob) -> int:
        """返回固化的开画片段数；旧 v2 已证明来源的完整文字视为单段。"""

        if job.image_start_reply is None:
            return 0
        return len(job.image_start_parts) if job.image_start_parts is not None else 1

    async def deliver_start_part(self, job: ConversationJob, index: int) -> None:
        """按片段键绑定出站上下文，恢复开画通知不会重复记录或发送。"""
        async with self._delivery_scope(job, f"start:{index}"):
            await self._deliver_start_part(job, index)

    async def _deliver_start_part(self, job: ConversationJob, index: int) -> None:
        """投递指定的持久化开画片段，重试和热刷新均不得重新分段。"""

        job = ConversationJob.model_validate(job.model_dump(by_alias=True))
        if job.image_start_reply is None:
            raise ValueError("workflow image start reply is missing")
        parts = job.image_start_parts
        if parts is None:
            parts = (job.image_start_reply.text,)
        if not 0 <= index < len(parts):
            raise ValueError("workflow image start part index is invalid")
        reply = GeneratedReply(
            text=parts[index], requestId=job.image_start_reply.request_id,
            source=job.image_start_reply.source,
        )
        if (execution := active_image_task.get()) is not None:
            await execution.check_cancelled()
        await self._reply_gateway.send_bot_exact_part(
            self._bot_provider(job.bot_id), scene=job.scene,
            target_id=job.group_id or job.user_id, reply=reply,
        )

    async def _generate_media(self, job: ConversationJob) -> ChatMediaResponse:
        """按任务中固化的输出类型生成图片或语音。"""

        config = self._config_provider()
        if job.media_output == "IMAGE":
            if not config.chat_image_generation_enabled:
                raise RuntimeError("image generation was disabled after enqueue")
            if not job.background_image:
                await self._send_image_start(job)
            timeout_seconds = config.chat_image_timeout_seconds
            max_bytes = config.chat_image_max_bytes
            image_format = config.chat_image_response_format
            voice = None
            audio_format = None
        else:
            if not config.chat_tts_enabled:
                raise RuntimeError("audio generation was disabled after enqueue")
            timeout_seconds = config.chat_tts_timeout_seconds
            max_bytes = config.chat_tts_max_audio_bytes
            image_format = None
            voice = config.chat_tts_voice
            audio_format = config.chat_tts_response_format
        return await self._chat_service_provider().generate_media_queued(
            f"image:{job.job_id}" if job.background_image else job.session_id,
            job.prompt,
            request_id=job.request_id,
            output_type=job.media_output,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
            image_format=image_format,
            voice=voice,
            audio_format=audio_format,
            user=job.user or "qq-user",
            user_name=job.user_name,
            chat_context=job.chat_context,
            recent_group_context=job.recent_group_context,
            media_session_id=job.session_id,
        )

    async def delivery_count(self, job: ConversationJob, result: ConversationResult) -> int:
        """缓存恢复也先登记原用户和事件时间，再计算图片、正文的持久投递进度。"""
        if self._receive_queued is not None:
            async with self._runtime_lease():
                await self._receive_original(job)
        return len(result.tool_images) + await self._delivery_count(job, result)

    async def _receive_original(self, job: ConversationJob) -> None:
        """生成与缓存投递共享接收登记，仓库按请求去重并保留首次原时间。"""
        if (
            self._receive_queued is not None and not job.background_image
            and job.ai_job_id is None and job.auto_speak_max_reply_chars is None
        ):
            # 旧任务没有独立原话，必须显式未知，不能让ChatContext重新把拼装prompt当本人证据。
            source_options = {"source": {"source_kind": "unknown", "scene": job.scene}}
            if has_user_memory_source(job):
                secret = self._config_provider().chat_session_secret.get_secret_value()
                source_options["source"] = queued_memory_source(job, secret)
            await self._receive_queued(
                job.session_id, job.prompt, request_id=job.request_id,
                created_at=datetime.fromtimestamp(job.created_at, UTC),
                chat_context=job.chat_context, generation_id=job.history_generation_id,
                **source_options,
            )

    def _requires_image_completion(
        self, job: ConversationJob, result: ConversationResult,
    ) -> bool:
        """群聊生成图片与后台绘图追加完成通知，语音不进入绘图通知流程。"""

        if not job.background_image and job.scene != "group":
            return False
        return result.kind in {"WORKFLOW_IMAGES", "WORKFLOW_VIDEO"} or (
            result.kind == "MEDIA" and result.media is not None
            and result.media.items[0].type == "IMAGE"
        )

    async def _delivery_count(
        self,
        job: ConversationJob,
        result: ConversationResult,
    ) -> int:
        """独立计算图片、文字和提醒的投递进度，恢复时不重发已确认片段。"""

        if self._requires_image_completion(job, result):
            return 2
        if result.reply_as_file:
            return 1 + int(self._file_mention_target(job, result) is not None)
        if result.kind in {"SILENT", "IMAGE_QUEUED", "TOOL_IMAGES"}:
            return 0
        if result.kind == "TEXT":
            return len(result.delivery_parts)
        if result.kind == "TOOL_REPLY" and result.multi_reply:
            return len(result.delivery_parts) + int(bool(result.workflow_images))
        if result.kind == "WORKFLOW_IMAGES":
            return 1
        if result.kind == "MIXED":
            return 1 + len(result.delivery_parts)
        return 1

    def _file_mention_target(
        self, job: ConversationJob, result: ConversationResult,
    ) -> str | None:
        """文件上传和群提醒分别保存进度，提醒只能使用原任务或工具登记的目标。"""
        if job.scene != "group":
            return None
        return result.mention_target_id if result.kind == "TOOL_REPLY" else job.mention_target_id

    async def _send_multi_reply_part(
        self, job: ConversationJob, result: ConversationResult, index: int,
        prefix: MessageSegment | None,
    ) -> None:
        """按持久化模型边界发送一条正文，保留代码格式并沿用统一出站登记。"""
        assert result.reply is not None
        reply = generated_reply_from_model(
            result.delivery_parts[index], result.reply.request_id, preserve_format=True,
        )
        await self._reply_gateway.send_bot_exact_part(
            self._bot_provider(job.bot_id), scene=job.scene,
            target_id=job.group_id or job.user_id, reply=reply, prefix=prefix,
        )

    async def deliver_part(
        self, job: ConversationJob, result: ConversationResult, index: int,
    ) -> None:
        """单独发送图片及文字，每个成功片段独立进入原会话上下文。"""
        async with self._delivery_scope(job, f"result:{index}"):
            await self._deliver_part(job, result, index)

    async def _deliver_part(
        self, job: ConversationJob, result: ConversationResult, index: int,
    ) -> None:
        """按持久化索引投递附件或正文，消息边界与提醒目标来自已校验结果。"""
        if job.ai_job_id is not None:
            config = self._config_provider()
            policy = AccessPolicy(
                allowed_users=config.chat_allowed_users,
                allowed_groups=config.chat_allowed_groups,
                blocked_users=config.chat_blocked_users,
                blocked_groups=config.chat_blocked_groups,
                allow_all=config.chat_allow_all,
            )
            if not config.chat_job_enabled or not policy.allows(
                user_id=job.user_id, group_id=job.group_id,
            ):
                raise ChatAiRejectedError("AI job delivery is disabled for this target")
        if (execution := active_image_task.get()) is not None:
            await execution.check_cancelled()
        if index < 0:
            raise ValueError("delivery index is invalid")
        if index < len(result.tool_images):
            validated = ConversationResult.model_validate(result.model_dump(by_alias=True))
            image = validated.tool_images[index]
            await self._reply_gateway.send_media_bot(
                self._bot_provider(job.bot_id), scene=job.scene,
                target_id=job.group_id or job.user_id,
                segment=MessageSegment.image(f"base64://{image.data}"), prefix=None,
            )
            return
        index -= len(result.tool_images)
        if result.kind in {"SILENT", "IMAGE_QUEUED"}:
            raise ValueError("silent result has no delivery")
        image_completion = self._requires_image_completion(job, result)
        if image_completion:
            if index == 1:
                await self._deliver_image_completion(job)
                return
            if index != 0:
                raise ValueError("image with completion has exactly two deliveries")
        if result.reply_as_file:
            validated = ConversationResult.model_validate(result.model_dump(by_alias=True))
            assert validated.reply is not None
            target = self._file_mention_target(job, validated)
            if index == 1 and target is not None:
                await self._reply_gateway.send_file_mention(
                    self._bot_provider(job.bot_id), target_id=job.group_id, member_id=target,
                )
                return
            if index != 0:
                raise ValueError("file reply delivery index is invalid")
            await self._reply_gateway.send_reply_file(
                self._bot_provider(job.bot_id), scene=job.scene,
                target_id=job.group_id or job.user_id,
                reply=GeneratedReply.model_validate(
                    validated.reply.model_dump(by_alias=True), context={"file_reply": True},
                ),
            )
            return
        if result.multi_reply:
            validated = ConversationResult.model_validate(result.model_dump(by_alias=True))
            if validated.workflow_images and index == 0:
                await self._reply_gateway.send_media_bot(
                    self._bot_provider(job.bot_id), scene=job.scene,
                    target_id=job.group_id or job.user_id,
                    segment=MessageSegment.image(f"base64://{validated.workflow_images[0].data}"),
                )
                return
            part_index = index - int(bool(validated.workflow_images))
            target = validated.mention_target_id if validated.kind == "TOOL_REPLY" else (
                job.mention_target_id
            )
            prefix = (
                MessageSegment.at(target)
                if part_index == 0 and target is not None and job.scene == "group" else None
            )
            await self._send_multi_reply_part(job, validated, part_index, prefix)
            return
        if result.kind == "TOOL_REPLY":
            if index != 0:
                raise ValueError("tool reply has exactly one delivery")
            validated = ConversationResult.model_validate(result.model_dump(by_alias=True))
            assert validated.reply is not None
            await self._reply_gateway.send_tool_reply(
                self._bot_provider(job.bot_id), scene=job.scene,
                target_id=job.group_id or job.user_id,
                reply=GeneratedReply.model_validate(validated.reply.model_dump(by_alias=True)),
                prefix=(
                    MessageSegment.at(validated.mention_target_id)
                    if validated.mention_target_id is not None and job.scene == "group" else None
                ),
                image=(
                    MessageSegment.image(f"base64://{validated.workflow_images[0].data}")
                    if validated.workflow_images else None
                ),
            )
            return
        if result.kind == "TEXT" or (result.kind == "MIXED" and index > 0):
            validated = ConversationResult.model_validate(result.model_dump(by_alias=True))
            assert validated.reply is not None
            full_reply = GeneratedReply.model_validate(validated.reply.model_dump(by_alias=True))
            part_index = index if result.kind == "TEXT" else index - 1
            prefix = None
            if part_index == 0:
                if result.kind == "TEXT":
                    target = job.mention_target_id
                elif job.auto_speak_max_reply_chars is not None:
                    target = None
                else:
                    target = job.user_id
                if target is not None and job.scene == "group":
                    prefix = MessageSegment.at(target)
            await self._reply_gateway.send_bot_exact_part(
                self._bot_provider(job.bot_id), scene=job.scene,
                target_id=job.group_id or job.user_id,
                reply=GeneratedReply(
                    text=validated.delivery_parts[part_index],
                    requestId=full_reply.request_id, source=full_reply.source,
                ),
                prefix=prefix,
            )
            return
        elif result.kind == "WORKFLOW_VIDEO":
            media = self._media_store_provider()
            if media is None:
                raise RuntimeError("video media storage is unavailable")
            owner = MediaOwner(entry="qq", session_id=job.session_id)
            asset = await media.describe(result.workflow_video_id, owner=owner)
            if asset.mime_type != "video/mp4":
                raise ChatAiProtocolError("video attachment type is invalid")
            async with media.open_content(asset.id, owner=owner) as handle:
                data = await finish_database_write(handle.read)
                media_segment = MessageSegment.video(
                    "base64://" + base64.b64encode(data).decode("ascii")
                )
            await self._reply_gateway.send_video_asset_bot(
                self._bot_provider(job.bot_id), scene=job.scene,
                target_id=job.group_id or job.user_id, segment=media_segment, asset=asset,
            )
            return
        elif result.kind in {"MIXED", "WORKFLOW_IMAGES"}:
            artifact = result.workflow_images[index]
            media_segment = MessageSegment.image(f"base64://{artifact.data}")
            prefix = None
        else:
            assert result.media is not None
            prefix = (
                MessageSegment.at(job.mention_target_id)
                if index == 0 and job.mention_target_id is not None and not image_completion
                else None
            )
            item = result.media.items[0]
            media_segment = (
                MessageSegment.image(f"base64://{item.data}")
                if item.type == "IMAGE"
                else MessageSegment.record(f"base64://{item.data}")
            )
        if (execution := active_image_task.get()) is not None:
            await execution.check_cancelled()
        try:
            await self._reply_gateway.send_media_bot(
                self._bot_provider(job.bot_id), scene=job.scene,
                target_id=job.group_id or job.user_id, segment=media_segment,
                prefix=prefix,
            )
        except Exception as error:
            if job.workflow_request is not None or bool(
                {"local_workflow_generate_image", "local_workflow_generate_video"}
                & set(job.chat_tools)
            ):
                # 只记录协议关闭码，避免对端的关闭原因正文夹带配置或凭据。
                received = error.rcvd if isinstance(error, ConnectionClosedError) else None
                sent = error.sent if isinstance(error, ConnectionClosedError) else None
                logger.warning(
                    "本地工作流处理失败：stage=napcat_delivery job_id=%s "
                    "request_id=%s session_id=%s result_kind=%s part_index=%s "
                    "image_count=%s error=%s ws_received_code=%s ws_sent_code=%s",
                    job.job_id,
                    job.request_id,
                    job.session_id,
                    result.kind,
                    index,
                    len(result.workflow_images),
                    type(error).__name__,
                    received.code if received is not None else None,
                    sent.code if sent is not None else None,
                )
            raise

    async def deliver_failure(self, job: ConversationJob, error: Exception) -> None:
        """终态失败结束原输入保护；只有用户实际收到的失败提示才能登记为助手消息。"""
        try:
            async with self._delivery_scope(job, "failure"):
                await self._deliver_failure(job, error)
        finally:
            if self._finish_received_callback is not None:
                async with self._runtime_lease():
                    await self._finish_received(job)

    async def _finish_received(self, job: ConversationJob) -> None:
        """只结束真实用户请求；重试中的生成失败不能调用，派生任务没有用户占位。"""
        if (
            self._finish_received_callback is not None and not job.background_image
            and job.ai_job_id is None and job.auto_speak_max_reply_chars is None
        ):
            await self._finish_received_callback(job.session_id, job.request_id)

    async def _deliver_failure(self, job: ConversationJob, error: Exception) -> None:
        """繁忙拒绝保持静默；其他工具及执行失败由无工具 LLM 说明。"""

        if (
            job.ai_job_id is not None
            or job.auto_speak_max_reply_chars is not None
        ):
            return
        if is_chat_busy_error(error) and not (
            isinstance(error, ChatAiError) and error.error_code == "AI_RATE_LIMITED"
        ):
            return
        if model_configuration_error_code(error) is not None:
            await self._reply_gateway.send_configuration_error(
                self._bot_provider(job.bot_id), scene=job.scene,
                target_id=job.group_id or job.user_id,
            )
            return
        if isinstance(error, _SILENT_FAILURES):
            return
        unavailable_workflow = isinstance(error, WorkflowNotFoundError)
        intent = ReplyIntent(
            requestId=job.request_id, sessionId=job.session_id,
            code="FEATURE_UNAVAILABLE" if unavailable_workflow else "TASK_FAILED",
            facts=FeatureUnavailableFacts(feature="所选本地绘图工作流")
            if unavailable_workflow else TaskFailedFacts(
                operation=self._operation_name(job), error_type=type(error).__name__,
                error_code=error.error_code if isinstance(error, ChatAiError) else None,
            ),
            originalPrompt=job.prompt, user=job.user, userName=job.user_name,
            chatContext=job.chat_context,
        )
        reply = await self._reply_composer.compose(intent)
        if reply is not None:
            if (execution := active_image_task.get()) is not None:
                await execution.check_cancelled()
            await self._reply_gateway.send_bot(
                self._bot_provider(job.bot_id), scene=job.scene,
                target_id=job.group_id or job.user_id, reply=reply,
            )

    def _operation_name(self, job: ConversationJob) -> str:
        """按实际任务类型报告失败；可用绘图工具只代表权限，不能证明发生过绘图。"""

        if job.background_image or job.media_output == "IMAGE":
            return "image_generation"
        if job.media_output == "AUDIO":
            return "audio_generation"
        return "conversation"

    def _reply_limit(self, job: ConversationJob, configured_limit: int) -> int:
        """普通聊天使用全局上限，自动发言在其专用上限内进一步收紧。"""

        if job.auto_speak_max_reply_chars is None:
            return configured_limit
        return min(configured_limit, job.auto_speak_max_reply_chars)

    async def commit(self, job: ConversationJob, result: ConversationResult) -> None:
        """普通轮次保存用户请求；主动发言和后台结果由真实出站日志独立承载。"""

        if (result.kind == "SILENT" or job.background_image
                or (self._outgoing_recorder is not None
                    and job.auto_speak_max_reply_chars is not None)):
            if self._finish_received_callback is not None:
                async with self._runtime_lease():
                    await self._finish_received(job)
            return
        if job.ai_job_id is not None:
            # 定时结果只有确认发送的助手消息；确认 Job 不得把内部任务提示伪造成用户。
            async with self._runtime_lease():
                await self._chat_service_provider().client.acknowledge_job(
                    job.bot_id, job.ai_job_id,
                )
            return
        if result.kind == "IMAGE_QUEUED":
            assert result.reply is not None
            delivered_answer = f"{result.reply.text}\n[绘图任务已提交]"
        elif result.kind in {"TEXT", "MIXED", "TOOL_REPLY"}:
            assert result.reply is not None
            delivered_answer = result.reply.text
        elif result.kind == "TOOL_IMAGES":
            delivered_answer = ""
        elif result.kind == "WORKFLOW_VIDEO":
            delivered_answer = "[已生成视频]"
        elif result.kind == "WORKFLOW_IMAGES":
            delivered_answer = "[已生成图片]"
        else:
            assert result.media is not None
            item = result.media.items[0]
            delivered_answer = (
                result.media.text.strip()
                if result.media.text is not None and result.media.text.strip()
                else "[已生成图片]" if item.type == "IMAGE" else "[已生成语音]"
            )
        if result.tool_images:
            marker = f"[已发送 {len(result.tool_images)} 张工具图片]"
            delivered_answer = f"{delivered_answer}\n{marker}".strip()
        if self._outgoing_recorder is not None:
            # 完整文件在出站日志；为用户问题及群昵称留预算，避免大附件挤掉整个配对轮次。
            marker = "\n[完整已发送内容见出站日志和素材]"
            budget = self._config_provider().chat_history_max_chars
            preview_limit = min(32_000, max(1, budget - len(job.prompt) - 256 - len(marker)))
            if len(delivered_answer) > preview_limit:
                delivered_answer = delivered_answer[:preview_limit] + marker
        async with self._runtime_lease():
            await self._chat_service_provider().commit_queued(
                job.session_id,
                job.prompt,
                delivered_answer,
                chat_context=job.chat_context,
                request_id=job.request_id,
            )
            await self._finish_received(job)
