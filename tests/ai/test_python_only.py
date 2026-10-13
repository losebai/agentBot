"""验证默认调用不依赖已删除的外部 AI 服务。"""

import pytest

from agent_bot.ai.errors import AiError
from agent_bot.ai.routing import AiRouteRequest, select_route


@pytest.mark.parametrize(
    ("route_request", "capability"),
    [
        (AiRouteRequest(), "ordinary-text"),
        (AiRouteRequest(agent=True), "agent"),
        (AiRouteRequest(images=True), "media"),
    ],
)
def test_default_requests_use_local_services(route_request, capability):
    """默认文本、Agent 和识图均由本地服务处理。"""
    decision = select_route(route_request)
    assert (decision.backend, decision.capability) == ("python", capability)


@pytest.mark.parametrize(
    "route_request",
    [AiRouteRequest(videos=True), AiRouteRequest(chat_tools=("soloncode",))],
)
def test_removed_capabilities_fail_before_external_request(route_request):
    """未迁移能力明确拒绝，禁止转发至不存在的服务。"""
    with pytest.raises(AiError, match="AI_CAPABILITY_UNSUPPORTED"):
        select_route(route_request)
