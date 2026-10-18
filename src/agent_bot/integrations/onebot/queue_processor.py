"""持久化会话队列的模型来源、投递与失败解释。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

from nonebot.adapters.onebot.v11 import Bot, MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed, ApiNotAvailable, NetworkError

from agent_bot.ai.chat_errors import (
    ChatAiError,
    ChatAiProtocolError,
    ChatAiRejectedError,
    ChatAiUnavailableError,
)
from agent_bot.config import RuntimeConfig
from agent_bot.integrations.onebot.replies import OneBotReplyGateway
from agent_bot.integrations.workflow_store import WorkflowNotFoundError
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
from agent_bot.services.replies import (
    FeatureUnavailableFacts,
    GeneratedReply,
    LlmReplyComposer,
    ReplyIntent,
    TaskFailedFacts,
    WorkflowStatusFacts,
)
from agent_bot.services.tool_images import capture_tool_images, current_tool_images
from agent_bot.services.tool_operations import ImageEffect, ReplyEffect, ToolOperationStore
from agent_bot.services.workflow_execution import WorkflowExecutionService, WorkflowImageRequest
from agent_bot.state import AccessPolicy, split_reply

RuntimeLease = Callable[[], AbstractAsyncContextManager[None]]
LegacyToolSessionValue = tuple[Sequence[ChatToolName], str | None]
ToolSession = AbstractAsyncContextManager[Any]

logger = logging.getLogger(__name__)


_SILENT_FAILURES = (
    ChatRateLimitedError, ConversationQueueFullError, ConversationJobExpiredError,
    ChatAiUnavailableError, ChatAiRejectedError, ChatAiProtocolError,
    ModelGuardUnavailableError,
)


def generated_reply_from_model(
    text: str, request_id: str, *, as_file: bool = False,
) -> GeneratedReply:
    """仅适配非空模型正文，文件保留格式；空白结果不能二次模型解释。"""

    normalized = text if as_file else text.strip()
    if not normalized.strip():
        raise ChatAiProtocolError("model reply is empty")
    return GeneratedReply.model_validate(
        {"text": normalized, "requestId": request_id}, context={"file_reply": as_file},
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
    ) -> None:
        """保存运行时依赖提供器，使配置热更新后无需重建处理器。"""

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

    @property
    def retryable_errors(self) -> tuple[type[Exception], ...]:
        """返回队列编排层允许按策略重试的业务与 OneBot 异常。"""

        return (
            ChatAiUnavailableError,
            ModelGuardUnavailableError,
            ConnectionError,
            TimeoutError,
            ActionFailed,
            ApiNotAvailable,
            NetworkError,
        )

    async def generate(self, job: ConversationJob) -> ConversationResult:
        """将本次 AI 附件与已生成结果一并保存，投递重试复用队列快照。"""
        with capture_tool_images() as images:
            result = await self._generate(job)
        if images.items:
            result = ConversationResult.model_validate(result.model_copy(update={
                "tool_images": images.items,
            }).model_dump(by_alias=True))
        return result

    async def _generate(self, job: ConversationJob) -> ConversationResult:
        """生成文本或媒体并规范化已生成任务正文，工作流只持久化首张已验证图片。"""

        async with self._runtime_lease():
            if (execution := active_image_task.get()) is not None:
                await execution.check_cancelled()
            if job.ai_job_reply is not None:
                # 已生成的任务也先走回复格式校验，确保持久化正文与发送分段相同。
                generated = generated_reply_from_model(job.ai_job_reply.text, job.request_id)
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
                                {"system_prompt_append": job.system_prompt_append}
                                if job.system_prompt_append is not None
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
                    elif "local_workflow_generate_image" in job.chat_tools:
                        # 只记录本地稳定错误码，不输出可能含凭据的异常正文。
                        logger.warning(
                            "本地工作流处理失败：stage=agent_generate job_id=%s "
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

    async def _submit_image(
        self, job: ConversationJob, workflow_request: WorkflowImageRequest | None = None,
        *, start_reply: str | None = None,
    ) -> str:
        """答复送达后将受限绘图参数持久化到独立队列，不等待生成。"""

        if (
            workflow_request is None
            and not self._config_provider().chat_image_generation_enabled
        ):
            raise RuntimeError("image generation is unavailable")
        if workflow_request is not None and start_reply is None:
            raise ValueError("workflow submission requires the agent start reply")
        await self._send_image_start(job, text=start_reply)
        task_id = "image-" + hashlib.sha256(job.job_id.encode("utf-8")).hexdigest()[:32]
        task = ConversationJob.model_validate(job.model_copy(update={
            "job_id": task_id, "request_id": f"{job.request_id[:120]}-image",
            "background_image": True, "workflow_request": workflow_request,
            "media_output": "IMAGE" if workflow_request is None else None,
            "chat_tools": (), "image_urls": (), "video_urls": (),
            "image_start_reply": None, "image_start_parts": None,
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
        return ConversationResult(kind="WORKFLOW_IMAGES", workflowImages=(WorkflowImageArtifact(
            mimeType=saved.media_type, data=saved.data,
        ),))

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
        """图片送达后通知原创建人；保存模型文案供重试复用，群聊始终原生 @。"""

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
        )

    async def delivery_count(self, job: ConversationJob, result: ConversationResult) -> int:
        """通用图片逐张投递，原有文本或媒体随后投递。"""
        return len(result.tool_images) + await self._delivery_count(job, result)

    def _requires_image_completion(
        self, job: ConversationJob, result: ConversationResult,
    ) -> bool:
        """群聊生成图片与后台绘图追加完成通知，语音不进入绘图通知流程。"""

        if not job.background_image and job.scene != "group":
            return False
        return result.kind == "WORKFLOW_IMAGES" or (
            result.kind == "MEDIA" and result.media is not None
            and result.media.items[0].type == "IMAGE"
        )

    async def _delivery_count(
        self,
        job: ConversationJob,
        result: ConversationResult,
    ) -> int:
        """将图片和完成通知分别计入进度，通知重试时不重发已送达图片。"""

        if self._requires_image_completion(job, result):
            return 2
        if result.reply_as_file:
            return 1 + int(self._file_mention_target(job, result) is not None)
        if result.kind in {"SILENT", "IMAGE_QUEUED", "TOOL_IMAGES"}:
            return 0
        if result.kind == "TEXT":
            return len(result.delivery_parts)
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

    async def deliver_part(
        self, job: ConversationJob, result: ConversationResult, index: int,
    ) -> None:
        """按持久化索引先发图片再通知创建人，失败后不重发已确认附件。"""
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
            if (
                job.workflow_request is not None
                or "local_workflow_generate_image" in job.chat_tools
            ):
                logger.warning(
                    "本地工作流处理失败：stage=napcat_delivery job_id=%s "
                    "request_id=%s session_id=%s result_kind=%s part_index=%s "
                    "image_count=%s error=%s",
                    job.job_id,
                    job.request_id,
                    job.session_id,
                    result.kind,
                    index,
                    len(result.workflow_images),
                    type(error).__name__,
                )
            raise

    async def deliver_failure(self, job: ConversationJob, error: Exception) -> None:
        """配置和忙碌状态直接通知，其余模型失败静默，业务失败才调用解释模型。"""

        if (
            job.ai_job_id is not None
            or job.auto_speak_max_reply_chars is not None
        ):
            return
        if is_chat_busy_error(error):
            await self._reply_gateway.send_busy_error(
                self._bot_provider(job.bot_id), scene=job.scene,
                target_id=job.group_id or job.user_id,
            )
            return
        if model_configuration_error_code(error) is not None:
            await self._reply_gateway.send_configuration_error(
                self._bot_provider(job.bot_id), scene=job.scene,
                target_id=job.group_id or job.user_id,
            )
            return
        if "mention_member" in job.chat_tools:
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
        """将任务类型转换为不包含用户内容的稳定失败操作名。"""

        if (
            job.background_image or job.media_output == "IMAGE"
            or "local_workflow_generate_image" in job.chat_tools
        ):
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
        """在消息全部投递后提交一轮对话历史。"""

        if result.kind == "SILENT" or job.background_image:
            return
        if result.kind == "IMAGE_QUEUED":
            assert result.reply is not None
            delivered_answer = f"{result.reply.text}\n[绘图任务已提交]"
        elif result.kind in {"TEXT", "MIXED", "TOOL_REPLY"}:
            assert result.reply is not None
            delivered_answer = result.reply.text
        elif result.kind == "TOOL_IMAGES":
            delivered_answer = ""
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
        async with self._runtime_lease():
            await self._chat_service_provider().commit_queued(
                job.session_id,
                job.prompt,
                delivered_answer,
                chat_context=job.chat_context,
                request_id=job.request_id,
            )
            if job.ai_job_id is not None:
                await self._chat_service_provider().client.acknowledge_job(
                    job.bot_id, job.ai_job_id,
                )
