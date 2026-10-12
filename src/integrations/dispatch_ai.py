"""dispatch-ai 异步 HTTP 客户端。"""

import asyncio
import base64
import binascii
import json
import re
from collections.abc import Sequence

import httpx
from pydantic import ValidationError

from agent_bot.models import (
    AgentChatChunk,
    AgentToolName,
    AiJobResult,
    AiJobResultsResponse,
    AutoSpeakDecisionRequest,
    AutoSpeakDecisionResponse,
    AutoSpeakDecisionResult,
    ChatContext,
    ChatMediaRequest,
    ChatMediaResponse,
    ChatMediaResult,
    ChatMessage,
    ChatToolName,
    DispatchAiInputContent,
    DispatchAiInputMessage,
    DispatchAiRequest,
    DispatchAiResult,
    LlmRequestOptions,
    MediaOutputType,
)
from agent_bot.multimodal import InvalidImageAttachmentError, fetch_image_data_urls
from agent_bot.services.request_deadline import remaining_timeout, request_deadline
from agent_bot.services.tool_images import ToolImageCollection, record_tool_images

_MAX_AGENT_EVENT_CHARS = 2_000_000
_MAX_AGENT_REPLY_CHARS = 1_000_000
_MAX_AUTO_SPEAK_DECISION_RESPONSE_BYTES = 16_384
_HTTP_URL_REQUEST = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_MEDIA_ERROR_CODES = frozenset(
    {
        "MEDIA_MODEL_NOT_FOUND",
        "MEDIA_PROTOCOL_UNSUPPORTED",
        "MEDIA_RESPONSE_INVALID",
        "MEDIA_TOO_LARGE",
        "MEDIA_UPSTREAM_UNAVAILABLE",
    }
)
_IMAGE_MIME_TYPES = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}
_AUDIO_MIME_TYPES = {
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "pcm": "audio/pcm",
}


class DispatchAiError(RuntimeError):
    """dispatch-ai 调用错误基类。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
    ) -> None:
        """保留可安全分类的状态，避免上层解析异常文本。"""

        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class DispatchAiUnavailableError(DispatchAiError):
    """网络、超时或上游服务不可用。"""


class DispatchAiRejectedError(DispatchAiError):
    """上游明确拒绝请求。"""


class DispatchAiProtocolError(DispatchAiError):
    """上游响应不符合约定。"""


class DispatchAiImageError(DispatchAiError):
    """QQ 图片无法安全下载或转换为多模态输入。"""


class DispatchAiClient:
    """调用现有 Solon AI 服务，不自动重试有副作用的请求。"""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        token: str | None = None,
        request_options: LlmRequestOptions | None = None,
        agent_max_steps: int | None = None,
        web_search_max_steps: int = 3,
        mcp_services: Sequence[str] = (),
        laya_enabled: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """保存请求预算及 MCP 选择；服务端地址和凭据始终由 Java 端管理。"""
        self._timeout_seconds = min(timeout_seconds, 300.0)
        self._token = token
        self._request_options = request_options or LlmRequestOptions()
        self._agent_max_steps = agent_max_steps
        self._web_search_max_steps = web_search_max_steps
        self._mcp_services = tuple(mcp_services)
        self._laya_enabled = laya_enabled
        self._http = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/",
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            follow_redirects=False,
        )

    async def decide_auto_speak(
        self,
        *,
        state: str,
        session_id: str,
        request_id: str,
    ) -> AutoSpeakDecisionResponse:
        """在共享总预算内请求服务端拥有的固定 Laya 决策。"""
        try:
            with request_deadline(self._timeout_seconds):
                timeout = remaining_timeout(self._timeout_seconds)
                return await asyncio.wait_for(
                    self._decide_auto_speak(
                        state=state,
                        session_id=session_id,
                        request_id=request_id,
                    ),
                    timeout=timeout,
                )
        except asyncio.TimeoutError as exc:
            raise DispatchAiUnavailableError("dispatch-ai request timed out") from exc

    async def _decide_auto_speak(
        self,
        *,
        state: str,
        session_id: str,
        request_id: str,
    ) -> AutoSpeakDecisionResponse:
        """发送严格决策契约，并按现有异常层次解析响应。"""
        request_data = AutoSpeakDecisionRequest(
            state=state,
            sessionId=session_id,
            requestsId=request_id,
            requestTimeoutMillis=max(
                1,
                int(remaining_timeout(self._timeout_seconds) * 1000),
            ),
        )
        headers = {
            "User-Agent": "dispatch-chat/auto-speak",
            "X-Request-ID": request_id,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            response = await self._http.post(
                "chat/autoSpeakDecision",
                json=request_data.model_dump(by_alias=True, mode="json"),
                headers=headers,
                timeout=httpx.Timeout(remaining_timeout(self._timeout_seconds)),
            )
        except httpx.TimeoutException as exc:
            raise DispatchAiUnavailableError("dispatch-ai request timed out") from exc
        except httpx.RequestError as exc:
            raise DispatchAiUnavailableError("dispatch-ai is unavailable") from exc

        if response.status_code >= 500 or response.status_code == 429:
            raise DispatchAiUnavailableError(
                f"dispatch-ai returned HTTP {response.status_code}",
                status_code=response.status_code,
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise DispatchAiRejectedError(
                f"dispatch-ai returned HTTP {response.status_code}",
                status_code=response.status_code,
            )
        if len(response.content) > _MAX_AUTO_SPEAK_DECISION_RESPONSE_BYTES:
            raise DispatchAiProtocolError("dispatch-ai returned an invalid decision response")
        try:
            result = AutoSpeakDecisionResult.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise DispatchAiProtocolError(
                "dispatch-ai returned an invalid decision response"
            ) from exc
        if result.code != 200:
            raise DispatchAiRejectedError(
                f"dispatch-ai rejected request with code {result.code}"
            )
        if result.data is None:
            raise DispatchAiProtocolError("dispatch-ai returned an empty decision")
        return result.data

    async def generate_media(
        self,
        *,
        model: str,
        session_id: str,
        prompt: str | None,
        messages: Sequence[ChatMessage],
        request_id: str,
        output_type: MediaOutputType,
        timeout_seconds: float,
        max_bytes: int,
        image_format: str | None,
        voice: str | None,
        audio_format: str | None,
        user: str,
    ) -> ChatMediaResponse:
        """在图片下载、HTTP 请求和完整响应读取期间共享同一总预算。"""
        try:
            with request_deadline(min(self._timeout_seconds, timeout_seconds)):
                timeout = remaining_timeout(min(self._timeout_seconds, timeout_seconds))
                return await asyncio.wait_for(
                    self._generate_media(
                        model=model,
                        session_id=session_id,
                        prompt=prompt,
                        messages=messages,
                        request_id=request_id,
                        output_type=output_type,
                        timeout_seconds=timeout_seconds,
                        max_bytes=max_bytes,
                        image_format=image_format,
                        voice=voice,
                        audio_format=audio_format,
                        user=user,
                    ),
                    timeout=timeout,
                )
        except asyncio.TimeoutError as exc:
            raise DispatchAiUnavailableError("dispatch-ai request timed out") from exc

    async def _generate_media(
        self,
        *,
        model: str,
        session_id: str,
        prompt: str | None,
        messages: Sequence[ChatMessage],
        request_id: str,
        output_type: MediaOutputType,
        timeout_seconds: float,
        max_bytes: int,
        image_format: str | None,
        voice: str | None,
        audio_format: str | None,
        user: str,
    ) -> ChatMediaResponse:
        """发送类型化媒体请求，并校验完整响应的格式与大小。"""
        request_data = ChatMediaRequest(
            requestTimeoutMillis=max(1, int(remaining_timeout(self._timeout_seconds) * 1000)),
            requestId=request_id,
            sessionId=session_id,
            user=user,
            model=model,
            outputType=output_type,
            prompt=prompt,
            messages=tuple(messages),
            timeoutSeconds=max(1, int(timeout_seconds)),
            maxBytes=max_bytes,
            imageFormat=image_format,
            voice=voice,
            audioFormat=audio_format,
        )
        headers = {
            "User-Agent": f"dispatch-chat/{session_id}",
            "X-Request-ID": request_id,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            response = await self._http.post(
                "chat/mediaOne",
                json=request_data.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
                headers=headers,
                timeout=httpx.Timeout(remaining_timeout(timeout_seconds)),
            )
        except httpx.TimeoutException as exc:
            raise DispatchAiUnavailableError("dispatch-ai media request timed out") from exc
        except httpx.RequestError as exc:
            raise DispatchAiUnavailableError("dispatch-ai is unavailable") from exc

        try:
            result = ChatMediaResult.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            if response.status_code >= 500 or response.status_code == 429:
                raise DispatchAiUnavailableError(
                    f"dispatch-ai returned HTTP {response.status_code}",
                    status_code=response.status_code,
                ) from exc
            if response.status_code < 200 or response.status_code >= 300:
                raise DispatchAiRejectedError(
                    f"dispatch-ai returned HTTP {response.status_code}",
                    status_code=response.status_code,
                ) from exc
            raise DispatchAiProtocolError(
                "dispatch-ai returned an invalid media response"
            ) from exc

        if result.code != 200:
            if result.description not in _MEDIA_ERROR_CODES:
                if response.status_code >= 500 or response.status_code == 429:
                    raise DispatchAiUnavailableError(
                        f"dispatch-ai returned HTTP {response.status_code}",
                        status_code=response.status_code,
                    )
                if response.status_code < 200 or response.status_code >= 300:
                    raise DispatchAiRejectedError(
                        f"dispatch-ai returned HTTP {response.status_code}",
                        status_code=response.status_code,
                    )
                raise DispatchAiProtocolError("dispatch-ai returned an unknown media error")
            if result.description == "MEDIA_UPSTREAM_UNAVAILABLE":
                raise DispatchAiUnavailableError(
                    "dispatch-ai media service is unavailable",
                    status_code=response.status_code,
                    error_code=result.description,
                )
            raise DispatchAiRejectedError(
                f"dispatch-ai rejected media request with {result.description}",
                status_code=response.status_code,
                error_code=result.description,
            )
        if response.status_code < 200 or response.status_code >= 300 or result.data is None:
            raise DispatchAiProtocolError("dispatch-ai returned an invalid media response")

        _validate_media_response(
            result.data,
            output_type=output_type,
            max_bytes=max_bytes,
            image_format=image_format,
            audio_format=audio_format,
        )
        return result.data

    async def complete(
        self,
        *,
        model: str,
        session_id: str,
        prompt: str,
        messages: Sequence[ChatMessage],
        request_id: str,
        user: str | None = None,
        chat_context: ChatContext | None = None,
        image_urls: Sequence[str] = (),
        video_urls: Sequence[str] = (),
        use_agent: bool = True,
        use_web_search: bool = True,
        request_options: LlmRequestOptions | None = None,
        chat_tools: Sequence[ChatToolName] = (),
        tool_session_token: str | None = None,
        laya_decision: bool = False,
    ) -> str:
        """在图片下载、HTTP 请求和完整响应读取期间共享同一总预算。"""
        try:
            with request_deadline(self._timeout_seconds):
                timeout = remaining_timeout(self._timeout_seconds)
                return await asyncio.wait_for(
                    self._complete(
                        model=model,
                        session_id=session_id,
                        prompt=prompt,
                        messages=messages,
                        request_id=request_id,
                        user=user,
                        chat_context=chat_context,
                        image_urls=image_urls,
                        video_urls=video_urls,
                        use_agent=use_agent,
                        use_web_search=use_web_search,
                        request_options=request_options,
                        chat_tools=chat_tools,
                        tool_session_token=tool_session_token,
                        laya_decision=laya_decision,
                    ),
                    timeout=timeout,
                )
        except asyncio.TimeoutError as exc:
            raise DispatchAiUnavailableError("dispatch-ai request timed out") from exc

    async def _complete(
        self,
        *,
        model: str,
        session_id: str,
        prompt: str,
        messages: Sequence[ChatMessage],
        request_id: str,
        user: str | None = None,
        chat_context: ChatContext | None = None,
        image_urls: Sequence[str] = (),
        video_urls: Sequence[str] = (),
        use_agent: bool = True,
        use_web_search: bool = True,
        request_options: LlmRequestOptions | None = None,
        chat_tools: Sequence[ChatToolName] = (),
        tool_session_token: str | None = None,
        laya_decision: bool = False,
    ) -> str:
        """在已绑定的预算内准备图片与消息，并消费普通响应或 Agent 流。"""
        if bool(chat_tools or laya_decision) != bool(tool_session_token):
            raise ValueError("chat tools and tool session token must be provided together")
        if laya_decision and (not self._laya_enabled or chat_tools):
            raise ValueError("Laya decision requires enabled Laya without other chat tools")
        try:
            image_data_urls = await fetch_image_data_urls(self._http, image_urls)
        except InvalidImageAttachmentError as exc:
            raise DispatchAiImageError("image material is unavailable") from exc

        effective_options = request_options or self._request_options
        option_values = effective_options.model_dump(exclude_none=True)
        network_tools: tuple[AgentToolName, ...] = ("web_search",) if use_web_search else ()
        if use_web_search and _HTTP_URL_REQUEST.search(prompt):
            network_tools += ("read_url",)
        selected_tools = (
            tuple(chat_tools) + network_tools
            if chat_tools
            else (network_tools if use_agent and use_web_search else None)
        )
        if laya_decision:
            selected_tools = ()
        max_steps = self._agent_max_steps
        if max_steps is None:
            max_steps = (
                5
                if selected_tools and "local_workflow_generate_image" in selected_tools
                else self._web_search_max_steps
            )
        request_messages: list[ChatMessage] | None = list(messages)
        request_input: list[DispatchAiInputMessage] | None = None
        if image_data_urls:
            request_input = [
                DispatchAiInputMessage(
                    role=message.role,
                    history_request_id=message.history_request_id,
                    content=(
                        DispatchAiInputContent(type="input_text", text=message.content),
                    ),
                )
                for message in messages
            ]
            if not request_input:
                request_input.append(
                    DispatchAiInputMessage(
                        role="user",
                        content=(DispatchAiInputContent(type="input_text", text=prompt),),
                    )
                )
            current = request_input[-1]
            request_input[-1] = current.model_copy(
                update={
                    "content": (
                        *current.content,
                        *(
                            DispatchAiInputContent(
                                type="input_image",
                                image_url=image_data_url,
                            )
                            for image_data_url in image_data_urls
                        ),
                    )
                }
            )
            request_messages = None

        mcp_services = () if laya_decision else (
            self._mcp_services if use_agent or chat_tools else ()
        )
        if self._laya_enabled and (chat_tools or laya_decision):
            mcp_services = tuple(dict.fromkeys((*mcp_services, "laya")))
        request_data = DispatchAiRequest(
            requestTimeoutMillis=max(1, int(remaining_timeout(self._timeout_seconds) * 1000)),
            prompt=prompt,
            sessionId=session_id,
            model=model,
            requestsId=request_id,
            messages=request_messages,
            input=request_input,
            user=user,
            chatContext=chat_context,
            tools=selected_tools,
            mcpServices=mcp_services or None,
            layaDecision=True if laya_decision else None,
            toolSessionToken=tool_session_token,
            maxSteps=max_steps if selected_tools or mcp_services or laya_decision else None,
            videoUrls=tuple(video_urls) or None,
            **option_values,
        )
        headers = {
            "User-Agent": f"dispatch-chat/{session_id}",
            "X-Request-ID": request_id,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        if use_agent and not chat_tools and not laya_decision:
            return await self._complete_agent(request_data, headers)

        try:
            endpoint = (
                "chat/chatAgentOne"
                if chat_tools or laya_decision
                else "chat/chatOne"
            )
            response = await self._http.post(
                endpoint,
                json=request_data.model_dump(by_alias=True, exclude_none=True, mode="json"),
                headers=headers,
                timeout=httpx.Timeout(remaining_timeout(self._timeout_seconds)),
            )
        except httpx.TimeoutException as exc:
            raise DispatchAiUnavailableError("dispatch-ai request timed out") from exc
        except httpx.RequestError as exc:
            raise DispatchAiUnavailableError("dispatch-ai is unavailable") from exc

        if response.status_code >= 500 or response.status_code == 429:
            raise DispatchAiUnavailableError(
                f"dispatch-ai returned HTTP {response.status_code}",
                status_code=response.status_code,
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise DispatchAiRejectedError(
                f"dispatch-ai returned HTTP {response.status_code}",
                status_code=response.status_code,
            )

        try:
            result = DispatchAiResult.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise DispatchAiProtocolError("dispatch-ai returned an invalid response") from exc

        if result.code != 200:
            raise DispatchAiRejectedError(f"dispatch-ai rejected request with code {result.code}")
        if result.data is None or (
            not result.data.replay_context.strip() and not result.data.images
        ):
            raise DispatchAiProtocolError("dispatch-ai returned an empty reply")
        try:
            images = ToolImageCollection()
            images.add(result.data.images)
            record_tool_images(images.items)
        except ValueError as exc:
            raise DispatchAiProtocolError("dispatch-ai returned invalid tool images") from exc
        return result.data.replay_context

    async def _complete_agent(
        self,
        request_data: DispatchAiRequest,
        headers: dict[str, str],
    ) -> str:
        stream_headers = {**headers, "Accept": "text/event-stream"}
        result_parts: list[str] = []
        images = ToolImageCollection()
        result_chars = 0
        completed = False

        try:
            async with self._http.stream(
                "POST",
                "chat/agentChat",
                json=request_data.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
                headers=stream_headers,
                timeout=httpx.Timeout(remaining_timeout(self._timeout_seconds)),
            ) as response:
                if response.status_code >= 500 or response.status_code == 429:
                    raise DispatchAiUnavailableError(
                        f"dispatch-ai returned HTTP {response.status_code}",
                        status_code=response.status_code,
                    )
                if response.status_code < 200 or response.status_code >= 300:
                    raise DispatchAiRejectedError(
                        f"dispatch-ai returned HTTP {response.status_code}",
                        status_code=response.status_code,
                    )
                content_type = response.headers.get("Content-Type", "")
                if not content_type.casefold().startswith("text/event-stream"):
                    raise DispatchAiProtocolError(
                        "dispatch-ai returned an invalid agent stream"
                    )

                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line.removeprefix("data:").strip()
                    if not data:
                        raise DispatchAiProtocolError("dispatch-ai returned an invalid agent event")
                    if len(data) > 14_000_000:
                        raise DispatchAiProtocolError(
                            "dispatch-ai agent event is too large"
                        )
                    try:
                        chunk = AgentChatChunk.model_validate(json.loads(data))
                    except (ValueError, ValidationError) as exc:
                        raise DispatchAiProtocolError(
                            "dispatch-ai returned an invalid agent event"
                        ) from exc
                    if len(data) > _MAX_AGENT_EVENT_CHARS and any(
                        choice.delta.type != "images" for choice in chunk.choices
                    ):
                        raise DispatchAiProtocolError("dispatch-ai agent event is too large")
                    for choice in chunk.choices:
                        if choice.delta.type == "images":
                            try:
                                images.add(choice.delta.images)
                            except ValueError as exc:
                                raise DispatchAiProtocolError("invalid tool image total") from exc
                            continue
                        if choice.delta.type == "error":
                            raise DispatchAiUnavailableError(
                                "dispatch-ai agent stream failed"
                            )
                        if choice.delta.type == "done":
                            completed = True
                            continue
                        if choice.delta.type != "result" or choice.delta.content is None:
                            continue
                        result_chars += len(choice.delta.content)
                        if result_chars > _MAX_AGENT_REPLY_CHARS:
                            raise DispatchAiProtocolError(
                                "dispatch-ai agent reply is too large"
                            )
                        result_parts.append(choice.delta.content)
                    if completed:
                        break
        except httpx.TimeoutException as exc:
            raise DispatchAiUnavailableError("dispatch-ai request timed out") from exc
        except httpx.RequestError as exc:
            raise DispatchAiUnavailableError("dispatch-ai is unavailable") from exc

        reply = "".join(result_parts).strip()
        if not completed or (not reply and not images.items):
            raise DispatchAiProtocolError("dispatch-ai returned an incomplete agent reply")
        record_tool_images(images.items)
        return reply

    async def pending_jobs(
        self, bot_id: str, after_id: str | None = None,
    ) -> tuple[AiJobResult, ...]:
        """拉取尚未确认的任务结果，不在读取时消费结果。"""
        response = await self._job_request("GET", "job/results", bot_id, after_id=after_id)
        try:
            result = AiJobResultsResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise DispatchAiProtocolError("invalid AI job results") from exc
        if result.code != 200 or any(job.bot_id != bot_id for job in result.data):
            raise DispatchAiProtocolError("AI job results do not match the requested bot")
        return result.data

    async def acknowledge_job(self, bot_id: str, job_id: str) -> None:
        """只有本地投递流程完成后，才确认对应机器人拥有的任务。"""
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise ValueError("invalid AI job identifier")
        response = await self._job_request("POST", f"job/{job_id}/ack", bot_id)
        try:
            result = DispatchAiResult.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise DispatchAiProtocolError("invalid AI job acknowledgement") from exc
        if result.code != 200:
            raise DispatchAiRejectedError("AI job acknowledgement was rejected")

    async def _job_request(
        self, method: str, path: str, bot_id: str, *, after_id: str | None = None,
    ) -> httpx.Response:
        """使用固定服务地址和凭据访问有界的 Job 接口。"""
        if not re.fullmatch(r"[1-9][0-9]{4,19}", bot_id) or not self._token:
            raise ValueError("AI job bot identity and service token are required")
        params = {"botId": bot_id}
        if after_id is not None:
            if not re.fullmatch(r"[a-f0-9]{32}", after_id):
                raise ValueError("invalid AI job cursor")
            params["afterId"] = after_id
        try:
            response = await self._http.request(
                method, path, params=params,
                headers={"Authorization": f"Bearer {self._token}"}, timeout=10,
            )
        except httpx.RequestError as exc:
            raise DispatchAiUnavailableError("AI job service is unavailable") from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise DispatchAiUnavailableError("AI job service is unavailable")
        if not 200 <= response.status_code < 300:
            raise DispatchAiRejectedError("AI job request was rejected")
        if len(response.content) > 3_000_000:
            raise DispatchAiProtocolError("AI job response is too large")
        return response

    async def aclose(self) -> None:
        await self._http.aclose()


def _validate_media_response(
    response: ChatMediaResponse,
    *,
    output_type: MediaOutputType,
    max_bytes: int,
    image_format: str | None,
    audio_format: str | None,
) -> None:
    item = response.items[0]
    if item.type != output_type or item.data.startswith("data:"):
        raise DispatchAiProtocolError("dispatch-ai returned an invalid media item")
    max_encoded_chars = ((max_bytes + 2) // 3) * 4
    if len(item.data) > max_encoded_chars:
        raise DispatchAiProtocolError("dispatch-ai returned oversized media")
    try:
        raw = base64.b64decode(item.data, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise DispatchAiProtocolError("dispatch-ai returned invalid media Base64") from exc
    if not raw or len(raw) > max_bytes:
        raise DispatchAiProtocolError("dispatch-ai returned invalid media size")

    if output_type == "IMAGE":
        if image_format not in _IMAGE_MIME_TYPES:
            raise DispatchAiProtocolError("image media format is invalid")
        expected_mime = _IMAGE_MIME_TYPES[image_format]
        matches_signature = _matches_image_signature(raw, image_format)
    else:
        if audio_format not in _AUDIO_MIME_TYPES:
            raise DispatchAiProtocolError("audio media format is invalid")
        expected_mime = _AUDIO_MIME_TYPES[audio_format]
        matches_signature = _matches_audio_signature(raw, audio_format)
    if item.mime_type != expected_mime or not matches_signature:
        raise DispatchAiProtocolError("dispatch-ai media format does not match request")


def _matches_image_signature(raw: bytes, media_format: str) -> bool:
    if media_format == "png":
        return raw.startswith(b"\x89PNG\r\n\x1a\n")
    if media_format == "jpeg":
        return raw.startswith(b"\xff\xd8\xff")
    return len(raw) >= 12 and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"


def _matches_audio_signature(raw: bytes, media_format: str) -> bool:
    if media_format == "mp3":
        return raw.startswith(b"ID3") or (
            len(raw) >= 2 and raw[0] == 0xFF and raw[1] & 0xE0 == 0xE0
        )
    if media_format == "opus":
        return raw.startswith(b"OggS")
    if media_format == "aac":
        return len(raw) >= 2 and raw[0] == 0xFF and raw[1] & 0xF6 == 0xF0
    if media_format == "flac":
        return raw.startswith(b"fLaC")
    if media_format == "wav":
        return len(raw) >= 12 and raw.startswith(b"RIFF") and raw[8:12] == b"WAVE"
    return len(raw) % 2 == 0
