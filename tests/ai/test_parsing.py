import pytest

from agent_bot.ai.errors import AiError
from agent_bot.ai.providers.parsing import parse_ndjson, parse_sse


async def chunks(raw):
    for byte in raw:
        yield bytes([byte])


async def test_utf8_sse_split_and_multiline():
    raw = ":comment\r\ndata: 你好\r\ndata: 世界\r\n\r\n".encode()
    assert [item async for item in parse_sse(chunks(raw))] == ["你好\n世界"]
    assert [item async for item in parse_ndjson(chunks(b'\n{"done":true}'))] == ['{"done":true}']


@pytest.mark.parametrize(
    "raw",
    [b"data: unfinished", b"data: \xff\n\n", b"data: " + b"a" * 262145],
    ids=["unfinished", "invalid-utf8", "oversized"],
)
async def test_invalid_or_oversized_event(raw):
    async def bounded_chunks():
        for offset in range(0, len(raw), 8192):
            yield raw[offset : offset + 8192]

    with pytest.raises(AiError):
        _ = [item async for item in parse_sse(bounded_chunks())]
