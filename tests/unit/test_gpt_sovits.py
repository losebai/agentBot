from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from agent_bot.integrations.gpt_sovits import GptSoVitsClient, GptSoVitsError

WAV = b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + b"\x00" * 32


async def test_actual_tts_requests_stay_bounded_without_chat_media_slot(tmp_path: Path) -> None:
    """并发限制在真正的 TTS HTTP 边界生效，不依赖普通聊天占用媒体槽。"""

    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        """首个合成挂起，第二个合成必须在释放后才能请求上游。"""

        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return httpx.Response(200, content=WAV)

    client = GptSoVitsClient(
        url="http://127.0.0.1:9880/tts", reference_audio_path="reference.wav",
        reference_text="参考文本", reference_language="zh", text_language="zh",
        output_directory=tmp_path, speed_factor=1, timeout_seconds=30,
        max_input_chars=100, max_audio_bytes=1024, max_concurrent_requests=1,
        transport=httpx.MockTransport(handler),
    )
    first = asyncio.create_task(client.generate("第一句"))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(client.generate("第二句"))
    try:
        await asyncio.sleep(.01)
        assert calls == 1
        assert not second.done()
    finally:
        release.set()
        await asyncio.gather(first, second)
        await client.aclose()
    assert calls == 2


@pytest.mark.asyncio
async def test_generate_posts_bounded_config_and_writes_valid_wav(tmp_path: Path) -> None:
    """漏传参考音频、语言、语速或未落盘有效 WAV 时应失败。"""

    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=WAV, headers={"content-type": "audio/wav"})

    client = GptSoVitsClient(
        url="http://127.0.0.1:9880/tts",
        reference_audio_path="G:/models/hutao/reference.wav",
        reference_text="参考文本",
        reference_language="zh",
        text_language="zh",
        output_directory=tmp_path,
        speed_factor=1.1,
        timeout_seconds=30,
        max_input_chars=100,
        max_audio_bytes=1024,
        transport=httpx.MockTransport(handler),
    )

    result = await client.generate("你好，旅行者。")

    payload = json.loads(requests[0].content)
    assert str(requests[0].url) == "http://127.0.0.1:9880/tts"
    assert payload == {
        "text": "你好，旅行者。",
        "text_lang": "zh",
        "ref_audio_path": "G:/models/hutao/reference.wav",
        "prompt_text": "参考文本",
        "prompt_lang": "zh",
        "speed_factor": 1.1,
        "media_type": "wav",
        "streaming_mode": False,
    }
    assert result.media_type == "audio/wav"
    assert result.bytes == len(WAV)
    assert result.path.is_absolute()
    assert result.path.parent == await asyncio.to_thread(tmp_path.resolve)
    assert await asyncio.to_thread(result.path.read_bytes) == WAV
    await client.aclose()


@pytest.mark.asyncio
async def test_generate_rejects_text_over_database_limit(tmp_path: Path) -> None:
    """数据库文本上限失效并向上游发送请求时应失败。"""

    client = GptSoVitsClient(
        url="http://127.0.0.1:9880/tts",
        reference_audio_path="G:/models/hutao/reference.wav",
        reference_text="参考文本",
        reference_language="zh",
        text_language="zh",
        output_directory=tmp_path,
        speed_factor=1,
        timeout_seconds=30,
        max_input_chars=4,
        max_audio_bytes=1024,
        transport=httpx.MockTransport(
            lambda _request: pytest.fail("over-limit text reached upstream")
        ),
    )

    with pytest.raises(GptSoVitsError) as invalid:
        await client.generate("超过四个字符")

    assert invalid.value.code == "INVALID_ARGUMENT"
    await client.aclose()


@pytest.mark.asyncio
async def test_generate_rejects_oversized_or_invalid_audio(tmp_path: Path) -> None:
    """响应超过数据库字节上限或伪造 WAV 时应失败且不落盘。"""

    responses = iter(
        (
            httpx.Response(200, content=WAV * 30, headers={"content-type": "audio/wav"}),
            httpx.Response(200, content=b"not-wave", headers={"content-type": "audio/wav"}),
        )
    )
    client = GptSoVitsClient(
        url="http://127.0.0.1:9880/tts",
        reference_audio_path="G:/models/hutao/reference.wav",
        reference_text="参考文本",
        reference_language="zh",
        text_language="zh",
        output_directory=tmp_path,
        speed_factor=1,
        timeout_seconds=30,
        max_input_chars=100,
        max_audio_bytes=128,
        transport=httpx.MockTransport(lambda _request: next(responses)),
    )

    with pytest.raises(GptSoVitsError) as oversized:
        await client.generate("第一条")
    with pytest.raises(GptSoVitsError) as invalid:
        await client.generate("第二条")

    assert oversized.value.code == "LIMIT_EXCEEDED"
    assert invalid.value.code == "INVALID_RESPONSE"
    assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []
    await client.aclose()
