"""仅连接校验后的公开地址，限制解码大小并记录脱敏的网页失败原因。"""

import asyncio
import ipaddress
import logging
import socket
import ssl
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx
from httpcore._backends.anyio import AnyIOBackend

from agent_bot.agent.errors import AgentError

logger = logging.getLogger(__name__)


def public_address(value):
    try:
        address = ipaddress.ip_address(value)
        if getattr(address, "ipv4_mapped", None) is not None:
            address = address.ipv4_mapped
        return address.is_global and not address.is_multicast and not address.is_reserved
    except ValueError:
        return False


def safe_url(value, *, public=False):
    try:
        parts = urlsplit(value)
        if (
            len(value) > 2048
            or parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or any(ord(c) <= 32 for c in value)
        ):
            return None
        if parts.port is not None and not 1 <= parts.port <= 65535:
            return None
        if public:
            host = parts.hostname.rstrip(".").lower()
            if host == "localhost" or host.endswith((".localhost", ".local")) or "%" in host:
                return None
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                if not public_address(host):
                    return None
        return value
    except (ValueError, TypeError):
        return None


async def resolve_addresses(host, port):
    rows = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(dict.fromkeys(row[4][0] for row in rows))


class PublicNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, *, resolver=resolve_addresses, backend=None):
        self.resolver, self.backend = resolver, backend or AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        addresses = await asyncio.wait_for(self.resolver(host, port), timeout)
        if not addresses or any(not public_address(ip) for ip in addresses):
            raise AgentError("AGENT_URL_REJECTED", status_code=422)
        stream = await self.backend.connect_tcp(
            addresses[0],
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
        peer = stream.get_extra_info("server_addr")
        if (
            not peer
            or not public_address(peer[0])
            or ipaddress.ip_address(peer[0]) != ipaddress.ip_address(addresses[0])
        ):
            await stream.aclose()
            raise AgentError("AGENT_URL_REJECTED", status_code=422)
        return stream

    async def connect_unix_socket(self, *args, **kwargs):
        raise AgentError("AGENT_URL_REJECTED", status_code=422)

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


class CoreStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class PublicTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            network_backend=PublicNetworkBackend(),
            max_connections=8,
            max_keepalive_connections=0,
            retries=0,
        )

    async def handle_async_request(self, request):
        response = await self.pool.handle_async_request(
            httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=request.url.raw_scheme,
                    host=request.url.raw_host,
                    port=request.url.port,
                    target=request.url.raw_path,
                ),
                headers=request.headers.raw,
                content=request.stream,
                extensions=request.extensions,
            )
        )
        return httpx.Response(
            response.status,
            headers=response.headers,
            stream=CoreStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self):
        await self.pool.aclose()


class PageParser(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.hidden, self.in_title = base, 0, False
        self.parts, self.title, self.links = [], [], []
        self.link = None

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        if tag == "title":
            self.in_title = True
        if self.hidden:
            return
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")
        if tag == "a" and len(self.links) < 20:
            href = dict(attrs).get("href")
            url = safe_url(urljoin(self.base, href)) if href else None
            if url:
                self.link = {"text": "", "url": url}
                self.links.append(self.link)

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)
        if tag == "title":
            self.in_title = False
        if tag == "a":
            self.link = None

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)
            if self.in_title:
                self.title.append(data)
            if self.link is not None:
                self.link["text"] = (self.link["text"] + data)[:200]


@dataclass(frozen=True)
class PageText:
    url: str
    title: str
    content_type: str
    content: str
    links: tuple[dict, ...] = ()
    truncated: bool = False


class PublicPageReader:
    """读取公开网页，原始链接参数和响应正文不能进入失败日志。"""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """允许测试注入传输，正式请求使用经过地址校验的连接后端。"""
        self.transport = transport

    async def read(self, url: str, *, remaining: Callable[[], float]) -> PageText:
        """按剩余预算读取网页，仅记录目标主机、HTTP 状态或异常类型。"""
        try:
            async with httpx.AsyncClient(
                transport=self.transport or PublicTransport(),
                trust_env=False,
                follow_redirects=False,
                headers={"Accept-Encoding": "identity"},
            ) as client:
                for redirect in range(4):
                    if not safe_url(url, public=True):
                        raise AgentError("AGENT_URL_REJECTED", status_code=422)
                    async with client.stream("GET", url, timeout=remaining()) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            if redirect == 3 or "location" not in response.headers:
                                raise AgentError("AGENT_PAGE_REDIRECT_LIMIT")
                            url = urljoin(url, response.headers["location"])
                            continue
                        if not 200 <= response.status_code < 300:
                            logger.warning(
                                "公开网页读取失败：host=%s status=%s",
                                urlsplit(url).hostname, response.status_code,
                            )
                            raise AgentError("AGENT_PAGE_UNAVAILABLE", status_code=503)
                        content_type = (
                            response.headers.get("content-type", "").split(";")[0].lower()
                        )
                        if content_type not in {"text/plain", "text/html"}:
                            raise AgentError("AGENT_PAGE_TYPE_UNSUPPORTED")
                        encoding = response.headers.get("content-encoding", "identity").lower()
                        if encoding not in {"identity", "gzip", "deflate"}:
                            raise AgentError("AGENT_PAGE_TYPE_UNSUPPORTED")
                        decoder = (
                            zlib.decompressobj(31 if encoding == "gzip" else 15)
                            if encoding != "identity"
                            else None
                        )
                        raw_size, body = 0, bytearray()
                        # Mock responses may be pre-read; real responses always stream raw bytes.
                        if response.is_stream_consumed:
                            body.extend(response.content)
                        else:
                            async for chunk in response.aiter_raw():
                                remaining()
                                raw_size += len(chunk)
                                if raw_size > 1048576:
                                    raise AgentError("AGENT_PAGE_TOO_LARGE")
                                body.extend(
                                    decoder.decompress(chunk, 1048577 - len(body))
                                    if decoder
                                    else chunk
                                )
                                if len(body) > 1048576 or (decoder and decoder.unconsumed_tail):
                                    raise AgentError("AGENT_PAGE_TOO_LARGE")
                            if decoder and not decoder.eof:
                                raise AgentError("AGENT_PAGE_INVALID")
                        if len(body) > 1048576:
                            raise AgentError("AGENT_PAGE_TOO_LARGE")
                        text = bytes(body).decode(response.encoding or "utf-8", errors="replace")
                        if content_type == "text/plain":
                            return PageText(url, "", content_type, text)
                        parser = PageParser(url)
                        parser.feed(text)
                        return PageText(
                            url,
                            "".join(parser.title)[:300],
                            content_type,
                            "".join(parser.parts).strip(),
                            tuple(parser.links),
                        )
        except (
            httpx.HTTPError,
            httpcore.NetworkError,
            httpcore.TimeoutException,
            OSError,
            zlib.error,
        ) as error:
            logger.warning(
                "公开网页读取失败：host=%s error=%s",
                urlsplit(url).hostname, type(error).__name__,
            )
            raise AgentError("AGENT_PAGE_UNAVAILABLE", status_code=503) from error
