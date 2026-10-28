"""验证 SDK 不得放宽流完整性、响应大小及工具参数的原始约束。"""

import gzip
import json
import logging
from contextlib import aclosing

import httpx
import pytest
from pydantic import SecretStr

from agent.test_model_protocols import body, model_call, streamed
from agent_bot.ai.errors import AiError
from agent_bot.ai.providers.langchain import LangChainProvider
from ai.test_providers import ByteStream


@pytest.mark.parametrize("failure,event", [
    ("missing_terminal", "other"),
    ("upstream_failed", "response.failed"),
    ("invalid_json", "none"),
])
async def test_invalid_response_logs_location_without_upstream_content(
    caplog: pytest.LogCaptureFixture, failure: str, event: str,
) -> None:
    """不同协议失败应能定位校验位置，诊断不得记录响应正文、请求内容或凭据。"""
    protocol = "openai-responses"
    secret = "PRIVATE_UPSTREAM_TEXT_AND_CREDENTIAL"
    if failure == "missing_terminal":
        raw = streamed(protocol, body(protocol)).split(b'data: {"type": "response.completed"')[0]
    elif failure == "upstream_failed":
        raw = ("data: " + json.dumps({
            "type": "response.failed", "response": {"error": {"message": secret}},
        }) + "\n\n").encode()
    else:
        raw = ('data: {"private": "' + secret + '\n\n').encode()
    upstream = ByteStream(raw)
    provider = LangChainProvider(protocol, transport=httpx.MockTransport(
        lambda _: httpx.Response(200, stream=upstream),
    ))
    caplog.set_level(logging.WARNING, logger="agent_bot.ai.providers.langchain")
    try:
        with pytest.raises(AiError, match="AI_RESPONSE_INVALID"):
            call = model_call(protocol).model_copy(update={"credential": SecretStr(secret)})
            _ = [item async for item in provider.stream_turn(call)]
        assert upstream.closed
        diagnostics = [
            record.getMessage() for record in caplog.records
            if record.name == "agent_bot.ai.providers.langchain"
        ]
        assert len(diagnostics) == 1
        diagnostic = diagnostics[0]
        assert "protocol=openai-responses" in diagnostic
        assert "stage=model_execute" in diagnostic
        assert "code=AI_RESPONSE_INVALID" in diagnostic
        assert "upstream_status=200" in diagnostic
        assert "terminal=False" in diagnostic
        assert f"last_event={event}" in diagnostic
        assert "source=" in diagnostic and "source=none" not in diagnostic
        assert secret not in caplog.text
    finally:
        await provider.aclose()


@pytest.mark.parametrize("protocol", ["openai-chat", "openai-responses", "ollama"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_compressed_response_decoded_once(protocol, streaming):
    """供应商合法压缩的正文经过边界解压后不能再次交给 SDK 解压。"""
    value = body(protocol)
    raw = streamed(protocol, value) if streaming else json.dumps(value).encode()
    compressed = gzip.compress(raw)
    provider = LangChainProvider(protocol, transport=httpx.MockTransport(
        lambda _: httpx.Response(200, stream=ByteStream(compressed), headers={
            "content-encoding": "gzip", "content-length": str(len(compressed)),
        })
    ))
    try:
        if streaming:
            events = [event async for event in provider.stream_turn(model_call(protocol))]
            result = events[-1].turn
        else:
            result = await provider.complete_turn(model_call(protocol))
        assert result.calls[0].arguments == {"query": "天气"}
    finally:
        await provider.aclose()


@pytest.mark.parametrize("mutation", ["after_finish", "choice_index"])
async def test_chat_stream_rejects_ambiguous_choices(mutation):
    """工具参数只能来自第零选择且结束之后不能再追加内容。"""
    raw = streamed("openai-chat", body("openai-chat"))
    if mutation == "choice_index":
        raw = raw.replace(b'"choices": [{"index": 0', b'"choices": [{"index": 1')
    else:
        extra = {"choices": [{"index": 0, "delta": {"content": "extra"}}]}
        raw = raw.replace(b"data: [DONE]", ("data: " + json.dumps(extra)
                          + "\n\ndata: [DONE]").encode())
    provider = LangChainProvider("openai-chat", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=raw)
    ))
    try:
        with pytest.raises(AiError, match="AI_RESPONSE_INVALID"):
            _ = [event async for event in provider.stream_turn(model_call("openai-chat"))]
    finally:
        await provider.aclose()


@pytest.mark.parametrize("protocol", ["openai-chat", "openai-responses", "ollama"])
async def test_missing_terminal_closes_stream(protocol):
    """截断上游响应不能被 SDK 的部分消息聚合当成成功。"""
    raw = streamed(protocol, body(protocol))
    if protocol == "openai-chat":
        raw = raw.replace(b"data: [DONE]\n\n", b"")
    elif protocol == "openai-responses":
        raw = raw.split(b'data: {"type": "response.completed"')[0]
    else:
        raw = raw.replace(b'"done": true', b'"done": false')
    upstream = ByteStream(raw)
    provider = LangChainProvider(
        protocol, transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=upstream))
    )
    try:
        with pytest.raises(AiError):
            async with aclosing(provider.stream_turn(model_call(protocol))) as events:
                _ = [event async for event in events]
        assert upstream.closed
    finally:
        await provider.aclose()


async def test_responses_final_payload_cannot_disagree_with_stream():
    """权威完成项和增量内容不一致时，不能执行可能被更改的工具。"""
    value = body("openai-responses")
    raw = streamed("openai-responses", value)
    final = {
        **value,
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "changed"}],
            }
        ],
    }
    raw = raw.replace(json.dumps(value).encode(), json.dumps(final).encode())
    provider = LangChainProvider(
        "openai-responses",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=raw)),
    )
    try:
        with pytest.raises(AiError):
            _ = [event async for event in provider.stream_turn(model_call("openai-responses"))]
    finally:
        await provider.aclose()


@pytest.mark.parametrize("streaming", [False, True])
async def test_response_bytes_are_bounded(streaming):
    """SDK 自身缓冲前拒绝超大响应，取消时也释放上游。"""
    raw = (
        b'data: {"padding":"' + b"x" * 262145 + b'"}\n\n'
        if streaming
        else b'{"padding":"' + b"x" * 1048576 + b'"}'
    )
    upstream = ByteStream(raw)
    provider = LangChainProvider(
        "openai-chat", transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=upstream))
    )
    try:
        with pytest.raises(AiError, match="AI_RESPONSE_TOO_LARGE"):
            if streaming:
                _ = [event async for event in provider.stream_turn(model_call("openai-chat"))]
            else:
                await provider.complete_turn(model_call("openai-chat"))
        assert upstream.closed
    finally:
        await provider.aclose()


async def test_duplicate_json_tool_keys_rejected():
    """SDK 容忍的重复参数键仍属于项目协议错误。"""
    value = body("openai-chat")
    value["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = '{"a":1,"a":2}'
    provider = LangChainProvider(
        "openai-chat", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=value))
    )
    try:
        with pytest.raises(AiError):
            await provider.complete_turn(model_call("openai-chat"))
    finally:
        await provider.aclose()
