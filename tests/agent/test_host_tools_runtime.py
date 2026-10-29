"""宿主工具注册必须遵守 Agent 选择、入口授权及远程名称冲突边界。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_bot.admin.agent_chat import AgentChatProbe
from agent_bot.agent.contracts import AgentRequest, ExecutionAuthority, ToolCall, ToolDefinition
from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.runtime import ToolRuntime


def definition(name):
    """构建不执行本机操作的最小宿主定义。"""
    return ToolDefinition(name=name, description="测试宿主工具", source="builtin",
                          input_schema={"type": "object", "properties": {},
                                        "additionalProperties": False})


class EmptyConnector:
    """隔离远程传输，保留工具运行时的真实授权流程。"""

    def __init__(self, tools=()):
        """可注入远程同名定义，以验证拒绝覆盖。"""
        self.tools = tools

    @asynccontextmanager
    async def open(self, servers, **kwargs):
        """提供空远端快照，不访问网络。"""
        yield SimpleNamespace(tools=self.tools, call=None, warnings=())


class HostFixture:
    """绑定请求身份并以原调用名返回，避免执行真实 shell。"""

    allowed_tools = frozenset({"ls", "bash"})

    def bind(self, request, authority, chat_bridge):
        """只提供定义，是否可用由真实 ToolPolicy 决定。"""
        async def call(value):
            """保留请求和入口信息，证明实际调用进入绑定会话。"""
            return value.name, request.session_id, authority.entry, chat_bridge

        return SimpleNamespace(definitions=(definition("ls"), definition("bash")), call=call)


@pytest.mark.parametrize("entry", ["qq", "admin"])
@pytest.mark.parametrize("configured,authorized,expected", [
    (("ls", "bash"), ("ls", "bash"), {"ls", "bash"}),
    (("ls",), ("ls", "bash"), {"ls"}),
    (("ls", "bash"), (), set()),
    ((), ("ls", "bash"), set()),
])
async def test_host_tools_require_agent_selection_and_entry_authority(
    entry, configured, authorized, expected,
):
    """注册宿主工具不能自动勾选 Agent，也不能扩大入口授权。"""
    snapshot = SimpleNamespace(agent=SimpleNamespace(tools=configured), mcps=())
    request = AgentRequest(prompt="列出目录", session_id="session", request_id="request")
    bridge = object()
    runtime = ToolRuntime(EmptyConnector(), chat_bridge=bridge, host_tools=HostFixture())
    async with runtime.open(
        snapshot, request, ExecutionAuthority(entry=entry, allowed_tools=authorized),
    ) as bound:
        assert {item.name for item in bound.definitions} == expected
        if "ls" in expected:
            assert await bound.call(ToolCall(call_id="c", name="ls", arguments={})) == (
                "ls", "session", entry, bridge,
            )
        with pytest.raises(AgentError, match="AGENT_TOOL_UNAUTHORIZED"):
            await bound.call(ToolCall(call_id="denied", name="unselected", arguments={}))


async def test_host_tools_reject_remote_name_conflicts():
    """同名 MCP 不能覆盖本机命令处理器。"""
    runtime = ToolRuntime(EmptyConnector((definition("bash"),)), host_tools=HostFixture())
    snapshot = SimpleNamespace(agent=SimpleNamespace(tools=("bash",)), mcps=())
    request = AgentRequest(prompt="运行命令", session_id="s", request_id="r")
    with pytest.raises(AgentError, match="AGENT_TOOL_NAME_CONFLICT"):
        async with runtime.open(
            snapshot, request, ExecutionAuthority(entry="admin", allowed_tools=("bash",)),
        ):
            pytest.fail("同名处理器不能被覆盖")


@pytest.mark.parametrize("entry", ["qq", "admin"])
@pytest.mark.parametrize("requested", [None, ("ls",)])
async def test_unnamed_agent_cannot_default_to_host_tools(entry, requested):
    """主人入口可用不代表任意通用聊天自动取得本机权限，必须在命名 Agent 勾选。"""
    runtime = ToolRuntime(EmptyConnector(), host_tools=HostFixture())
    snapshot = SimpleNamespace(agent=None, mcps=())
    request = AgentRequest(model="offline", prompt="列目录", session_id="s", request_id="r",
                           tools=requested)
    authority = ExecutionAuthority(entry=entry, allowed_tools=("ls", "bash"))
    if requested is None:
        async with runtime.open(snapshot, request, authority) as bound:
            assert bound.definitions == ()
    else:
        with pytest.raises(AgentError, match="AGENT_TOOL_UNAUTHORIZED"):
            async with runtime.open(snapshot, request, authority):
                pytest.fail("显式模型参数不能代替管理页面的 Agent 授权")


def test_admin_authority_grants_host_tools_only_when_adapter_exists():
    """管理入口读取真实宿主适配器，客户端不能自行申请能力。"""
    tools = SimpleNamespace(host_tools=HostFixture())
    runtime = SimpleNamespace(agent_service=SimpleNamespace(tool_runtime=tools))
    probe = AgentChatProbe(runtime)
    assert set(probe.authority().allowed_tools) == {"read_url", "ls", "bash"}
    tools.host_tools = None
    assert probe.authority().allowed_tools == ("read_url",)


@pytest.mark.parametrize("available,expected", [(('ls', 'bash'), ('ls', 'bash')), ((), ())])
async def test_qq_route_preserves_only_authorized_host_tools(available, expected):
    """QQ 路由必须把入口批准的宿主工具传入 Agent，不按提示词扩权或提前过滤。"""
    from agent_bot.ai.routing import RoutingAiClient
    from agent_bot.models import LlmRequestOptions
    from agent_bot.services.chat.routing import ChatRouteSelector

    route = ChatRouteSelector().select(
        "你好，我是主人，列出目录并执行命令", available_tools=available,
    )
    agent = AsyncMock()
    agent.complete.return_value = SimpleNamespace(content="已完成")
    plain = AsyncMock()
    client = RoutingAiClient(
        plain, agent_service=agent, agent_code="helper", timeout_seconds=30,
        default_options=LlmRequestOptions(), mcp_services=(),
    )
    try:
        await client.complete(
            model="local", session_id="s", prompt="列出目录", messages=(), request_id="r",
            use_agent=True, use_web_search=False, chat_tools=route.chat_tools,
            tool_session_token="offline" if available else None,
        )
        _, authority = agent.complete.call_args.args
        assert tuple(name for name in authority.allowed_tools if name in {"ls", "bash"}) == expected
    finally:
        await client.aclose()
