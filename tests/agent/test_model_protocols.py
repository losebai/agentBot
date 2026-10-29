import json
from contextlib import aclosing

import httpx
import pytest

from agent_bot.agent.contracts import AgentModelCall, ToolDefinition, ToolResult, TurnMessage
from agent_bot.ai.errors import AiError
from agent_bot.ai.providers.agent_chat import AgentChatProvider
from agent_bot.ai.providers.agent_ollama import AgentOllamaProvider
from agent_bot.ai.providers.agent_responses import AgentResponsesProvider
from agent_bot.ai.providers.transport import ProviderTransport
from ai.test_providers import ByteStream
from ai.test_providers import call as text_call

PROVIDERS = {
    "openai-chat": AgentChatProvider,
    "openai-responses": AgentResponsesProvider,
    "ollama": AgentOllamaProvider,
}


def model_call(protocol, **patch):
    return AgentModelCall(
        resolved=text_call(protocol).resolved,
        messages=(TurnMessage(role="user", text="lookup"),),
        timeout_seconds=30,
        tools=(
            ToolDefinition(
                name="web_search",
                source="builtin",
                input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
            ),
        ),
        **patch,
    )


def body(protocol, tool=True):
    args = {"query": "天气"}
    text = "先查一下" if tool else "已查询到结果"
    fn = {"name": "web_search", "arguments": json.dumps(args)}
    if protocol == "openai-chat":
        msg = {"role": "assistant", "content": text}
        if tool:
            msg["tool_calls"] = [{"id": "c1", "type": "function", "function": fn}]
        return {
            "model": "Qwen/X",
            "choices": [{"message": msg, "finish_reason": "tool_calls" if tool else "stop"}],
        }
    if protocol == "openai-responses":
        output = [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ]
        if tool:
            output.append({"type": "function_call", "call_id": "c1", **fn})
        return {"model": "Qwen/X", "status": "completed", "output": output}
    msg = {"role": "assistant", "content": text, "thinking": "private-thought"}
    if tool:
        msg["tool_calls"] = [{"function": {"name": "web_search", "arguments": args}}]
    return {"model": "Qwen/X", "message": msg, "done": True}


def streamed(protocol, value):
    """流式测试使用同一终态正文，避免测试替身掩盖文本与终态不一致。"""
    if protocol == "ollama":
        return json.dumps(value).encode() + b"\n"
    if protocol == "openai-responses":
        events = [
            {"type": "response.output_text.delta",
             "delta": value["output"][0]["content"][0]["text"]},
        ]
        for index, item in enumerate(value["output"]):
            if item["type"] == "function_call":
                item = {**item, "id": "fc_1", "status": "completed"}
                events.extend([
                    {"type": "response.output_item.added", "output_index": index,
                     "item": {**item, "arguments": "", "status": "in_progress"}},
                    {"type": "response.function_call_arguments.delta", "output_index": index,
                     "item_id": "fc_1", "delta": item["arguments"]},
                    {"type": "response.output_item.done", "output_index": index, "item": item},
                ])
        events.append({"type": "response.completed", "response": value})
    else:
        message = value["choices"][0]["message"]
        events = [
            {
                "model": "Qwen/X",
                "choices": [
                    {"index": 0, "delta": {"content": message["content"]}, "finish_reason": None}
                ],
            }
        ]
        for tool in message.get("tool_calls", []):
            fn = tool["function"]
            for i, fragment in enumerate([fn["arguments"][:8], fn["arguments"][8:]]):
                delta = {"index": 0, "function": {"arguments": fragment}}
                if i == 0:
                    delta.update(id="c1", type="function")
                    delta["function"]["name"] = fn["name"]
                events.append(
                    {
                        "choices": [
                            {"index": 0, "delta": {"tool_calls": [delta]}, "finish_reason": None}
                        ]
                    }
                )
        events.append(
            {
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": value["choices"][0]["finish_reason"]}
                ]
            }
        )
    result = "".join("data: " + json.dumps(e) + "\n\n" for e in events)
    return (result + ("data: [DONE]\n\n" if protocol == "openai-chat" else "")).encode()


@pytest.mark.parametrize("protocol", PROVIDERS)
async def test_model_tool_roundtrip(protocol):
    sent = []

    def handle(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=body(protocol, len(sent) == 1))

    provider = PROVIDERS[protocol](ProviderTransport(httpx.MockTransport(handle)))
    request = model_call(protocol)
    try:
        turn = await provider.complete_turn(request)
        assert turn.calls[0].name == "web_search"
        followup = request.model_copy(
            update={
                "messages": (
                    *request.messages,
                    TurnMessage(
                        role="assistant",
                        text=turn.content,
                        tool_calls=turn.calls,
                        continuation=turn.continuation,
                    ),
                    TurnMessage(
                        role="tool",
                        tool_result=ToolResult(
                            call_id=turn.calls[0].call_id, name="web_search", content="sunny"
                        ),
                    ),
                )
            }
        )
        final = await provider.complete_turn(followup)
        assert final.content == "已查询到结果" and len(sent) == 2
        assert "sunny" in json.dumps(sent[1])
        assert "private-thought" not in turn.model_dump_json()
        if protocol == "ollama":
            assert sent[1]["messages"][1]["thinking"] == "private-thought"
    finally:
        await provider.aclose()


@pytest.mark.parametrize("protocol", PROVIDERS)
async def test_fragmented_arguments_and_stream_close(protocol):
    raw = ByteStream(streamed(protocol, body(protocol)))
    provider = PROVIDERS[protocol](
        ProviderTransport(httpx.MockTransport(lambda request: httpx.Response(200, stream=raw)))
    )
    try:
        async with aclosing(provider.stream_turn(model_call(protocol))) as stream:
            events = [e async for e in stream]
        assert events[-1].turn.calls[0].arguments == {"query": "天气"}
        assert all(e.turn is None for e in events[:-1])
        assert raw.closed
    finally:
        await provider.aclose()


@pytest.mark.parametrize("protocol", PROVIDERS)
async def test_final_only_never_accepts_tool_calls(protocol):
    provider = PROVIDERS[protocol](
        ProviderTransport(
            httpx.MockTransport(lambda request: httpx.Response(200, json=body(protocol)))
        )
    )
    try:
        with pytest.raises(AiError):
            await provider.complete_turn(model_call(protocol, final_only=True))
    finally:
        await provider.aclose()


@pytest.mark.parametrize("protocol", PROVIDERS)
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("final_only", [False, True])
async def test_dsml_pseudo_tool_call_is_invalid_instead_of_final_text(
    protocol, streaming, final_only,
):
    """原生协议中的 DSML 正文不能冒充真实调用，工具耗尽后的收尾轮也必须拒绝。"""
    marker = "\uff5c\uff5cDSML\uff5c\uff5c"
    text = (f'我会修改配置。\n<{marker} calls><{marker} invoke name="bot_config_set">'
            f'<{marker} parameter name="updates" string="false">'
            '{"chat_auto_speak_prompt":"规则"}'
            f'</{marker} parameter></{marker} invoke></{marker} calls>')
    value = body(protocol, tool=False)
    if protocol == "openai-chat":
        value["choices"][0]["message"]["content"] = text
    elif protocol == "openai-responses":
        value["output"][0]["content"][0]["text"] = text
    else:
        value["message"]["content"] = text
    raw = ByteStream(streamed(protocol, value)) if streaming else None
    provider = PROVIDERS[protocol](ProviderTransport(httpx.MockTransport(
        lambda _: httpx.Response(200, stream=raw) if streaming else httpx.Response(200, json=value),
    )))
    try:
        with pytest.raises(AiError, match="AGENT_MODEL_RESPONSE_INVALID"):
            if streaming:
                async with aclosing(provider.stream_turn(model_call(
                    protocol, final_only=final_only,
                ))) as events:
                    async for event in events:
                        assert event.kind != "turn_end"
            else:
                await provider.complete_turn(model_call(protocol, final_only=final_only))
        if raw is not None:
            assert raw.closed
    finally:
        await provider.aclose()


@pytest.mark.parametrize("protocol", PROVIDERS)
async def test_missing_terminal_rejected_and_connection_closed(protocol):
    data = streamed(protocol, body(protocol))
    if protocol == "openai-chat":
        data = data.replace(b"data: [DONE]\n\n", b"")
    elif protocol == "openai-responses":
        data = data.split(b"\n\n")[0] + b"\n\n"
    else:
        value = body(protocol)
        value["done"] = False
        data = json.dumps(value).encode() + b"\n"
    raw = ByteStream(data)
    provider = PROVIDERS[protocol](
        ProviderTransport(httpx.MockTransport(lambda request: httpx.Response(200, stream=raw)))
    )
    try:
        with pytest.raises(AiError):
            async with aclosing(provider.stream_turn(model_call(protocol))) as events:
                async for _ in events:
                    pass
        assert raw.closed
    finally:
        await provider.aclose()
