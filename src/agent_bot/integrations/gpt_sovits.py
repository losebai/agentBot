"""受限 GPT-SoVITS HTTP 客户端。"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict

from agent_bot.database_pool import finish_database_write


class _TtsRequest(BaseModel):
    """发送给 GPT-SoVITS `/tts` 的固定请求。"""

    model_config = ConfigDict(frozen=True)

    text: str
    text_lang: str
    ref_audio_path: str
    prompt_text: str
    prompt_lang: str
    speed_factor: float
    media_type: str = "wav"
    streaming_mode: bool = False


@dataclass(frozen=True, slots=True)
class GeneratedSpeech:
    """已经校验并写入本地目录的语音文件。"""

    path: Path
    media_type: str
    bytes: int


class GptSoVitsError(RuntimeError):
    """可安全返回给 MCP 调用方的稳定语音错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        """保存错误码和可选 HTTP 状态，不保存上游响应正文。"""

        super().__init__(message)
        self.code = code
        self.status_code = status_code


class GptSoVitsClient:
    """调用固定 GPT-SoVITS 音色并有界保存 WAV。"""

    def __init__(
        self,
        *,
        url: str,
        reference_audio_path: str,
        reference_text: str,
        reference_language: str,
        text_language: str,
        output_directory: str | Path,
        speed_factor: float,
        timeout_seconds: float,
        max_input_chars: int,
        max_audio_bytes: int,
        max_concurrent_requests: int = 1,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """使用数据库下发的固定地址、音色和资源上限创建客户端。"""

        parsed_url = urlsplit(url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError("GPT-SoVITS URL is invalid")
        if not reference_audio_path or len(reference_audio_path) > 2048:
            raise ValueError("GPT-SoVITS reference audio path is invalid")
        if not reference_text or len(reference_text) > 4000:
            raise ValueError("GPT-SoVITS reference text is invalid")
        if reference_language not in {"zh", "en", "ja", "yue", "ko"}:
            raise ValueError("GPT-SoVITS reference language is invalid")
        if text_language not in {"zh", "en", "ja", "yue", "ko"}:
            raise ValueError("GPT-SoVITS text language is invalid")
        if not 0.25 <= speed_factor <= 4:
            raise ValueError("GPT-SoVITS speed is invalid")
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("GPT-SoVITS timeout is invalid")
        if not 1 <= max_input_chars <= 4096:
            raise ValueError("GPT-SoVITS text limit is invalid")
        if not 1 <= max_audio_bytes <= 10_485_760:
            raise ValueError("GPT-SoVITS audio limit is invalid")
        if not 1 <= max_concurrent_requests <= 16:
            raise ValueError("GPT-SoVITS concurrency limit is invalid")
        self._semaphore = asyncio.Semaphore(max_concurrent_requests)
        self._url = url
        self._reference_audio_path = reference_audio_path
        self._reference_text = reference_text
        self._reference_language = reference_language
        self._text_language = text_language
        self._output_directory = Path(output_directory).expanduser().resolve()
        self._speed_factor = speed_factor
        self._max_input_chars = max_input_chars
        self._max_audio_bytes = max_audio_bytes
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )

    async def generate(self, text: str) -> GeneratedSpeech:
        """合成并校验 WAV，取消时等目录和文件线程结束，防止刷新提前释放资源。"""

        normalized = text.strip()
        if not normalized or len(normalized) > self._max_input_chars:
            raise GptSoVitsError("INVALID_ARGUMENT", "语音文本为空或超过限制")
        request = _TtsRequest(
            text=normalized,
            text_lang=self._text_language,
            ref_audio_path=self._reference_audio_path,
            prompt_text=self._reference_text,
            prompt_lang=self._reference_language,
            speed_factor=self._speed_factor,
        )
        async with self._semaphore:
            data = await self._request_audio(request)
        if not _is_wav(data):
            raise GptSoVitsError("INVALID_RESPONSE", "GPT-SoVITS 返回的音频格式无效")
        await finish_database_write(
            lambda: self._output_directory.mkdir(parents=True, exist_ok=True)
        )
        output_path = self._output_directory / f"tts-{secrets.token_hex(16)}.wav"
        await finish_database_write(lambda: output_path.write_bytes(data))
        return GeneratedSpeech(
            path=output_path,
            media_type="audio/wav",
            bytes=len(data),
        )

    async def _request_audio(self, request: _TtsRequest) -> bytes:
        """流式读取一次上游响应并强制执行数据库字节上限。"""

        try:
            async with self._http.stream(
                "POST",
                self._url,
                json=request.model_dump(),
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise GptSoVitsError(
                        "UPSTREAM_ERROR",
                        "GPT-SoVITS 拒绝了请求",
                        status_code=response.status_code,
                    )
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        if int(content_length) > self._max_audio_bytes:
                            raise GptSoVitsError("LIMIT_EXCEEDED", "生成音频超过限制")
                    except ValueError as error:
                        raise GptSoVitsError(
                            "INVALID_RESPONSE",
                            "GPT-SoVITS 返回的音频长度无效",
                        ) from error
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > self._max_audio_bytes:
                        raise GptSoVitsError("LIMIT_EXCEEDED", "生成音频超过限制")
                    chunks.append(chunk)
                return b"".join(chunks)
        except GptSoVitsError:
            raise
        except httpx.TimeoutException as error:
            raise GptSoVitsError("TIMEOUT", "GPT-SoVITS 请求超时") from error
        except httpx.RequestError as error:
            raise GptSoVitsError("UNAVAILABLE", "GPT-SoVITS 服务不可用") from error

    async def aclose(self) -> None:
        """关闭 HTTP 连接池。"""

        await self._http.aclose()


def _is_wav(data: bytes) -> bool:
    """核对 RIFF/WAVE 魔数。"""

    return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WAVE"
