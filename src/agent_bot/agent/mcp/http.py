"""在 MCP SDK 下层限制源站和响应大小，为有界截图响应保留空间。"""

from urllib.parse import urljoin

import httpx2

from agent_bot.agent.errors import AgentError
from agent_bot.media.config import MAX_TOTAL_IMAGE_BYTES

# 图片使用 base64 编码；额外 1 MiB 留给 JSON 元数据，SSE 和普通 JSON 共用同一上限。
MAX_MCP_RESPONSE_BYTES = (MAX_TOTAL_IMAGE_BYTES + 2) // 3 * 4 + 1048576


def origin(url):
    return url.scheme, url.host, url.port or (443 if url.scheme == "https" else 80)


class BoundedStream(httpx2.AsyncByteStream):
    def __init__(self, response, endpoint, on_failure):
        self.response = response
        self.endpoint, self.on_failure = endpoint, on_failure

    async def __aiter__(self):
        sse = "text/event-stream" in self.response.headers.get("content-type", "")
        pending, size = b"", 0
        try:
            async for chunk in self.response.aiter_bytes():
                if sse:
                    pending += chunk
                    normalized = pending.replace(b"\r\n", b"\n")
                    frames = normalized.split(b"\n\n")
                    if any(len(frame) > MAX_MCP_RESPONSE_BYTES for frame in frames):
                        raise AgentError("AGENT_MCP_RESPONSE_TOO_LARGE")
                    for frame in frames[:-1]:
                        lines = frame.decode("utf-8").splitlines()
                        if any(line.strip() == "event: endpoint" for line in lines):
                            target = "\n".join(
                                line[5:].lstrip() for line in lines if line.startswith("data:")
                            )
                            destination = httpx2.URL(urljoin(self.endpoint, target))
                            if origin(destination) != origin(httpx2.URL(self.endpoint)):
                                raise AgentError("AGENT_MCP_ORIGIN_REJECTED", status_code=422)
                        yield frame + b"\n\n"
                    # Retain the original partial frame to handle CRLF across chunks.
                    if len(frames) > 1:
                        pending = frames[-1]
                else:
                    size += len(chunk)
                    if size > MAX_MCP_RESPONSE_BYTES:
                        raise AgentError("AGENT_MCP_RESPONSE_TOO_LARGE")
                    yield chunk
        except AgentError as error:
            self.on_failure(error)
            raise
        finally:
            await self.response.aclose()

    async def aclose(self):
        await self.response.aclose()


class McpTransport(httpx2.AsyncBaseTransport):
    def __init__(self, endpoint, on_failure):
        self.allowed_origin = origin(httpx2.URL(endpoint))
        self.endpoint, self.on_failure = endpoint, on_failure
        self.inner = httpx2.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        if origin(request.url) != self.allowed_origin:
            raise AgentError("AGENT_MCP_ORIGIN_REJECTED", status_code=422)
        response = await self.inner.handle_async_request(request)
        if response.status_code in {401, 403}:
            await response.aclose()
            raise AgentError("AGENT_MCP_AUTH_REJECTED", status_code=502)
        if response.status_code >= 400 and request.method == "POST":
            await response.aclose()
            raise AgentError("AGENT_MCP_PROTOCOL_ERROR", status_code=502)
        headers = [
            (k, v)
            for k, v in response.headers.raw
            if k.lower() not in {b"content-encoding", b"content-length"}
        ]
        return httpx2.Response(
            response.status_code,
            headers=headers,
            stream=BoundedStream(response, self.endpoint, self.on_failure),
            extensions=response.extensions,
        )

    async def aclose(self):
        await self.inner.aclose()


def client_factory(server, secret, timeout, on_failure):
    def create(headers=None, timeout=timeout, auth=None):
        actual = dict(headers or {})
        if secret is not None:
            actual["Authorization"] = f"Bearer {secret.get_secret_value()}"
        return httpx2.AsyncClient(
            headers=actual,
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
            transport=McpTransport(server.endpoint, on_failure),
        )

    return create
