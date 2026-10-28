"""Typed registry and concrete tools owned by the chat gateway."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import time
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from agent_bot.database_pool import finish_database_write
from agent_bot.integrations.gpt_sovits import GeneratedSpeech, GptSoVitsError
from agent_bot.integrations.tagged_memory_store import (
    TaggedMemoryCapacityError,
    TaggedMemoryConflictError,
    TaggedMemoryStoreError,
)
from agent_bot.integrations.workflow_store import WorkflowNotFoundError
from agent_bot.media.contracts import MediaOwner
from agent_bot.mentions import (
    AmbiguousMentionTargetError,
    InvalidGroupMemberDataError,
    InvalidMentionDirectiveError,
    MentionTargetNotFoundError,
    resolve_tool_mention,
)
from agent_bot.models import ChatContext
from agent_bot.multimodal import DownloadedImage
from agent_bot.services.chat_reply import ChatReplyCollector
from agent_bot.services.image_tasks import (
    ImageTaskCancellation,
    ImageTaskError,
    ImageTaskList,
    ImageTaskOwner,
)
from agent_bot.services.naraka import (
    NarakaAuthenticationError,
    NarakaBusyError,
    NarakaDataUpdatingError,
    NarakaError,
    NarakaNotFoundError,
    NarakaRateLimitedError,
    NarakaUnavailableError,
    is_naraka_role_id,
)
from agent_bot.services.tagged_memory import (
    MEMORY_TOOL_DESCRIPTION,
    MemoryArguments,
    MemoryResult,
    MemorySession,
    MemorySourceError,
)
from agent_bot.services.tool_operations import (
    EffectToolName,
    ImageEffect,
    ImageTaskEffect,
    ReplyEffect,
    SpeechEffect,
    ToolEffect,
    ToolOperationConflict,
    ToolOperationStore,
)
from agent_bot.services.workflow_execution import (
    GeneratedWorkflowImage,
    WorkflowExecutionError,
    WorkflowImageCollector,
    WorkflowImageRequest,
    WorkflowSummary,
)
from agent_bot.services.workflow_inputs import WorkflowInputImage

_MAX_RESULT_CHARS = 32_000
_MAX_SEARCH_RESULTS = 10

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from agent_bot.services.bot_config_tools import BotConfigTools


class _Bot(Protocol):
    async def call_api(self, api: str, **data: object) -> object: ...


class _NarakaService(Protocol):
    async def query(self, session_id: str, role_id: str) -> object: ...

    async def query_by_nickname(self, session_id: str, nickname: str) -> object: ...

    async def search_players(self, session_id: str, nickname: str) -> object: ...


class _WorkflowService(Protocol):
    async def list_workflows(self) -> tuple[WorkflowSummary, ...]: ...

    async def describe_workflow(self, workflow_key: str) -> WorkflowSummary: ...

    async def prepare_request(
        self, *, workflow_key: str, positive_prompt: str, negative_prompt: str,
        image_urls: tuple[str, ...] = (), request_scope: str,
        input_image: DownloadedImage | None = None,
    ) -> WorkflowImageRequest: ...

    async def generate(
        self,
        *,
        workflow_key: str,
        positive_prompt: str,
        negative_prompt: str,
        input_image: WorkflowInputImage | None = None,
        workflow_sha256: str | None = None,
    ) -> GeneratedWorkflowImage: ...


class _TtsService(Protocol):
    async def generate(self, text: str) -> GeneratedSpeech: ...


class ImageTaskService(Protocol):
    """仅允许按认证身份列出和取消当前用户的绘图任务。"""

    async def list_tasks(self, owner: ImageTaskOwner, *, limit: int = 20) -> ImageTaskList:
        """返回不含提示词或连接信息的有界任务摘要。"""
        ...

    async def cancel(self, owner: ImageTaskOwner, task_id: str) -> ImageTaskCancellation:
        """幂等取消当前身份拥有的任务。"""
        ...


@dataclass(frozen=True, slots=True)
class CollectedSpeech:
    """一次 MCP 调用生成且等待 OneBot 投递的语音。"""

    text: str
    speech: GeneratedSpeech | None
    audio: bytes | None = None


class SpeechCollector:
    """并发安全地限制一次 Agent 请求只能生成一条语音。"""

    def __init__(self) -> None:
        """初始化空的请求级语音收集器。"""

        self._calls = 0
        self._speech: CollectedSpeech | None = None
        self._lock = asyncio.Lock()

    @property
    def speech(self) -> CollectedSpeech | None:
        """返回已经生成的语音快照。"""

        return self._speech

    async def reserve_call(self) -> int:
        """在生成副作用前占用唯一一次调用额度。"""

        async with self._lock:
            if self._calls >= 1:
                raise GptSoVitsError("LIMIT_EXCEEDED", "本次语音生成次数已达上限")
            self._calls += 1
            return self._calls

    async def add(self, text: str, speech: GeneratedSpeech) -> None:
        """保存已经由 GPT-SoVITS 校验并落盘的 WAV。"""

        if (
            not text.strip()
            or speech.media_type != "audio/wav"
            or speech.bytes <= 0
            or speech.bytes > 10_485_760
        ):
            raise GptSoVitsError("INVALID_RESPONSE", "GPT-SoVITS 返回的语音无效")
        async with self._lock:
            if self._speech is not None:
                raise GptSoVitsError("LIMIT_EXCEEDED", "本次语音生成次数已达上限")
            self._speech = CollectedSpeech(text=text.strip(), speech=speech)

    async def restore(self, text: str, audio: bytes) -> None:
        """从持久化结果恢复 WAV，无需依赖原进程生成的临时文件。"""

        if (
            not text.strip() or not 12 <= len(audio) <= 10_485_760
            or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE"
        ):
            raise GptSoVitsError("INVALID_RESPONSE", "持久化语音结果无效")
        async with self._lock:
            if self._speech is None:
                self._speech = CollectedSpeech(text=text.strip(), speech=None, audio=audio)


@dataclass(slots=True)
class ChatToolContext:
    """保存服务端绑定的真实身份、工具白名单和请求级资源，模型不能自行构造。"""

    session_id: str
    user_id: str
    allowed_tools: frozenset[str]
    user_name: str | None = None
    chat_context: ChatContext | None = None
    group_id: int | None = None
    bot_user_id: str | None = None
    bot: _Bot | None = None
    naraka_service: _NarakaService | None = None
    group_member_provider: Callable[[], Awaitable[object]] | None = field(
        default=None,
        repr=False,
    )
    mention_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    reply_collector: ChatReplyCollector = field(default_factory=ChatReplyCollector, repr=False)
    group_members: object | None = field(default=None, repr=False)
    workflow_service: _WorkflowService | None = field(default=None, repr=False)
    workflow_collector: WorkflowImageCollector | None = field(default=None, repr=False)
    image_tasks: ImageTaskService | None = field(default=None, repr=False)
    image_urls: tuple[str, ...] = field(default=(), repr=False)
    image_material_ids: tuple[str, ...] = field(default=(), repr=False)
    material_store: Any = field(default=None, repr=False)
    # 仅真实主人入站事件签发；不从群消息、模型参数或自动任务继承。
    material_library_access: bool = False
    workflow_request_scope: str = field(default_factory=lambda: secrets.token_hex(16))
    tts_service: _TtsService | None = None
    tts_collector: SpeechCollector | None = field(default=None, repr=False)
    job_id: str | None = None
    operation_store: ToolOperationStore | None = field(default=None, repr=False)
    deadline_at: float | None = None
    memory_session: MemorySession | None = field(default=None, repr=False)
    bot_config_tools: BotConfigTools | None = field(default=None, repr=False)

    def ensure_active(self) -> None:
        """工具回调和实际副作用均不得超过原消息的绝对截止时间。"""

        if self.deadline_at is not None and time.monotonic() >= self.deadline_at:
            raise ChatToolError("DEADLINE_EXCEEDED", "本次请求已超时")

    async def begin_effect(
        self, tool: EffectToolName, signature: str,
    ) -> ToolEffect | None:
        """持久化排队工具的执行权；直接调用仍使用请求内的次数约束。"""

        self.ensure_active()
        if self.operation_store is None or self.job_id is None:
            return None
        return await self.operation_store.begin(self.job_id, tool, signature)

    async def complete_effect(
        self, tool: EffectToolName, signature: str, result: ToolEffect,
    ) -> None:
        """在返回模型之前保存外部操作结果。"""

        if self.operation_store is not None and self.job_id is not None:
            await self.operation_store.complete(self.job_id, tool, signature, result)

    async def restore_effect(self, result: ToolEffect) -> None:
        """把已验证的持久媒体重新放入本次投递收集器。"""

        if isinstance(result, ImageTaskEffect):
            if self.workflow_collector is None:
                raise ChatToolError("FORBIDDEN", "本次请求未授权图片生成")
            self.workflow_collector.queued_task_id = result.task_id
        elif isinstance(result, ImageEffect):
            if self.workflow_collector is None:
                raise ChatToolError("FORBIDDEN", "本次请求未授权图片生成")
            if not self.workflow_collector.images:
                await self.workflow_collector.add(GeneratedWorkflowImage(
                    media_type=result.media_type,
                    data=base64.b64decode(result.data, validate=True),
                ))
        elif isinstance(result, SpeechEffect):
            if self.tts_collector is None:
                raise ChatToolError("FORBIDDEN", "本次请求未授权语音生成")
            await self.tts_collector.restore(
                result.text, base64.b64decode(result.data, validate=True),
            )


class ChatToolError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ChatToolResult:
    content: str
    is_error: bool = False


ToolHandler = Callable[[dict[str, object], ChatToolContext], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class ChatToolDefinition:
    name: str
    description: str
    input_schema: dict[str, object]
    handler: ToolHandler = field(repr=False, compare=False)


class ChatToolRegistry:
    def __init__(self, definitions: Sequence[ChatToolDefinition]) -> None:
        self._definitions: dict[str, ChatToolDefinition] = {}
        for definition in definitions:
            if definition.name in self._definitions:
                raise ValueError(f"duplicate tool name: {definition.name}")
            self._definitions[definition.name] = definition

    def list_tools(self, allowed_tools: frozenset[str]) -> list[ChatToolDefinition]:
        return [
            definition
            for name, definition in self._definitions.items()
            if name in allowed_tools
        ]

    async def call(
        self,
        name: str,
        arguments: Mapping[str, object] | None,
        context: ChatToolContext,
    ) -> ChatToolResult:
        context.ensure_active()
        definition = self._definitions.get(name)
        if definition is None:
            raise ChatToolError("NOT_FOUND", "工具不存在")
        if name not in context.allowed_tools:
            raise ChatToolError("FORBIDDEN", "当前请求未授权此工具")
        try:
            value = await definition.handler(dict(arguments or {}), context)
            content = _json_content(value)
            return ChatToolResult(content=content)
        except ChatToolError:
            raise
        except ToolOperationConflict as error:
            raise ChatToolError("EXECUTION_UNCERTAIN", str(error)) from error
        except (MentionTargetNotFoundError, NarakaNotFoundError) as error:
            raise ChatToolError("NOT_FOUND", "未找到目标") from error
        except (AmbiguousMentionTargetError, InvalidGroupMemberDataError) as error:
            raise ChatToolError("INVALID_ARGUMENT", "目标不明确或群成员数据无效") from error
        except InvalidMentionDirectiveError as error:
            raise ChatToolError("FORBIDDEN", "不能执行该提醒目标") from error
        except ValidationError as error:
            raise ChatToolError("INVALID_ARGUMENT", _validation_error_message(error)) from error
        except ValueError as error:
            raise ChatToolError("INVALID_ARGUMENT", "工具参数无效") from error
        except (
            NarakaAuthenticationError,
            NarakaBusyError,
            NarakaDataUpdatingError,
            NarakaRateLimitedError,
            NarakaUnavailableError,
        ) as error:
            raise ChatToolError("TEMPORARILY_UNAVAILABLE", "工具服务暂时不可用") from error
        except NarakaError as error:
            raise ChatToolError("UPSTREAM_ERROR", "永劫查询失败") from error
        except (WorkflowExecutionError, ImageTaskError) as error:
            raise ChatToolError(error.code, str(error)) from error
        except WorkflowNotFoundError as error:
            raise ChatToolError(
                "WORKFLOW_UNAVAILABLE",
                "所选工作流不存在或已禁用，请调用 local_workflow_list 重新选择 workflow_key",
            ) from error
        except GptSoVitsError as error:
            raise ChatToolError(error.code, str(error)) from error
        except Exception as error:
            raise ChatToolError("INTERNAL", "工具执行失败") from error


class _StrictArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class NarakaQueryArguments(_StrictArguments):
    role_id: str | None = Field(default=None, max_length=32)
    nickname: str | None = Field(default=None, min_length=1, max_length=32)

    @model_validator(mode="after")
    def exactly_one_target(self) -> NarakaQueryArguments:
        if (self.role_id is None) == (self.nickname is None):
            raise ValueError("exactly one Naraka target is required")
        if self.role_id is not None and not is_naraka_role_id(self.role_id):
            raise ValueError("invalid Naraka role ID")
        if self.nickname is not None and _has_control(self.nickname):
            raise ValueError("invalid Naraka nickname")
        return self


class NarakaSearchArguments(_StrictArguments):
    nickname: str = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def safe_nickname(self) -> NarakaSearchArguments:
        if _has_control(self.nickname):
            raise ValueError("invalid Naraka nickname")
        return self


class MentionArguments(_StrictArguments):
    target: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=500)


class ImageGenerateArguments(_StrictArguments):
    """直接模型生图只接收画面描述及开画文案，不允许选择服务、模型或身份。"""

    prompt: str = Field(
        min_length=1, max_length=2000,
        description="完整图片生成描述，只写画面要求，不附分析或聊天答复。",
    )
    start_reply: str = Field(
        min_length=1, max_length=1000,
        description="面向用户的一句开画答复；送达后才排队，不能声称图片已完成。",
    )


class WorkflowGenerateArguments(_StrictArguments):
    start_reply: str = Field(
        min_length=1, max_length=1000,
        description=(
            "决定绘图后向用户发送的一句开画答复，网关送达后才提交任务。"
            "不能声称已经提交或完成；如果拒绝绘图，应直接回复用户，不要调用此工具。"
        ),
    )
    workflow_key: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9-]{0,127}$",
        description="必须明确填写 local_workflow_list 返回的可用工作流 key，不使用默认回退",
    )
    positive_prompt: str = Field(
        min_length=1,
        max_length=2_000,
        description=(
            "忠于用户要求的最终画面描述，按主体与风格、外貌、服饰、姿势与焦点、"
            "构图与视角、环境与光色组织，重要特征靠前，避免重复和冲突。"
            "非人物跳过不适用项；图生图写清保留特征与调整目标，仅修改用户要求的部分。"
            "遵循所选工作流的语言和格式要求，不附分析、标题或聊天答复。"
        ),
    )
    negative_prompt: str = Field(
        default="",
        max_length=2_000,
        description=(
            "只写需要排除的缺陷、干扰或用户明确不需要的元素，不得与正向要求冲突。"
            "不要套用会删掉所需细节的通用负面词；无必要排除项时留空。"
        ),
    )


class WorkflowVideoArguments(_StrictArguments):
    """H3 仅接受运动描述、工作流键和开始文案，不开放地址或负向字段。"""

    start_reply: str = Field(min_length=1, max_length=1000)
    workflow_key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,127}$")
    positive_prompt: str = Field(min_length=1, max_length=2000)


class WorkflowTasksArguments(_StrictArguments):
    """当前用户任务列表的服务端有界分页参数。"""

    limit: int = Field(default=20, ge=1, le=20, strict=True)


class WorkflowCancelArguments(_StrictArguments):
    """只允许队列任务标识，不接受身份或上游提示标识。"""

    task_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]{1,128}$")


class TtsGenerateArguments(_StrictArguments):
    text: str = Field(
        min_length=1,
        max_length=4_096,
        description=(
            "需要合成的完整可朗读正文；不要传入括号语气说明或表情，"
            "应使用自然措辞、语气词和标点表达语气"
        ),
    )

    @field_validator("text")
    @classmethod
    def require_speakable_character(cls, value: str) -> str:
        """删除不可朗读标记，并拒绝清洗后没有正文的文本。"""

        normalized = value.strip()
        while True:
            normalized, replacements = re.subn(
                r"\([^()]*\)|\uff08[^\uff08\uff09]*\uff09",
                "",
                normalized,
            )
            if replacements == 0:
                break
        normalized = re.sub(r"[0-9#*]\ufe0f?\u20e3", "", normalized)
        spoken_characters: list[str] = []
        for character in normalized:
            code_point = ord(character)
            category = unicodedata.category(character)
            if (
                category in {"So", "Sk", "Cf"}
                or code_point in {0x20E3, 0xFE0E, 0xFE0F}
                or 0xE0020 <= code_point <= 0xE007F
            ):
                continue
            spoken_characters.append(character)
        normalized = re.sub(r"\s+", " ", "".join(spoken_characters)).strip()
        if not any(character.isalnum() for character in normalized):
            raise ValueError("TTS text must contain a speakable character")
        return normalized


def _validation_error_message(error: ValidationError) -> str:
    """Return bounded field-level validation details without echoing input values."""
    type_messages = {
        "missing": "必填",
        "extra_forbidden": "不支持",
        "string_too_short": "长度不足",
        "string_too_long": "长度超过限制",
        "string_pattern_mismatch": "格式不正确",
        "string_type": "必须是字符串",
        "int_type": "必须是整数",
        "int_parsing": "必须是整数",
        "bool_type": "必须是布尔值",
        "bool_parsing": "必须是布尔值",
        "value_error": "取值不正确",
    }
    details: list[str] = []
    for item in error.errors(include_url=False, include_input=False)[:5]:
        path = ".".join(str(part)[:64] for part in item.get("loc", ())) or "参数"
        reason = type_messages.get(str(item.get("type", "")), "类型或取值不正确")
        details.append(f"{path}：{reason}")
    return f"工具参数无效：{'；'.join(details)}"


async def _naraka_query(arguments: dict[str, object], context: ChatToolContext) -> object:
    parsed = NarakaQueryArguments.model_validate(arguments)
    if context.naraka_service is None:
        raise ChatToolError("TEMPORARILY_UNAVAILABLE", "永劫查询未配置")
    if parsed.role_id is not None:
        result = await context.naraka_service.query(context.session_id, parsed.role_id)
    else:
        result = await context.naraka_service.query_by_nickname(
            context.session_id, parsed.nickname or ""
        )
    payload = _model_payload(result)
    if isinstance(payload.get("overview"), list):
        payload["overview"] = payload["overview"][:20]
    if isinstance(payload.get("matches"), list):
        payload["matches"] = payload["matches"][:10]
    return payload


async def _naraka_search(arguments: dict[str, object], context: ChatToolContext) -> object:
    parsed = NarakaSearchArguments.model_validate(arguments)
    if context.naraka_service is None:
        raise ChatToolError("TEMPORARILY_UNAVAILABLE", "永劫查询未配置")
    result = await context.naraka_service.search_players(context.session_id, parsed.nickname)
    payload = _model_payload(result)
    players = payload.get("players")
    if isinstance(players, list):
        payload["players"] = players[:_MAX_SEARCH_RESULTS]
    return payload


async def _mention_member(arguments: dict[str, object], context: ChatToolContext) -> object:
    """校验当前群目标并登记提醒意图；实际发送统一使用 Agent 最终正文。"""

    parsed = MentionArguments.model_validate(arguments)
    if context.group_id is None or context.bot is None or context.bot_user_id is None:
        raise ChatToolError("FORBIDDEN", "仅可在当前群执行提醒")
    async with context.mention_lock:
        if context.group_members is None:
            if context.group_member_provider is not None:
                context.group_members = await context.group_member_provider()
            else:
                context.group_members = await context.bot.call_api(
                    "get_group_member_list",
                    group_id=context.group_id,
                    no_cache=True,
                )
        command = resolve_tool_mention(
            parsed.target,
            parsed.text,
            context.group_members,
            bot_user_id=context.bot_user_id,
        )
        context.ensure_active()
        await context.reply_collector.select(
            ReplyEffect(text=command.text, target_user_id=command.target_user_id),
            job_id=context.job_id, store=context.operation_store,
        )
        return {"status": "queued", "target_user_id": command.target_user_id}


def _image_task_owner(context: ChatToolContext) -> ImageTaskOwner:
    """从认证工具会话提取拥有者，不使用模型传入的身份。"""

    context.ensure_active()
    if not context.bot_user_id:
        raise ChatToolError("FORBIDDEN", "当前请求未绑定机器人身份")
    return ImageTaskOwner(
        bot_id=context.bot_user_id, session_id=context.session_id, user_id=context.user_id,
    )


async def _local_workflow_tasks(
    arguments: dict[str, object], context: ChatToolContext,
) -> ImageTaskList:
    """列出当前会话中当前用户自己的最近绘图任务。"""

    parsed = WorkflowTasksArguments.model_validate(arguments)
    if context.image_tasks is None:
        raise ChatToolError("UNAVAILABLE", "当前请求未启用图片任务管理")
    result = await context.image_tasks.list_tasks(_image_task_owner(context), limit=parsed.limit)
    context.ensure_active()
    return result


async def _local_workflow_cancel(
    arguments: dict[str, object], context: ChatToolContext,
) -> ImageTaskCancellation:
    """幂等取消当前会话中当前用户拥有的指定绘图任务。"""

    parsed = WorkflowCancelArguments.model_validate(arguments)
    if context.image_tasks is None:
        raise ChatToolError("UNAVAILABLE", "当前请求未启用图片任务管理")
    result = await context.image_tasks.cancel(_image_task_owner(context), parsed.task_id)
    context.ensure_active()
    return result


async def _local_workflow_list(
    arguments: dict[str, object],
    context: ChatToolContext,
) -> object:
    """返回当前数据库中可执行工作流的有界元数据。"""

    _StrictArguments.model_validate(arguments)
    if context.workflow_service is None:
        raise ChatToolError("TEMPORARILY_UNAVAILABLE", "本地工作流未配置")
    workflows = await context.workflow_service.list_workflows()
    return {
        "workflows": [
            {
                "key": workflow.key,
                "name": workflow.name,
                "description": workflow.description,
                "workflow_type": workflow.workflow_type,
                "requires_image": workflow.requires_image,
            }
            for workflow in workflows
        ]
    }


async def _image_generate(arguments: dict[str, object], context: ChatToolContext) -> object:
    """直接生图提交已有持久队列；恢复回执时不重复提交计费任务。"""
    parsed = ImageGenerateArguments.model_validate(arguments)
    collector = context.workflow_collector
    if collector is None or collector.submit_image is None:
        raise ChatToolError("TEMPORARILY_UNAVAILABLE", "直接生图队列未配置")
    signature = hashlib.sha256(parsed.model_dump_json().encode("utf-8")).hexdigest()
    cached = await context.begin_effect("image_generate", signature)
    if cached is not None:
        if not isinstance(cached, ImageTaskEffect):
            raise ChatToolError("INVALID_RESULT", "持久化图片任务无效")
        await context.restore_effect(cached)
        return {"status": "queued", "task_id": cached.task_id}
    await collector.reserve_call()
    context.ensure_active()
    task_id = await collector.submit_image(parsed.prompt, parsed.start_reply)
    collector.queued_task_id = task_id
    await context.complete_effect("image_generate", signature, ImageTaskEffect(task_id=task_id))
    return {"status": "queued", "task_id": task_id}


async def _save_generated_workflow_image(
    context: ChatToolContext, image: GeneratedWorkflowImage,
) -> None:
    """正常生成和回执恢复共用持久化；保存失败不得重调已经完成的工作流。"""
    if context.material_store is not None:
        await context.material_store.put(
            owner=MediaOwner(entry="qq", session_id=context.session_id),
            name="工作流生成图片", data=image.data, mime_type=image.media_type,
            category="generated",
        )


async def _generate_local_workflow(
    arguments: dict[str, object], context: ChatToolContext, *, video: bool = False,
) -> object:
    """图片与视频共享准备和提交额度；视频只通过持久队列投递。"""

    if video:
        arguments = {
            **WorkflowVideoArguments.model_validate(arguments).model_dump(),
            "negative_prompt": "",
        }
    parsed = WorkflowGenerateArguments.model_validate(arguments)
    tool = "local_workflow_generate_video" if video else "local_workflow_generate_image"
    if context.workflow_service is None or context.workflow_collector is None:
        raise ChatToolError("TEMPORARILY_UNAVAILABLE", "本地工作流未配置")
    workflow_key = parsed.workflow_key
    context.ensure_active()
    summary = await context.workflow_service.describe_workflow(workflow_key)
    if (summary.workflow_type == "image_to_video") != video:
        raise ChatToolError("INVALID_ARGUMENT", "请使用与工作流类型一致的生成工具")
    if video and context.workflow_collector.submit is None:
        raise ChatToolError("TEMPORARILY_UNAVAILABLE", "视频后台队列未配置")
    signature_data = parsed.model_dump_json().encode("utf-8")
    if summary.requires_image:
        source_digest = hashlib.sha256(
            json.dumps(tuple(dict.fromkeys(
                context.image_material_ids or context.image_urls
            ))).encode("utf-8"),
        ).hexdigest()
        signature_data += f"\n{context.workflow_request_scope}\n{source_digest}".encode()
    signature = hashlib.sha256(signature_data).hexdigest()
    cached = await context.begin_effect(tool, signature)
    if cached is not None:
        if isinstance(cached, ImageTaskEffect):
            await context.restore_effect(cached)
            return {"status": "queued", "task_id": cached.task_id}
        if video or not isinstance(cached, ImageEffect):
            raise ChatToolError("INVALID_RESULT", "持久化图片结果无效")
        await context.restore_effect(cached)
        await _save_generated_workflow_image(context, context.workflow_collector.images[0])
        return {
            "workflow_key": cached.workflow_key, "image_index": 1,
            "media_type": cached.media_type,
            "bytes": len(context.workflow_collector.images[0].data),
        }
    started_at = time.monotonic()
    call_index: int | str = "-"
    try:
        call_index = await context.workflow_collector.reserve_call()
        context.ensure_active()
        image_options = {}
        if summary.requires_image and context.image_material_ids:
            if len(context.image_material_ids) != 1 or context.image_urls:
                raise ChatToolError("INVALID_ARGUMENT", "图生图必须提供唯一原图")
            if context.material_store is None:
                raise ChatToolError("UNAVAILABLE", "素材存储未初始化")
            id = context.image_material_ids[0]
            owner = MediaOwner(entry="qq", session_id=context.session_id)
            asset = await context.material_store.describe(id, owner=owner)
            async with context.material_store.open_content(id, owner=owner) as file:
                data = await finish_database_write(file.read)
            image_options["input_image"] = DownloadedImage(asset.mime_type, data)
        request = await context.workflow_service.prepare_request(
            workflow_key=workflow_key, positive_prompt=parsed.positive_prompt,
            negative_prompt=parsed.negative_prompt, image_urls=context.image_urls,
            request_scope=context.workflow_request_scope,
            **image_options,
        )
        if (request.input_image is not None) != summary.requires_image:
            raise WorkflowExecutionError("WORKFLOW_CHANGED", "工作流类型已变更，请重新发起请求")
        if (request.output_kind == "video") != video:
            raise WorkflowExecutionError("WORKFLOW_CHANGED", "工作流类型已变更，请重新发起请求")
        if request.fixed_reference != (summary.workflow_type == "fixed_reference"):
            raise WorkflowExecutionError("WORKFLOW_CHANGED", "工作流类型已变更，请重新发起请求")
        context.ensure_active()
        if context.workflow_collector.submit is not None:
            task_id = await context.workflow_collector.submit(
                request,
                parsed.start_reply,
            )
            context.workflow_collector.queued_task_id = task_id
            await context.complete_effect(
                tool, signature, ImageTaskEffect(task_id=task_id),
            )
            return {"status": "queued", "task_id": task_id}
        if request.workflow_sha256 is None:
            image = await context.workflow_service.generate(
                workflow_key=workflow_key, positive_prompt=request.positive_prompt,
                negative_prompt=request.negative_prompt,
            )
        else:
            image = await context.workflow_service.generate(
                workflow_key=workflow_key, positive_prompt=request.positive_prompt,
                negative_prompt=request.negative_prompt, input_image=request.input_image,
                workflow_sha256=request.workflow_sha256,
            )
        image_index = await context.workflow_collector.add(image)
        await context.complete_effect(
            tool, signature,
            ImageEffect(
                workflow_key=workflow_key, media_type=image.media_type,
                data=base64.b64encode(image.data).decode("ascii"),
            ),
        )
        await _save_generated_workflow_image(context, image)
    except Exception as error:
        error_code = error.code if isinstance(error, WorkflowExecutionError) else "INTERNAL"
        logger.warning(
            "本地工作流工具调用失败：session_id=%s workflow_key=%s call_index=%s "
            "code=%s error=%s elapsed_ms=%s",
            context.session_id,
            workflow_key,
            call_index,
            error_code,
            type(error).__name__,
            round((time.monotonic() - started_at) * 1000),
        )
        raise
    return {
        "workflow_key": workflow_key,
        "image_index": image_index,
        "media_type": image.media_type,
        "bytes": len(image.data),
    }


async def _local_workflow_generate_image(
    arguments: dict[str, object], context: ChatToolContext
) -> object:
    """保留图片工具的旧参数和同步兼容入口。"""
    return await _generate_local_workflow(arguments, context)


async def _local_workflow_generate_video(
    arguments: dict[str, object], context: ChatToolContext
) -> object:
    """视频使用独立授权，与图片工具共用一次提交额度。"""
    return await _generate_local_workflow(arguments, context, video=True)


async def _tts_generate(arguments: dict[str, object], context: ChatToolContext) -> object:
    """调用当前请求绑定的受限语音服务。"""

    parsed = TtsGenerateArguments.model_validate(arguments)
    if context.tts_service is None or context.tts_collector is None:
        raise ChatToolError("TEMPORARILY_UNAVAILABLE", "GPT-SoVITS 未配置")
    signature = hashlib.sha256(parsed.model_dump_json().encode("utf-8")).hexdigest()
    cached = await context.begin_effect("tts_generate", signature)
    if cached is not None:
        if not isinstance(cached, SpeechEffect):
            raise ChatToolError("INVALID_RESULT", "持久化语音结果无效")
        await context.restore_effect(cached)
        audio = base64.b64decode(cached.data, validate=True)
        return {"media_type": "audio/wav", "bytes": len(audio)}
    await context.tts_collector.reserve_call()
    context.ensure_active()
    speech = await context.tts_service.generate(parsed.text)
    await context.tts_collector.add(parsed.text, speech)
    if context.operation_store is not None:
        audio = await asyncio.to_thread(speech.path.read_bytes)
        if (
            len(audio) != speech.bytes or not 12 <= len(audio) <= 10_485_760
            or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE"
        ):
            raise ChatToolError("INVALID_RESULT", "语音结果无效")
        await context.complete_effect(
            "tts_generate", signature,
            SpeechEffect(text=parsed.text, data=base64.b64encode(audio).decode("ascii")),
        )
    return {
        "path": speech.path.as_posix(),
        "media_type": speech.media_type,
        "bytes": speech.bytes,
    }


async def _memory(arguments: dict[str, object], context: ChatToolContext) -> MemoryResult:
    """把模型参数交给当前令牌绑定的记忆会话。"""
    if context.memory_session is None:
        raise ChatToolError("UNAVAILABLE", "当前请求未启用记忆")
    from agent_bot.memory.embeddings import EmbeddingError

    try:
        return await context.memory_session.execute(MemoryArguments.model_validate(arguments))
    except MemorySourceError as error:
        raise ChatToolError("MEMORY_SOURCE_INVALID", str(error)) from error
    except TaggedMemoryConflictError as error:
        raise ChatToolError("MEMORY_CONFLICT", str(error)) from error
    except TaggedMemoryCapacityError as error:
        raise ChatToolError("MEMORY_CAPACITY", str(error)) from error
    except TaggedMemoryStoreError as error:
        raise ChatToolError("MEMORY_UNAVAILABLE", "记忆数据库不可用，请检查初始化与连接") from error
    except EmbeddingError as error:
        raise ChatToolError("MEMORY_SEMANTIC_FAILED", str(error)) from error


def build_chat_tool_registry() -> ChatToolRegistry:
    """装配受会话授权约束的工具，主人配置能力仍须在执行时复核身份。"""
    from agent_bot.services.bot_config_tools import (
        BotConfigGetArguments,
        BotConfigUpdateArguments,
        bot_config_get,
        bot_config_update,
    )

    return ChatToolRegistry(
        [
            ChatToolDefinition(
                name="bot_config_get",
                description=("仅机器人主人可用。查询机器人配置、字段用途和版本；"
                             "可按 names 或 query 筛选，用 offset 分页。密钥仅展示设置状态。"
                             "修改前先查询字段、只读状态和版本，不得猜测配置名称。"),
                input_schema=BotConfigGetArguments.model_json_schema(),
                handler=bot_config_get,
            ),
            ChatToolDefinition(
                name="bot_config_update",
                description=("仅机器人主人可用。按用户明确要求修改查询得到的可编辑非密钥配置，"
                             "values 只提交本次修改，expected_revision 使用最新查询版本；"
                             "静态字段还须 startup_revision。冲突后重新查询，不盲目覆盖。"
                             "保存不代表已经生效，需重启字段由用户手动重启后端。"),
                input_schema=BotConfigUpdateArguments.model_json_schema(),
                handler=bot_config_update,
            ),
            ChatToolDefinition(
                name="memory",
                description=MEMORY_TOOL_DESCRIPTION,
                input_schema=MemoryArguments.model_json_schema(),
                handler=_memory,
            ),
            ChatToolDefinition(
                name="naraka_query",
                description=(
                    "查询具体永劫无间角色的完整战绩详情，包括段位、评分、统计指标和最近对局。"
                    "用户提供角色 ID 或昵称并询问战绩时必须使用本工具。"
                ),
                input_schema=NarakaQueryArguments.model_json_schema(),
                handler=_naraka_query,
            ),
            ChatToolDefinition(
                name="naraka_search",
                description=(
                    "仅按昵称搜索永劫无间候选角色，最多返回 10 条；"
                    "不能代替 naraka_query 查询具体角色的战绩。"
                ),
                input_schema=NarakaSearchArguments.model_json_schema(),
                handler=_naraka_search,
            ),
            ChatToolDefinition(
                name="mention_member",
                description=(
                    "为最终回复登记当前 QQ 群的原生 @ 目标，工具本身不会发送消息。"
                    "目标可使用 QQ 号、群名片或昵称，也可以是当前消息发送者。"
                    "根据对话自主决定是否 @ 成员，无须用户每次明确要求。"
                    "text 表达提醒意图，工具执行后仍必须返回完整的最终回复正文。"
                    "网关把目标的原生 @ 与最终正文合并发送一次；不要用普通文本模拟 @。"
                ),
                input_schema=MentionArguments.model_json_schema(),
                handler=_mention_member,
            ),
            ChatToolDefinition(
                name="local_workflow_tasks",
                description=(
                    "列出当前用户在当前会话中自己的最近图片任务，返回任务 ID、状态和取消信息。"
                    "不能查询其他用户、其他会话的任务；不返回提示词或服务连接信息。"
                ),
                input_schema=WorkflowTasksArguments.model_json_schema(),
                handler=_local_workflow_tasks,
            ),
            ChatToolDefinition(
                name="local_workflow_cancel",
                description=(
                    "取消当前用户在当前会话中自己的指定图片任务；task_id 来自提交或任务列表。"
                    "重复取消是安全的。如返回上游取消中、不支持或失败，必须如实说明；"
                    "上游接受取消不代表 GPU 已立即停止。较旧 Comfy 服务或云端生图"
                    "可能只能停止后续投递，不能保证上游已停止计算。"
                ),
                input_schema=WorkflowCancelArguments.model_json_schema(),
                handler=_local_workflow_cancel,
            ),
            ChatToolDefinition(
                name="image_generate",
                description=(
                    "直接使用管理员配置的图片生成模型生成一张图片，无需 Comfy 工作流。"
                    "prompt 写完整画面要求，start_reply 写面向用户的开画答复。"
                    "QQ 先发送开画答复再提交后台队列；queued 只表示已排队，不能声称已完成。"
                    "收到 queued 后本轮即结束，不调用等待或任务查询工具轮询进度。"
                    "单次请求最多生成一张图片，与工作流生图共享额度。"
                    "此工具仅支持文生图，不能将当前图片作为编辑输入。"
                ),
                input_schema=ImageGenerateArguments.model_json_schema(),
                handler=_image_generate,
            ),
            ChatToolDefinition(
                name="local_workflow_list",
                description=(
                    "列出本地图片与视频工作流及用途描述；"
                    "生成前必须先调用本工具并依据描述选择可用的 workflow_key。"
                ),
                input_schema=_StrictArguments.model_json_schema(),
                handler=_local_workflow_list,
            ),
            ChatToolDefinition(
                name="local_workflow_generate_image",
                description=(
                    "使用数据库中的本地 Comfy 工作流生成一张图片。"
                    "必须先调用 local_workflow_list，并显式传入与用户需求最匹配的 "
                    "workflow_key，不允许省略或回退默认工作流。"
                    "单次聊天请求最多生成一张图片，不允许重复调用。"
                    "决定绘图时将你面向用户的开画答复写入 start_reply，"
                    "若决定拒绝则直接返回拒绝正文，不调用绘图工具。"
                    "网关先发送 start_reply 再提交后台任务，不另行请求模型；"
                    "queued 仅表示提交成功，"
                    "收到 queued 后本轮即结束，不调用等待或任务查询工具轮询进度；"
                    "后台完成后先发送图片，再由 LLM 生成完成文案并 @ 原请求者；"
                    "当前不能声称图片已经生成；"
                    "不要再调用语音工具播报进度或重复发送完成说明。"
                ),
                input_schema=WorkflowGenerateArguments.model_json_schema(),
                handler=_local_workflow_generate_image,
            ),
            ChatToolDefinition(
                name="local_workflow_generate_video",
                description=(
                    "从唯一输入图片生成带声音的视频。先调用 local_workflow_list 选择 "
                    "image_to_video 工作流，将人物动作、镜头运动和环境声写入 positive_prompt。"
                    "start_reply 为开始制作的文案；每请求与图片生成共用一次额度。"
                    "后台完成后发送视频，不要重复调用或声称尚未完成的任务已完成。"
                ),
                input_schema=WorkflowVideoArguments.model_json_schema(),
                handler=_local_workflow_generate_video,
            ),
            ChatToolDefinition(
                name="tts_generate",
                description=(
                    "使用管理员配置的本地 GPT-SoVITS 音色把文本合成为语音文件；"
                    "你可以根据对话内容、语气和表达效果自由选择用文字还是语音回复。"
                    "闲聊、问候、安慰、讲述等场景也可以主动发语音，无须用户明确要求、"
                    "特定关键词或事先征求同意，也不用解释为什么选择语音。"
                    "用户明确要求文字或拒绝语音时不要调用。单次请求最多生成一条语音。"
                    "成功生成后由网关发送语音，不再额外发送主请求正文。"
                    "text 必须传入完整的可朗读回复正文，"
                    "不要包含括号语气说明或表情；应通过自然措辞、语气词和标点表达语气。"
                ),
                input_schema=TtsGenerateArguments.model_json_schema(),
                handler=_tts_generate,
            ),
        ]
    )


def _model_payload(value: object) -> dict[str, Any]:
    model_dump = getattr(value, "model_dump", None)
    if not callable(model_dump):
        raise ChatToolError("INTERNAL", "工具返回类型无效")
    payload = model_dump(mode="json")
    if not isinstance(payload, dict):
        raise ChatToolError("INTERNAL", "工具返回类型无效")
    return payload


def _json_content(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    content = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(content) > _MAX_RESULT_CHARS:
        raise ChatToolError("RESULT_TOO_LARGE", "工具结果超过限制")
    return content


def _has_control(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)
