"""为模型 SDK 提供有界传输，认证只来自当前数据库快照。"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, NoReturn

import httpx

from agent_bot.ai.errors import AiError
from agent_bot.ai.providers.parsing import parse_ndjson, parse_sse
from agent_bot.services.request_deadline import remaining_timeout


def strict_object(text: str | bytes) -> dict:
    """拒绝重复键和非有限数值，防止 SDK 将歧义工具参数悄悄覆盖。"""

    def pairs(items: list[tuple[str, Any]]) -> dict:
        """同一个对象内只允许一个同名属性。"""
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def invalid_constant(value: str) -> NoReturn:
        """JSON 不允许 NaN 或 Infinity。"""
        raise ValueError("non-finite value")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)
        if not isinstance(value, dict):
            raise ValueError("object required")
        return value
    except (ValueError, TypeError, UnicodeError) as error:
        raise AiError("AI_RESPONSE_INVALID") from error


@dataclass
class WireState:
    """每次模型调用独立记录终止状态，不能在并发请求之间共享。"""

    protocol: str
    timeout: float
    credential: str | None
    base_url: str
    terminal: bool = False
    finish_reason: str | None = None
    last: dict | None = None
    response: httpx.Response | None = None
    tool_arguments: dict[int, str] = field(default_factory=dict)
    thinking: str = field(default="", repr=False)

    def inspect(self, value: dict, *, streaming: bool) -> None:
        """仅校验协议终态，消息和工具内容解析仍由 LangChain 完成。"""
        self.last = value
        if self.protocol == "ollama":
            if "error" in value:
                raise AiError("AI_UPSTREAM_REJECTED")
            self.thinking += value.get("message", {}).get("thinking") or ""
            if len(self.thinking.encode()) > 1048576:
                raise AiError("AI_RESPONSE_TOO_LARGE")
            if value.get("done") is True:
                self.terminal = True
                self.finish_reason = value.get("done_reason") or "stop"
        elif self.protocol == "openai-responses":
            kind = value.get("type")
            if kind in {"response.failed", "response.incomplete", "error"}:
                raise AiError("AI_RESPONSE_INVALID")
            result = value.get("response", {}) if streaming else value
            if not streaming or kind == "response.completed":
                if result.get("status") != "completed":
                    raise AiError("AI_RESPONSE_INVALID")
                self.terminal = True
                self.finish_reason = "stop"
                self.last = result
        else:
            choices = value.get("choices", [])
            if len(choices) > 1:
                raise AiError("AI_RESPONSE_INVALID")
            if choices:
                if choices[0].get("index", 0) != 0 or (streaming and self.finish_reason):
                    raise AiError("AI_RESPONSE_INVALID")
                reason = choices[0].get("finish_reason")
                if reason:
                    self.finish_reason = reason
                message = choices[0].get("delta" if streaming else "message", {})
                for item in message.get("tool_calls") or []:
                    arguments = item.get("function", {}).get("arguments", "")
                    if streaming:
                        index = item["index"]
                        if type(index) is not int or not 0 <= index < 8:
                            raise AiError("AI_RESPONSE_INVALID")
                        self.tool_arguments[index] = self.tool_arguments.get(index, "") + arguments
                        if len(self.tool_arguments[index].encode()) > 65536:
                            raise AiError("AI_RESPONSE_TOO_LARGE")
                    else:
                        strict_object(arguments)
            if not streaming:
                self.terminal = bool(choices and self.finish_reason)
        if not streaming and not self.terminal:
            raise AiError("AI_RESPONSE_INVALID")


class GuardedStream(httpx.AsyncByteStream):
    """在 SDK 读取之前检查单帧和总字节预算，关闭时归还原响应。"""

    def __init__(self, response: httpx.Response, state: WireState) -> None:
        """一个包装流只对应一个上游响应。"""
        self.response, self.state = response, state

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """流式转发完整帧，缺少明确终止信号视为协议失败。"""
        total = 0

        async def chunks() -> AsyncIterator[bytes]:
            """预算独立于 TCP 分块，避免单个大块绕过限额。"""
            nonlocal total
            async for chunk in self.response.aiter_bytes():
                remaining_timeout(self.state.timeout)
                total += len(chunk)
                if total > 4 * 1048576:
                    raise AiError("AI_RESPONSE_TOO_LARGE")
                yield chunk

        parser = parse_ndjson if self.state.protocol == "ollama" else parse_sse
        async for data in parser(chunks()):
            if data == "[DONE]" and self.state.protocol == "openai-chat":
                if self.state.finish_reason not in {"stop", "tool_calls"}:
                    raise AiError("AI_RESPONSE_INVALID")
                self.state.terminal = True
                for arguments in self.state.tool_arguments.values():
                    strict_object(arguments)
            else:
                self.state.inspect(strict_object(data), streaming=True)
            yield (
                data + "\n" if self.state.protocol == "ollama" else "data: " + data + "\n\n"
            ).encode()
        if not self.state.terminal:
            raise AiError("AI_RESPONSE_INVALID")

    async def aclose(self) -> None:
        """消费者取消后也必须释放原始连接。"""
        await self.response.aclose()


class ModelTransport(httpx.AsyncBaseTransport):
    """SDK 借用运行时拥有的 HTTP 池，不拥有或关闭公共池。"""

    def __init__(self, http: httpx.AsyncClient, state: WireState) -> None:
        """凭据和协议来自这一次调用的固定快照。"""
        self.http, self.state = http, state

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """覆盖 SDK 默认认证和地址拼接，拒绝自动重定向与失败重试。"""
        suffix = {
            "openai-chat": "/chat/completions",
            "openai-responses": "/responses",
            "ollama": "/api/chat",
        }[self.state.protocol]
        request.url = httpx.URL(self.state.base_url + suffix)
        # SDK 可从进程环境补入组织和项目；运行配置只以当前数据库快照为准。
        request.headers.pop("openai-organization", None)
        request.headers.pop("openai-project", None)
        if self.state.credential is None:
            request.headers.pop("authorization", None)
        else:
            request.headers["authorization"] = "Bearer " + self.state.credential
        payload = strict_object(request.content)
        response = await self.http.send(request, stream=True)
        self.state.response = response
        if not 200 <= response.status_code < 300:
            await response.aclose()
            status = response.status_code
            code = (
                "AI_RATE_LIMITED"
                if status == 429
                else "AI_UPSTREAM_AUTH_REJECTED"
                if status in {401, 403}
                else "AI_UPSTREAM_UNAVAILABLE"
                if status >= 500
                else "AI_UPSTREAM_REJECTED"
            )
            raise AiError(code, status_code=429 if status == 429 else 502)
        # aiter_bytes 已解压，流还会重新分帧，不能继续声明原编码和压缩长度。
        headers = response.headers.copy()
        headers.pop("content-encoding", None)
        headers.pop("content-length", None)
        if payload.get("stream"):
            return httpx.Response(
                response.status_code,
                headers=headers,
                stream=GuardedStream(response, self.state),
                request=request,
            )
        content = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                remaining_timeout(self.state.timeout)
                if len(content) + len(chunk) > 1048576:
                    raise AiError("AI_RESPONSE_TOO_LARGE")
                content.extend(chunk)
            self.state.inspect(strict_object(bytes(content)), streaming=False)
            return httpx.Response(
                response.status_code,
                headers=headers,
                content=bytes(content),
                request=request,
            )
        finally:
            await response.aclose()

    async def aclose(self) -> None:
        """只释放本次请求的响应，公共 HTTP 池由适配器统一关闭。"""
        if self.state.response is not None:
            await self.state.response.aclose()


def map_model_error(error: Exception) -> AiError:
    """SDK 包装异常沿 cause 找回项目错误，其他错误仅返回稳定分类。"""
    current = error
    while current is not None:
        if isinstance(current, AiError):
            return current
        if isinstance(current, (httpx.TimeoutException, TimeoutError)):
            return AiError("AI_TIMEOUT", status_code=504)
        if isinstance(current, httpx.RequestError):
            return AiError("AI_UPSTREAM_UNAVAILABLE", status_code=503)
        current = current.__cause__
    return AiError("AI_RESPONSE_INVALID")
