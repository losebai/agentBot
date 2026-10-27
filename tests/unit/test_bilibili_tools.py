"""B 站工具的真实身份、媒体权限和发布协议测试。"""

import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from tests.unit.test_bilibili import account, store  # noqa: F401

from agent_bot.agent.contracts import ExecutionAuthority, ToolCall
from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.runtime import BoundTools
from agent_bot.agent.tools.schema import ToolSchema
from agent_bot.media.contracts import MediaOwner
from agent_bot.plugins.bilibili.client import BilibiliClient
from agent_bot.plugins.bilibili.contracts import PublishArguments
from agent_bot.plugins.bilibili.tools import BilibiliTools
from agent_bot.plugins.bilibili.upload import publish_video


def test_bilibili_schemas_are_bounded_and_parameters_preflighted():
    """本地格式约束保留在原参数模型，不能把 pattern 扩展给远端工具。"""
    tools = BilibiliTools(lambda: object(), lambda: set())
    session = tools.bind(SimpleNamespace(session_id='s', attachment_ids=()),
                         ExecutionAuthority(entry='admin'), None, None, None)
    for definition in session.definitions:
        ToolSchema.validate_definition(definition)
    bound = BoundTools(session.definitions,
                       {item.name: session.call for item in session.definitions})
    with pytest.raises(AgentError, match='AGENT_TOOL_ARGUMENTS_INVALID'):
        bound.validate(ToolCall(call_id='c', name='bilibili_publish_video', arguments={
            'video_asset_id': 'a' * 32, 'cover_asset_id': 'b' * 32, 'title': '视频',
            'tid': 1, 'tags': ['测试'], 'copyright': 2,
        }))


async def test_write_tool_rechecks_real_qq_sender_before_side_effect():
    """模型无法把普通群成员提升为超级用户。"""
    service = SimpleNamespace(write=AsyncMock())
    bridge = SimpleNamespace(context=AsyncMock(return_value=SimpleNamespace(
        user_id='200', allowed_tools=frozenset({'bilibili_reply_comment'}),
        ensure_active=lambda: None)))
    tools = BilibiliTools(lambda: service, lambda: {'100'})
    session = tools.bind(
        SimpleNamespace(session_id='qq-session', request_id='r', attachment_ids=()),
        ExecutionAuthority(entry='qq', allowed_tools=('bilibili_reply_comment',)),
        None, None, bridge)
    call = ToolCall(call_id='c', name='bilibili_reply_comment', arguments={
        'video_id': 'av170001', 'root': 1, 'parent': 1, 'message': '测试',
    })
    with pytest.raises(AgentError, match='AGENT_TOOL_UNAUTHORIZED'):
        await session.call(call)
    service.write.assert_not_awaited()
    bridge.context.return_value.user_id = '100'
    service.write.return_value = {'rpid': 2}
    assert json.loads((await session.call(call)).content)['rpid'] == 2
    bridge.context.return_value.user_id = '200'
    with pytest.raises(AgentError, match='AGENT_TOOL_UNAUTHORIZED'):
        await session.call(call)


async def test_same_request_reuses_reply_result(store):  # noqa: F811
    """同一请求重入不会再向平台发送评论。"""
    from agent_bot.plugins.bilibili.contracts import ReplyArguments
    from agent_bot.plugins.bilibili.service import BilibiliService
    await store.save_account(account())
    client = SimpleNamespace(reply=AsyncMock(return_value={'rpid': 2}))
    service = BilibiliService(store, client)
    args = ReplyArguments(video_id='av170001', root=1, parent=1, message='测试')
    kwargs = {'owner': MediaOwner(entry='admin', session_id='s'), 'request_id': 'r'}
    assert await service.write('bilibili_reply_comment', args, **kwargs) == {'rpid': 2}
    assert await service.write('bilibili_reply_comment', args, **kwargs) == {'rpid': 2}
    client.reply.assert_awaited_once()


async def test_failed_record_save_prevents_repeat_external_write(store):  # noqa: F811
    """远端成功但保存失败时保留 pending，同请求不能再次发送。"""
    from agent_bot.plugins.bilibili.contracts import ReplyArguments
    from agent_bot.plugins.bilibili.service import BilibiliService
    await store.save_account(account())
    store.complete = AsyncMock(side_effect=RuntimeError('offline database failure'))
    client = SimpleNamespace(reply=AsyncMock(return_value={'rpid': 2}))
    service = BilibiliService(store, client)
    args = ReplyArguments(video_id='av170001', root=1, parent=1, message='测试')
    kwargs = {'owner': MediaOwner(entry='admin', session_id='s'), 'request_id': 'r'}
    with pytest.raises(AgentError, match='BILIBILI_RESULT_UNKNOWN'):
        await service.write('bilibili_reply_comment', args, **kwargs)
    with pytest.raises(AgentError, match='BILIBILI_OPERATION_PENDING'):
        await service.write('bilibili_reply_comment', args, **kwargs)
    client.reply.assert_awaited_once()


async def test_lost_reply_response_reports_unknown_without_resending(store):  # noqa: F811
    """评论请求已发出后的网络失败必须说明可能已发送。"""
    from agent_bot.plugins.bilibili.contracts import ReplyArguments
    from agent_bot.plugins.bilibili.service import BilibiliService
    await store.save_account(account())
    client = SimpleNamespace(reply=AsyncMock(side_effect=AgentError('BILIBILI_NETWORK_ERROR')))
    service = BilibiliService(store, client)
    args = ReplyArguments(video_id='av170001', root=1, parent=1, message='测试')
    kwargs = {'owner': MediaOwner(entry='admin', session_id='s'), 'request_id': 'r'}
    with pytest.raises(AgentError, match='BILIBILI_RESULT_UNKNOWN'):
        await service.write('bilibili_reply_comment', args, **kwargs)
    with pytest.raises(AgentError, match='BILIBILI_OPERATION_PENDING'):
        await service.write('bilibili_reply_comment', args, **kwargs)
    client.reply.assert_awaited_once()


async def test_upload_refuses_arbitrary_host_before_sending_cookies():
    """平台响应里的上传地址也不能把请求引向其他站点。"""
    def handle(request):
        """只允许一次源站预上传请求。"""
        assert request.url.host == 'member.bilibili.com'
        return httpx.Response(200, json={'OK': 1, 'endpoint': 'https://attacker.invalid',
                                       'upos_uri': 'upos://bucket/file.mp4'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(AgentError, match='BILIBILI_INVALID_RESPONSE'):
            await publish_video(BilibiliClient(http), account(), PublishArguments(
                video_asset_id='a' * 32, cover_asset_id='b' * 32, title='测试',
                tid=1, tags=['测试']),
                io.BytesIO(b'1234567890'), 10, b'cover', 'image/png')


async def test_multipart_upload_does_not_send_login_cookie_to_cdn():
    """逐块上传大小正确，CDN 只有临时令牌，最终结果表示等待审核。"""
    chunks = []
    def handle(request):
        """模拟完整投稿协议并验证实际上传内容与元数据。"""
        if request.url.host.endswith('bilivideo.com'):
            assert 'cookie' not in request.headers
            assert request.headers['x-upos-auth'] == 'temporary-upload-token'
            if request.method == 'PUT':
                chunks.append(request.content)
                return httpx.Response(200, text='MULTIPART_PUT_SUCCESS')
            if 'uploads' in request.url.params:
                return httpx.Response(200, json={'OK': 1, 'upload_id': 'upload-id'})
            assert len(json.loads(request.content)['parts']) == 3
            return httpx.Response(200, json={'OK': 1, 'key': '/bucket/file.mp4'})
        assert 'SESSDATA=test-session' in request.headers['cookie']
        if request.url.path == '/preupload':
            return httpx.Response(200, json={
                'OK': 1, 'endpoint': '//upos-test.bilivideo.com', 'upos_uri': 'upos://bucket/file.mp4',
                'auth': 'temporary-upload-token', 'chunk_size': 4, 'biz_id': 10,
            })
        if request.url.path.endswith('/cover/up'):
            return httpx.Response(200, json={'code': 0, 'data': {'url': 'https://i0.hdslb.com/a.png'}})
        body = json.loads(request.content)
        assert body['videos'][0]['filename'] == 'bucket/file'
        assert body['tag'] == '测试'
        return httpx.Response(200, json={'code': 0, 'data': {'aid': 10, 'bvid': 'BV17x411w7KC'}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await publish_video(BilibiliClient(http), account(), PublishArguments(
            video_asset_id='a' * 32, cover_asset_id='b' * 32, title='测试', tid=1, tags=['测试']),
            io.BytesIO(b'1234567890'), 10, b'cover', 'image/png')
    assert chunks == [b'1234', b'5678', b'90']
    assert result['status'] == 'submitted_for_review'
