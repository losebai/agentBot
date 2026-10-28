"""当前登录账号的视频分页和 QQ 到模型工具绑定的回归。"""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from tests.unit.test_bilibili import account, store  # noqa: F401

from agent_bot.agent.contracts import AgentRequest, ExecutionAuthority, ToolCall
from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.langchain import LangChainTools
from agent_bot.agent.tools.runtime import ToolRuntime
from agent_bot.plugins.bilibili.client import BilibiliClient
from agent_bot.plugins.bilibili.service import BilibiliService
from agent_bot.plugins.bilibili.tools import BilibiliTools
from agent_bot.services.chat.routing import ChatRouteSelector


@pytest.mark.parametrize('prompt', ['列出我的B站视频', '你好'])
def test_qq_route_preserves_all_authorized_bilibili_tools(prompt):
    """本地路由不能丢弃插件已授予的工具，是否调用交给模型判断。"""
    names = ('bilibili_login_status', 'bilibili_get_my_videos', 'bilibili_get_comments',
             'bilibili_reply_comment', 'bilibili_publish_video')
    assert ChatRouteSelector().select(prompt, available_tools=names).chat_tools == names
    assert ChatRouteSelector().select(prompt, available_tools=()).chat_tools == ()


def video_page():
    """使用创作中心真实字段形状；私有杂项字段不能进入模型结果。"""
    return {'page': {'pn': 2, 'ps': 2, 'count': 5}, 'arc_audits': [{
        'Archive': {'aid': 170001, 'bvid': 'BV17x411w7KC', 'title': '测试视频',
                    'state': 0, 'state_desc': '开放浏览', 'ctime': 1700000000,
                    'ptime': 1700000001, 'secret_extra': 'never-return-this'},
        'Videos': [{'filename': 'private-file'}],
    }]}


async def test_my_videos_uses_logged_in_account_and_bounded_page(store):  # noqa: F811
    """只读当前账号的已发布稿件，响应不暴露平台私有字段或 Cookie。"""
    from agent_bot.plugins.bilibili.contracts import MyVideosArguments

    await store.save_account(account())
    def handle(request):
        """检查固定创作中心端点与服务器保存的凭据。"""
        assert request.url.host == 'member.bilibili.com'
        assert request.url.path == '/x/web/archives'
        assert dict(request.url.params) == {'status': 'pubed', 'pn': '2', 'ps': '2'}
        assert 'SESSDATA=test-session' in request.headers['cookie']
        return httpx.Response(200, json={'code': 0, 'data': video_page()})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        service = BilibiliService(store, BilibiliClient(http))
        result = await service.my_videos(MyVideosArguments(page=2, page_size=2))
    assert result['uid'] == '12345'
    assert result['total'] == 5 and result['has_more']
    assert result['page'] == 2 and result['page_size'] == 2
    assert result['videos'][0] == {
        'aid': 170001, 'bvid': 'BV17x411w7KC', 'title': '测试视频',
        'state': 0, 'state_description': '开放浏览', 'published_at': 1700000001,
        'url': 'https://www.bilibili.com/video/BV17x411w7KC',
    }
    assert 'never-return-this' not in json.dumps(result)
    assert 'test-session' not in json.dumps(result)


async def test_my_videos_without_login_never_calls_upstream(store):  # noqa: F811
    """缺少账号时明确要求扫码，不能退化为查询任意公开用户。"""
    from agent_bot.plugins.bilibili.contracts import MyVideosArguments

    client = SimpleNamespace(my_videos=AsyncMock())
    with pytest.raises(AgentError, match='BILIBILI_NOT_LOGGED_IN'):
        await BilibiliService(store, client).my_videos(MyVideosArguments())
    client.my_videos.assert_not_awaited()


@pytest.mark.parametrize('data', [
    {'page': {'ps': 10, 'count': 0}, 'arc_audits': None},
    {'page': {'ps': 10, 'count': 0}},
])
async def test_empty_my_videos_is_valid_without_archive_rows(data):
    """未投稿账号返回空列表，不把空稿件集合识别成登录失败。"""
    from agent_bot.plugins.bilibili.contracts import MyVideosArguments

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={'code': 0, 'data': data},
    ))) as http:
        result = await BilibiliClient(http).my_videos(account(), MyVideosArguments())
    assert result['videos'] == [] and result['total'] == 0 and not result['has_more']


@pytest.mark.parametrize('data', [
    {'page': {'ps': 0, 'count': 1}, 'arc_audits': []},
    {'page': {'ps': 10, 'count': 1}},
    {'page': {'ps': 10, 'count': 1}, 'arc_audits': [{'Archive': {'aid': 1}}]},
])
async def test_invalid_archive_page_reports_protocol_error(data):
    """分页或稿件结构异常必须报错，不能伪造空账号列表。"""
    from agent_bot.plugins.bilibili.contracts import MyVideosArguments

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={'code': 0, 'data': data},
    ))) as http:
        with pytest.raises(AgentError, match='BILIBILI_INVALID_RESPONSE'):
            await BilibiliClient(http).my_videos(account(), MyVideosArguments())


class NoRemoteTools:
    """只隔离远程 MCP，保留真实本地授权、schema 和 LangChain 工具调用。"""

    @asynccontextmanager
    async def open(self, *_args, **_kwargs):
        """本用例不连接任何远程服务。"""
        yield SimpleNamespace(tools=(), warnings=())


async def test_qq_bilibili_schema_reaches_model_binding_and_executes_my_videos():
    """工具从 QQ 路由经真实 ToolRuntime 进入模型工具集合并可执行。"""
    names = ChatRouteSelector().select('我的视频', available_tools=(
        'bilibili_get_my_videos', 'bilibili_login_status',
    )).chat_tools
    service = SimpleNamespace(my_videos=AsyncMock(return_value={'videos': [], 'total': 0}))
    context = SimpleNamespace(user_id='100', allowed_tools=frozenset(names),
                              ensure_active=lambda: None)
    bridge = SimpleNamespace(context=AsyncMock(return_value=context))
    tools = BilibiliTools(lambda: service, lambda: {'100'})
    runtime = ToolRuntime(NoRemoteTools(), chat_bridge=bridge, bilibili_tools=tools)
    request = AgentRequest(prompt='我的视频', session_id='qq-offline', request_id='r')
    snapshot = SimpleNamespace(mcps=(), agent=SimpleNamespace(tools=('bilibili_get_my_videos',)))
    authority = ExecutionAuthority(entry='qq', allowed_tools=names)
    async with runtime.open(snapshot, request, authority) as bound:
        model_tools = LangChainTools(bound)
        assert set(model_tools.by_name) == {'bilibili_get_my_videos'}
        result = await model_tools.call(ToolCall(
            call_id='c', name='bilibili_get_my_videos', arguments={'page': 1, 'page_size': 10},
        ))
        assert json.loads(result.content) == {'videos': [], 'total': 0}
        with pytest.raises(AgentError, match='AGENT_TOOL_UNAUTHORIZED'):
            await model_tools.call(ToolCall(
                call_id='denied', name='bilibili_login_status', arguments={},
            ))
    assert service.my_videos.call_args.args[0].page == 1
    assert service.my_videos.call_args.args[0].page_size == 10


@pytest.mark.parametrize('args', [{'page': 0}, {'page_size': 21}, {'uid': '999'}, {'page': True}])
def test_my_video_arguments_reject_unbounded_pages_and_account_override(args):
    """分页和账号作用域必须在模型参数预检阶段收紧。"""
    from pydantic import ValidationError

    from agent_bot.plugins.bilibili.contracts import MyVideosArguments

    with pytest.raises(ValidationError):
        MyVideosArguments.model_validate(args)
