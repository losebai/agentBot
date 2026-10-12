import gzip

import httpx
import pytest

from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.public_http import PublicNetworkBackend, PublicPageReader


class FakeStream:
    closed = False

    def get_extra_info(self, name):
        return ("8.8.8.8", 443)

    async def aclose(self):
        self.closed = True


class FakeNetwork:
    def __init__(self):
        self.hosts = []

    async def connect_tcp(self, host, port, **kwargs):
        self.hosts.append(host)
        return FakeStream()


async def test_dns_rebinding_cannot_change_connected_ip():
    async def resolve(host, port):
        return ("8.8.8.8",)

    network = FakeNetwork()
    backend = PublicNetworkBackend(resolver=resolve, backend=network)
    await backend.connect_tcp("public.example", 443)
    assert network.hosts == ["8.8.8.8"]


@pytest.mark.parametrize(
    "addresses",
    [("127.0.0.1",), ("8.8.8.8", "192.168.1.1"), ("::ffff:127.0.0.1",), ("169.254.169.254",)],
)
async def test_private_dns_rejected(addresses):
    async def resolve(host, port):
        return addresses

    network = FakeNetwork()
    with pytest.raises(AgentError):
        await PublicNetworkBackend(resolver=resolve, backend=network).connect_tcp("example.com", 80)
    assert network.hosts == []


async def test_redirect_to_private_address_rejected():
    sent = []

    def handle(request):
        sent.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    reader = PublicPageReader(transport=httpx.MockTransport(handle))
    with pytest.raises(AgentError):
        await reader.read("https://example.com", remaining=lambda: 5)
    assert len(sent) == 1


async def test_decompressed_size_limit():
    data = gzip.compress(b"x" * 1048577)

    class Raw(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield data

    reader = PublicPageReader(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                headers={"content-type": "text/plain", "content-encoding": "gzip"},
                stream=Raw(),
            )
        )
    )
    with pytest.raises(AgentError) as error:
        await reader.read("https://example.com", remaining=lambda: 5)
    assert error.value.code == "AGENT_PAGE_TOO_LARGE"


async def test_html_removes_scripts_and_retains_text():
    reader = PublicPageReader(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<title>Title</title><script>bad()</script><p>Hello</p>",
            )
        )
    )
    page = await reader.read("https://example.com", remaining=lambda: 5)
    assert "Hello" in page.content and "bad()" not in page.content
