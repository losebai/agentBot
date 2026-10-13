"""Incremental UTF-8 framing with limits independent of HTTP chunk boundaries."""

import codecs
from collections.abc import AsyncIterable, AsyncIterator

from agent_bot.ai.errors import AiError


async def lines(chunks: AsyncIterable[bytes], maximum_bytes: int) -> AsyncIterator[str]:
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    pending = ""
    try:
        async for chunk in chunks:
            # Slice chunks too: one transport chunk must not allocate an unbounded frame.
            for offset in range(0, len(chunk), 8192):
                pending += decoder.decode(chunk[offset : offset + 8192])
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    if len(line.encode()) > maximum_bytes:
                        raise AiError("AI_RESPONSE_TOO_LARGE")
                    yield line.rstrip("\r")
                if len(pending.encode()) > maximum_bytes:
                    raise AiError("AI_RESPONSE_TOO_LARGE")
        pending += decoder.decode(b"", final=True)
    except UnicodeError as error:
        raise AiError("AI_RESPONSE_INVALID") from error
    if pending:
        yield pending.rstrip("\r")


async def parse_sse(chunks: AsyncIterable[bytes], *, maximum_event_bytes: int = 262144):
    data, size = [], 0
    async for line in lines(chunks, maximum_event_bytes):
        if line == "":
            if data:
                yield "\n".join(data)
            data, size = [], 0
            continue
        size += len(line.encode()) + 1
        if size > maximum_event_bytes:
            raise AiError("AI_RESPONSE_TOO_LARGE")
        if line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
    if data:
        raise AiError("AI_RESPONSE_INVALID")


async def parse_ndjson(chunks: AsyncIterable[bytes], *, maximum_line_bytes: int = 262144):
    async for line in lines(chunks, maximum_line_bytes):
        if line.strip():
            yield line
