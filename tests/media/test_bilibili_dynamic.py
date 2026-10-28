"""图文动态的整批媒体授权、真实上传协议和持久防重发验证。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from tests.unit.test_bilibili import account, store  # noqa: F401

from agent_bot.agent.contracts import ExecutionAuthority, ToolCall
from agent_bot.agent.errors import AgentError
from agent_bot.media.contracts import MediaOwner
from agent_bot.media.errors import MediaError
from agent_bot.plugins.bilibili.client import BilibiliClient
from agent_bot.plugins.bilibili.service import BilibiliService
from agent_bot.plugins.bilibili.tools import BilibiliTools
from agent_bot.services.chat.routing import ChatRouteSelector
from media.test_inputs import picture
from media.test_store import storage

OWNER = MediaOwner(entry='admin', session_id='admin_dynamic-test')


@pytest.mark.parametrize('changes', [
    {'text': ' '}, {'text': '文' * 2001}, {'image_asset_ids': []},
    {'image_asset_ids': ['a' * 32] * 10}, {'image_asset_ids': ['a' * 32] * 2},
    {'image_asset_ids': ['https://example.org/image.png']}, {'uid': '999'},
])
def test_dynamic_parameters_reject_unbounded_or_external_inputs(changes):
    """模型只能提供有界正文与本地图片 ID，不能替换账号或上传来源。"""
    from pydantic import ValidationError

    from agent_bot.plugins.bilibili.contracts import DynamicPublishArguments

    with pytest.raises(ValidationError):
        DynamicPublishArguments.model_validate({
            'text': '今天的照片', 'image_asset_ids': ['a' * 32], **changes,
        })


async def test_dynamic_uploads_images_in_order_and_reuses_success(
    store, media_database, agent_params, tmp_path, media_clock,  # noqa: F811
):
    """使用真实媒体归属和 SQLite 操作记录，成功重入不重复上传或发布。"""
    from agent_bot.plugins.bilibili.contracts import DynamicPublishArguments

    await store.save_account(account())
    requests = []
    def handle(request):
        """按实际 multipart 和 JSON 检查固定源站、CSRF 与图文顺序。"""
        requests.append(request)
        assert request.url.host == 'api.bilibili.com'
        assert 'SESSDATA=test-session' in request.headers['cookie']
        if request.url.path.endswith('/upload_bfs'):
            assert 'multipart/form-data' in request.headers['content-type']
            assert b'name="file_up"' in request.content
            assert b'test-csrf' in request.content
            assert b'daily' in request.content
            number = len(requests)
            return httpx.Response(200, json={'code': 0, 'data': {
                'image_url': f'https://i0.hdslb.com/bfs/new_dyn/{number}.png',
                'image_width': 12, 'image_height': 9,
            }})
        assert request.url.path == '/x/dynamic/feed/create/dyn'
        assert request.url.params['csrf'] == 'test-csrf'
        payload = json.loads(request.content)['dyn_req']
        assert payload['scene'] == 2
        assert payload['content']['contents'] == [
            {'raw_text': '今天的照片', 'type': 1, 'biz_id': ''},
        ]
        assert [item['img_src'] for item in payload['pics']] == [
            'https://i0.hdslb.com/bfs/new_dyn/1.png', 'https://i0.hdslb.com/bfs/new_dyn/2.png',
        ]
        assert all(item['img_width'] == 12 and item['img_size'] > 0 for item in payload['pics'])
        return httpx.Response(200, json={'code': 0, 'data': {
            'dyn_id_str': '1234567890123456789', 'result': 0, 'secret': 'never-return',
        }})
    async with storage(media_database, agent_params, tmp_path, media_clock) as media:
        assets = [
            await media.put(owner=OWNER, purpose='input', data=picture(), mime_type='image/png'),
            await media.put(owner=OWNER, purpose='output', data=picture('JPEG'),
                            mime_type='image/jpeg'),
        ]
        args = DynamicPublishArguments(text='今天的照片', image_asset_ids=[a.id for a in assets])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            service = BilibiliService(store, BilibiliClient(http))
            params = {'owner': OWNER, 'request_id': 'one-publish', 'media_store': media}
            result = await service.write('bilibili_publish_dynamic', args, **params)
            assert await service.write('bilibili_publish_dynamic', args, **params) == result
            media_clock.advance(86401)
            await media.cleanup(media_clock())
            assert await service.write('bilibili_publish_dynamic', args, **params) == result
    assert len(requests) == 3
    assert result == {'dynamic_id': '1234567890123456789',
                      'url': 'https://t.bilibili.com/1234567890123456789', 'status': 'submitted'}
    assert 'test-session' not in json.dumps(result) and 'never-return' not in json.dumps(result)


@pytest.mark.parametrize('bad_kind', ['other_owner', 'corrupt', 'webp'])
async def test_all_dynamic_assets_are_checked_before_first_external_upload(
    store, media_database, agent_params, tmp_path, media_clock, bad_kind,  # noqa: F811
):
    """第二张不合法也必须整批失败，第一张不能先被上传到平台。"""
    from agent_bot.plugins.bilibili.contracts import DynamicPublishArguments

    await store.save_account(account())
    client = SimpleNamespace(request=AsyncMock())
    async with storage(media_database, agent_params, tmp_path, media_clock) as media:
        first = await media.put(owner=OWNER, purpose='input', data=picture(), mime_type='image/png')
        second = await media.put(
            owner=(MediaOwner(entry='admin', session_id='other')
                   if bad_kind == 'other_owner' else OWNER),
            purpose='input', data=b'not-image' if bad_kind == 'corrupt' else picture('WEBP'),
            mime_type='image/webp' if bad_kind == 'webp' else 'image/png',
        )
        args = DynamicPublishArguments(text='正文', image_asset_ids=[first.id, second.id])
        with pytest.raises((MediaError, AgentError)):
            await BilibiliService(store, client).write(
                'bilibili_publish_dynamic', args, owner=OWNER, request_id='invalid',
                media_store=media,
            )
    client.request.assert_not_awaited()


@pytest.mark.parametrize('failure', [
    'network', 'invalid_response', 'invalid_envelope', 'http_500', 'cancel',
])
async def test_uncertain_dynamic_submission_stays_pending_without_resending(
    store, media_database, agent_params, tmp_path, media_clock, failure,  # noqa: F811
):
    """提交响应丢失、结构异常或请求取消都不能用同一请求自动重发。"""
    from agent_bot.plugins.bilibili.contracts import DynamicPublishArguments

    await store.save_account(account())
    posts = []
    def handle(request):
        """图片上传成功后模拟最终发布响应不确定。"""
        posts.append(request.url.path)
        if request.url.path.endswith('/upload_bfs'):
            return httpx.Response(200, json={'code': 0, 'data': {
                'image_url': 'https://i0.hdslb.com/bfs/new_dyn/test.png',
                'image_width': 12, 'image_height': 9,
            }})
        if failure == 'network':
            raise httpx.ReadError('offline', request=request)
        if failure == 'cancel':
            raise asyncio.CancelledError()
        if failure == 'http_500':
            return httpx.Response(500, json={'message': 'offline failure'})
        if failure == 'invalid_envelope':
            return httpx.Response(200, json={})
        return httpx.Response(200, json={'code': 0, 'data': {}})
    async with storage(media_database, agent_params, tmp_path, media_clock) as media:
        asset = await media.put(owner=OWNER, purpose='input', data=picture(), mime_type='image/png')
        args = DynamicPublishArguments(text='正文', image_asset_ids=[asset.id])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            service = BilibiliService(store, BilibiliClient(http))
            params = {'owner': OWNER, 'request_id': 'uncertain', 'media_store': media}
            if failure == 'cancel':
                with pytest.raises(asyncio.CancelledError):
                    await service.write('bilibili_publish_dynamic', args, **params)
            else:
                with pytest.raises(AgentError, match='BILIBILI_RESULT_UNKNOWN'):
                    await service.write('bilibili_publish_dynamic', args, **params)
            with pytest.raises(AgentError, match='BILIBILI_OPERATION_PENDING'):
                await service.write('bilibili_publish_dynamic', args, **params)
    assert posts == ['/x/dynamic/feed/draw/upload_bfs', '/x/dynamic/feed/create/dyn']


async def test_dynamic_write_tool_rechecks_qq_superuser_and_stays_visible_to_model():
    """新发布工具参与 QQ 路由，但普通群成员不能调用写操作。"""
    assert ChatRouteSelector().select('发布图文', available_tools=(
        'bilibili_publish_dynamic',
    )).chat_tools == ('bilibili_publish_dynamic',)
    service = SimpleNamespace(write=AsyncMock(return_value={'dynamic_id': '123'}))
    context = SimpleNamespace(user_id='200', allowed_tools={'bilibili_publish_dynamic'},
                              ensure_active=lambda: None)
    bridge = SimpleNamespace(context=AsyncMock(return_value=context))
    session = BilibiliTools(lambda: service, lambda: {'100'}).bind(
        SimpleNamespace(session_id='qq-test', request_id='r', attachment_ids=('a' * 32,)),
        ExecutionAuthority(entry='qq', allowed_tools=('bilibili_publish_dynamic',)),
        None, None, bridge,
    )
    assert 'a' * 32 in next(item.description for item in session.definitions
                            if item.name == 'bilibili_publish_dynamic')
    call = ToolCall(call_id='c', name='bilibili_publish_dynamic', arguments={
        'text': '正文', 'image_asset_ids': ['a' * 32],
    })
    with pytest.raises(AgentError, match='AGENT_TOOL_UNAUTHORIZED'):
        await session.call(call)
    service.write.assert_not_awaited()
    context.user_id = '100'
    assert json.loads((await session.call(call)).content)['dynamic_id'] == '123'


async def test_dynamic_total_size_is_checked_before_decoding_or_upload(
    store, media_database, agent_params, tmp_path, media_clock,  # noqa: F811
):
    """单图未超限但整批超过20MiB时，应先报告总量超限而不解析或上传图片。"""
    from agent_bot.plugins.bilibili.contracts import DynamicPublishArguments

    await store.save_account(account())
    client = SimpleNamespace(request=AsyncMock())
    async with storage(media_database, agent_params, tmp_path, media_clock) as media:
        raw = b'x' * (8 * 1024 * 1024)
        assets = [await media.put(owner=OWNER, purpose='input', data=raw, mime_type='image/png')
                  for _ in range(3)]
        args = DynamicPublishArguments(text='正文', image_asset_ids=[item.id for item in assets])
        with pytest.raises(MediaError, match='MEDIA_TOO_LARGE'):
            await BilibiliService(store, client).write(
                'bilibili_publish_dynamic', args, owner=OWNER, request_id='too-big',
                media_store=media,
            )
    client.request.assert_not_awaited()


@pytest.mark.parametrize('url', [123, 'https://attacker.invalid/image.png'])
async def test_invalid_uploaded_image_address_prevents_final_publication(url):
    """上游返回无效图片地址时明确报协议错误，不能继续最终提交。"""
    from agent_bot.plugins.bilibili.contracts import DynamicPublishArguments
    from agent_bot.plugins.bilibili.dynamic import DynamicImage, publish_dynamic

    client = SimpleNamespace(request=AsyncMock(return_value={
        'image_url': url, 'image_width': 12, 'image_height': 9,
    }))
    with pytest.raises(AgentError, match='BILIBILI_INVALID_RESPONSE'):
        await publish_dynamic(client, account(), DynamicPublishArguments(
            text='正文', image_asset_ids=['a' * 32],
        ), (DynamicImage(mime_type='image/png', data=picture()),))
    assert client.request.await_count == 1
