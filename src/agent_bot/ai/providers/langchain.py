"""用 LangChain 统一普通对话和 Agent，协议、权限与连接由项目显式管理。"""

from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager

import httpx
from langchain_core.messages import AIMessage, message_chunk_to_message
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI
from langsmith import tracing_context

from agent_bot.agent.contracts import AgentModelCall, ModelEvent, ModelTurn, TurnMessage
from agent_bot.ai.contracts import ProviderCall, ProviderDelta, ProviderProtocol, ProviderResult
from agent_bot.ai.errors import AiError
from agent_bot.ai.providers.langchain_http import (
    ModelTransport,
    WireState,
    map_model_error,
    strict_object,
)
from agent_bot.ai.providers.langchain_messages import to_langchain_messages, to_model_turn
from agent_bot.ai.providers.transport import ProviderTransport
from agent_bot.models import LlmRequestOptions
from agent_bot.services.request_deadline import remaining_timeout

OPTIONS = {
    "openai-chat": {
        "max_tokens": "max_completion_tokens",
        "temperature": "temperature",
        "top_p": "top_p",
        "presence_penalty": "presence_penalty",
        "frequency_penalty": "frequency_penalty",
        "stop": "stop",
    },
    "openai-responses": {
        "max_tokens": "max_output_tokens",
        "temperature": "temperature",
        "top_p": "top_p",
    },
    "ollama": {
        "max_tokens": "num_predict",
        "temperature": "temperature",
        "top_p": "top_p",
        "top_k": "top_k",
        "min_p": "min_p",
        "repetition_penalty": "repeat_penalty",
        "stop": "stop",
        "enable_thinking": "think",
    },
}


def reject_sync(request: httpx.Request) -> httpx.Response:
    """服务只执行异步请求，同步 SDK 客户端不得意外建立连接。"""
    raise RuntimeError("synchronous model transport is disabled")


class LangChainProvider:
    """每次请求创建模型快照，连接池由本运行时持有并明确关闭。"""

    def __init__(
        self,
        protocol: ProviderProtocol,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        provider_transport: ProviderTransport | None = None,
    ):
        """已有 ProviderTransport 注入只用于兼容现有调用边界。"""
        self.protocol = protocol
        self.transport = provider_transport or ProviderTransport(transport)
        self.options = OPTIONS[protocol]

    def validate_options(self, options: LlmRequestOptions) -> None:
        """按数据库显式协议校验参数，不把不支持的参数悄悄丢弃。"""
        if set(options.model_dump(exclude_none=True)) - self.options.keys():
            raise AiError("AI_OPTION_UNSUPPORTED", status_code=422)

    @asynccontextmanager
    async def model_session(self, call: AgentModelCall):
        """所有 SDK 仅借用受控传输，结束和取消均关闭当前响应。"""
        self.validate_options(call.options)
        timeout = remaining_timeout(call.timeout_seconds)
        credential = call.credential.get_secret_value() if call.credential is not None else None
        state = WireState(self.protocol, timeout, credential, call.resolved.provider.base_url)
        wire = ModelTransport(self.transport.http, state)
        sync = httpx.Client(transport=httpx.MockTransport(reject_sync), trust_env=False)
        http = httpx.AsyncClient(
            transport=wire, timeout=timeout, trust_env=False, follow_redirects=False
        )
        values = {
            self.options[key]: value
            for key, value in call.options.model_dump(exclude_none=True).items()
        }
        try:
            if self.protocol == "ollama":
                # Ollama 公开注入点只接收 HTTPX 参数；无连接的包装传输借用公共池。
                model = ChatOllama(
                    model=call.resolved.model.upstream_model,
                    base_url=call.resolved.provider.base_url,
                    validate_model_on_init=False,
                    reasoning=values.pop("think", None),
                    client_kwargs={
                        "timeout": timeout,
                        "trust_env": False,
                        "follow_redirects": False,
                    },
                    async_client_kwargs={"transport": wire},
                    sync_client_kwargs={"transport": httpx.MockTransport(reject_sync)},
                )
                invoke_options = {"options": values}
            else:
                model = ChatOpenAI(
                    model=call.resolved.model.upstream_model,
                    api_key=credential or "local-no-auth",
                    base_url=state.base_url,
                    organization="",
                    openai_proxy="",
                    max_retries=0,
                    http_client=sync,
                    http_async_client=http,
                    timeout=timeout,
                    use_responses_api=self.protocol == "openai-responses",
                    stream_usage=True,
                    output_version="responses/v1",
                )
                invoke_options = values
                if self.protocol == "openai-responses":
                    invoke_options.update(store=False, include=["reasoning.encrypted_content"])
                if call.final_only:
                    invoke_options["tool_choice"] = "none"
            if call.tools and not call.final_only:
                model = model.bind_tools(
                    [
                        {
                            "type": "function",
                            "function": {
                                "name": tool.name,
                                "description": tool.description,
                                "parameters": tool.input_schema,
                            },
                        }
                        for tool in call.tools
                    ]
                )
            with tracing_context(enabled=False):
                yield model, invoke_options, state
        except Exception as error:
            raise map_model_error(error) from error
        finally:
            await http.aclose()
            await wire.aclose()
            sync.close()  # noqa: ASYNC212 — 只关闭无网络的 MockTransport。

    async def complete_turn(self, call: AgentModelCall) -> ModelTurn:
        """一次完整模型调用使用 LangChain 异步接口，返回受控领域结果。"""
        async with self.model_session(call) as (model, options, state):
            if self.protocol == "ollama":
                options["stream"] = False
            message = await model.ainvoke(to_langchain_messages(call.messages), **options)
            return self.checked_message(message, call, state)

    def checked_message(
        self, message: AIMessage, call: AgentModelCall, state: WireState
    ) -> ModelTurn:
        """SDK 完成不代表上游协议完整，终止状态必须独立通过校验。"""
        if not state.terminal or state.finish_reason not in {"stop", "tool_calls"}:
            raise AiError("AI_RESPONSE_INVALID")
        if self.protocol == "ollama" and state.thinking:
            # SDK 在未显式启用 reasoning 时丢弃 thinking；沿用上游原行为并私有回填。
            message = message.model_copy(update={"additional_kwargs": {
                **message.additional_kwargs, "reasoning_content": state.thinking,
            }})
        turn = to_model_turn(message, call)
        if self.protocol == "openai-chat":
            if bool(turn.calls) != (state.finish_reason == "tool_calls"):
                raise AiError("AI_RESPONSE_INVALID")
        if self.protocol == "openai-responses":
            output = (state.last or {}).get("output")
            if not isinstance(output, list):
                raise AiError("AI_RESPONSE_INVALID")
            texts, calls = [], []
            for item in output:
                if item.get("type") == "message" and item.get("role") == "assistant":
                    for part in item.get("content", []):
                        if part.get("type") != "output_text":
                            raise AiError("AI_RESPONSE_INVALID")
                        texts.append(part["text"])
                elif item.get("type") == "function_call":
                    calls.append((item["call_id"], item["name"], strict_object(item["arguments"])))
                elif item.get("type") != "reasoning":
                    raise AiError("AI_RESPONSE_INVALID")
            if "".join(texts) != turn.content or calls != [
                (item.call_id, item.name, item.arguments) for item in turn.calls
            ]:
                raise AiError("AI_RESPONSE_INVALID")
        return turn

    async def stream_turn(self, call: AgentModelCall) -> AsyncIterator[ModelEvent]:
        """聚合 LangChain 消息块，文本逐块输出，工具参数完整后才能执行。"""
        async with self.model_session(call) as (model, options, state):
            aggregate = None
            async with aclosing(
                model.astream(to_langchain_messages(call.messages), **options)
            ) as stream:
                async for chunk in stream:
                    aggregate = chunk if aggregate is None else aggregate + chunk
                    text = str(chunk.text)
                    if text:
                        yield ModelEvent(kind="text_delta", delta=text)
            if aggregate is None:
                raise AiError("AI_RESPONSE_INVALID")
            message = message_chunk_to_message(aggregate)
            turn = self.checked_message(message, call, state)
            yield ModelEvent(kind="turn_end", turn=turn)

    def agent_call(self, call: ProviderCall) -> AgentModelCall:
        """普通文本复用模型层，但禁用任何工具调用能力。"""
        return AgentModelCall(
            resolved=call.resolved,
            credential=call.credential,
            messages=tuple(TurnMessage(role=m.role, text=m.content) for m in call.request.messages),
            options=call.request.options,
            timeout_seconds=call.request.timeout_seconds,
            final_only=True,
        )

    async def complete(self, call: ProviderCall) -> ProviderResult:
        """普通请求共享协议和资源管理，不进入 Agent 执行图。"""
        turn = await self.complete_turn(self.agent_call(call))
        return ProviderResult(content=turn.content, model=turn.model, usage=turn.usage)

    async def stream(self, call: ProviderCall) -> AsyncIterator[ProviderDelta]:
        """普通流事件映射到既有 API，关闭传递到模型流。"""
        async with aclosing(self.stream_turn(self.agent_call(call))) as stream:
            async for event in stream:
                if event.kind == "text_delta":
                    yield ProviderDelta(kind="content", content=event.delta or "")
                else:
                    yield ProviderDelta(kind="done", model=event.turn.model, usage=event.turn.usage)

    async def aclose(self) -> None:
        """当前运行时退出后关闭公共池，不能留给 SDK 析构。"""
        await self.transport.aclose()
