"""B 站接口和数据库在离线环境下的真实协议及事务行为。"""

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from agent_bot.agent.errors import AgentError
from agent_bot.plugins.bilibili.client import BilibiliClient
from agent_bot.plugins.bilibili.contracts import Account, PublishArguments, ReplyArguments
from agent_bot.plugins.bilibili.service import BilibiliService
from agent_bot.plugins.bilibili.store import BilibiliStore
from agent_bot.settings import DatabaseParams
from agent_bot.sqlite_database import SqliteConnection


@pytest.fixture
def store(tmp_path):
    """仅向临时 SQLite 导入新增表，不接触开发者数据库。"""
    path = tmp_path / 'bili.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.executescript(Path('sql/bilibili-init.sqlite.sql').read_text('utf-8'))
    return BilibiliStore(DatabaseParams(backend='sqlite', sqlite_path=str(path)),
                         connect=lambda **kw: SqliteConnection(
                             str(path), cursorclass=kw['cursorclass']))


def account():
    """构造只供离线调用使用的登录凭据。"""
    return Account(uid='12345', name='离线账号', cookies=SecretStr(json.dumps({
        'SESSDATA': 'test-session', 'bili_jct': 'test-csrf', 'DedeUserID': '12345',
    })))


async def test_account_and_operations_survive_new_store_instance(store):
    """账号重启保留，同请求成功结果返回缓存而不是再次执行。"""
    await store.save_account(account())
    assert (await store.account()).uid == '12345'
    assert 'test-session' not in (await store.account()).model_dump_json()
    assert await store.begin('a' * 64, '12345', 'bilibili_reply_comment') is None
    with pytest.raises(AgentError, match='BILIBILI_OPERATION_PENDING'):
        await store.begin('a' * 64, '12345', 'bilibili_reply_comment')
    await store.complete('a' * 64, {'rpid': 77})
    other = BilibiliStore(store.params, connect=store.connect)
    assert await other.begin('a' * 64, '12345', 'bilibili_reply_comment') == {'rpid': 77}
    await other.clear_account()
    assert await other.account() is None


async def test_concurrent_operation_has_only_one_owner(store):
    """唯一占位在真实 SQLite 写事务下仲裁，而不是依赖进程内缓存。"""
    other = BilibiliStore(store.params, connect=store.connect)
    results = await asyncio.gather(
        store.begin('b' * 64, '12345', 'bilibili_reply_comment'),
        other.begin('b' * 64, '12345', 'bilibili_reply_comment'), return_exceptions=True,
    )
    assert results.count(None) == 1
    assert sum(isinstance(value, AgentError) for value in results) == 1


def test_incremental_sqlite_schema_matches_full_initialization():
    """新库与既有库增量得到相同字段、默认值和 CHECK 约束。"""
    schemas = []
    for path in ('sql/bilibili-init.sqlite.sql', 'sql/agent_bot_init.sqlite.sql'):
        with sqlite3.connect(':memory:') as connection:
            connection.executescript(Path(path).read_text('utf-8'))
            schemas.append(connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE name LIKE 'agent_bot_bilibili_%' "
                'ORDER BY name').fetchall())
    assert schemas[0] == schemas[1]


@pytest.mark.parametrize('platform_code,state', [(86101, 'waiting'), (86090, 'scanned'),
                                                (86038, 'expired')])
async def test_qr_platform_states(platform_code, state):
    """未确认的二维码不产生账号凭据。"""
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={'code': 0, 'data': {'code': platform_code}}))) as http:
        assert await BilibiliClient(http).poll_qr('offline') == (state, None)


@pytest.mark.parametrize('url', [
    'https://account.bilibili.com/h5/account-h5/auth/scan-web?qrcode_key=offline',
    'https://passport.bilibili.com/h5-app/passport/login/scan?qrcode_key=offline',
    'https://passport.biligame.com/qrcode/scan?qrcode_key=offline',
])
async def test_generate_qr_accepts_current_and_existing_official_login_hosts(url):
    """平台已将新二维码迁至 account 域名，不能把成功响应误判成协议错误。"""
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={'code': 0, 'data': {'url': url, 'qrcode_key': 'offline-key'}}
    ))) as http:
        result = await BilibiliClient(http).generate_qr()
    assert result == {'url': url, 'qrcode_key': 'offline-key'}


@pytest.mark.parametrize('url', [
    'http://account.bilibili.com/h5/account-h5/auth/scan-web',
    'https://account.bilibili.com.example.org/scan',
    'https://example.org/scan',
])
async def test_generate_qr_rejects_insecure_or_untrusted_login_hosts(url):
    """兼容官方新域名时仍只接受 HTTPS 与明确登记的完整主机名。"""
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={'code': 0, 'data': {'url': url, 'qrcode_key': 'offline-key'}}
    ))) as http:
        with pytest.raises(AgentError, match='BILIBILI_INVALID_RESPONSE'):
            await BilibiliClient(http).generate_qr()


async def test_confirmed_qr_extracts_server_only_cookies():
    """凭据从回调中解析，不需要跟随带 Cookie 的重定向。"""
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={'code': 0, 'data': {'code': 0, 'url':
            'https://www.bilibili.com/?SESSDATA=test-session&bili_jct=test-csrf&DedeUserID=12345'}}
    ))) as http:
        state, value = await BilibiliClient(http).poll_qr('offline')
        assert state == 'confirmed' and value.uid == '12345'
        assert value.cookie_values()['bili_jct'] == 'test-csrf'
        assert 'test-csrf' not in value.model_dump_json()


@pytest.mark.parametrize('set_cookie', [False, True])
async def test_sessdata_callback_keeps_cookie_encoding(set_cookie):
    """扫码凭据中的编码逗号不能变成原始分隔符发回平台。"""
    def handle(request):
        """同时覆盖 Set-Cookie 和仅回调 URL 两种正常登录响应。"""
        if request.url.path.endswith('/poll'):
            headers = ({'Set-Cookie': 'SESSDATA=abc%2C123%2Cxyz; Path=/; Secure'}
                       if set_cookie else {})
            return httpx.Response(200, headers=headers, json={'code': 0, 'data': {
                'code': 0, 'url': 'https://www.bilibili.com/?SESSDATA=abc%2C123%2Cxyz'
                                  '&bili_jct=test-csrf&DedeUserID=12345',
            }})
        assert 'SESSDATA=abc%2C123%2Cxyz' in request.headers['cookie']
        return httpx.Response(200, json={'code': 0, 'data': {
            'isLogin': True, 'mid': 12345, 'uname': '离线账号',
        }})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = BilibiliClient(http)
        _, value = await client.poll_qr('offline')
        assert (await client.login_status(value))['logged_in']


async def test_client_login_and_reply_do_not_leak_credentials():
    """检查实际请求 Cookie/CSRF 和仅投影公开结果的行为。"""
    def handle(request):
        """验证写请求发送目标评论及必要 CSRF。"""
        if request.url.path.endswith('/nav'):
            return httpx.Response(200, json={'code': 0, 'data': {
                'isLogin': True, 'mid': 12345, 'uname': '离线账号',
            }})
        assert 'SESSDATA=test-session' in request.headers['cookie']
        assert 'csrf=test-csrf' in request.content.decode()
        assert 'parent=2' in request.content.decode()
        return httpx.Response(200, json={'code': 0, 'data': {'rpid': 99, 'secret': 'omit'}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = BilibiliClient(http)
        assert (await client.login_status(account()))['uid'] == '12345'
        result = await client.reply(account(), ReplyArguments(
            video_id='av170001', root=1, parent=2, message='你好'))
        assert result == {'rpid': 99, 'aid': 170001}


async def test_transport_failure_is_not_logged_out(store):
    """网络错误保留凭据并返回明确错误，不伪造未登录。"""
    await store.save_account(account())
    def handle(request):
        """模拟不包含真实网络的断连。"""
        raise httpx.ConnectError('sensitive transport text', request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        service = BilibiliService(store, BilibiliClient(http))
        with pytest.raises(AgentError, match='BILIBILI_NETWORK_ERROR'):
            await service.status()
    assert (await store.account()).uid == '12345'


async def test_replaced_qr_cannot_save_late_login(store):
    """旧轮询即使成功，也不能覆盖重新生成后的二维码会话。"""
    entered, release = asyncio.Event(), asyncio.Event()
    class Client:
        """提供可控的迟到登录响应。"""
        async def generate_qr(self):
            """返回离线二维码地址。"""
            return {'url': 'https://passport.bilibili.com/qrcode/test', 'qrcode_key': 'key'}
        async def poll_qr(self, key):
            """让测试在轮询途中替换二维码。"""
            entered.set()
            await release.wait()
            return 'confirmed', account()
        async def login_status(self, value):
            """确认登录身份。"""
            return {'logged_in': True, 'uid': value.uid, 'name': value.name}
    service = BilibiliService(store, Client())
    old = await service.generate_qr()
    polling = asyncio.create_task(service.poll_qr(old['session_id']))
    await entered.wait()
    await service.generate_qr()
    release.set()
    with pytest.raises(AgentError, match='BILIBILI_QR_EXPIRED'):
        await polling
    assert await store.account() is None


async def test_failed_login_verification_keeps_previous_account(store):
    """扫码成功但身份验证失败不能损坏已有登录。"""
    await store.save_account(account())
    class Client:
        """模拟新二维码确认后平台网络失败。"""
        async def generate_qr(self):
            """返回临时二维码。"""
            return {'url': 'https://passport.bilibili.com/qrcode/test', 'qrcode_key': 'key'}
        async def poll_qr(self, key):
            """返回待验证凭据。"""
            return 'confirmed', account().model_copy(update={'uid': '999'})
        async def login_status(self, value):
            """拒绝验证，旧记录必须保持。"""
            raise AgentError('BILIBILI_NETWORK_ERROR')
    service = BilibiliService(store, Client())
    qr = await service.generate_qr()
    with pytest.raises(AgentError, match='BILIBILI_NETWORK_ERROR'):
        await service.poll_qr(qr['session_id'])
    assert (await store.account()).uid == '12345'


def test_publish_requires_source_for_repost():
    """转载必须明确来源，不能静默改为原创。"""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        PublishArguments(video_asset_id='a' * 32, cover_asset_id='b' * 32,
                         title='视频', tid=1, tags=['测试'], copyright=2)


async def test_plugin_lifecycle_needs_no_qq_or_platform_login(store):
    """启停插件只校验本地表和创建客户端，未登录时仍能就绪。"""
    from types import SimpleNamespace

    from agent_bot.plugins.bilibili.plugin import BilibiliPlugin
    runtime = SimpleNamespace(database_params=store.params,
                              database_pool=SimpleNamespace(connect=store.connect),
                              bilibili_service=None)
    plugin = BilibiliPlugin()
    plugin.prepare(SimpleNamespace(runtime=runtime))
    await plugin.start()
    try:
        assert await plugin.is_ready()
        assert not (await plugin.service.status())['logged_in']
        assert plugin.bots() == []
    finally:
        await plugin.stop()
    assert not await plugin.is_ready()
    assert runtime.bilibili_service is None
