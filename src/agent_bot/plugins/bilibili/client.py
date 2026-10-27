"""B 站普通网页登录和视频评论协议；错误正文不得包含上游响应。"""

import json
from urllib.parse import parse_qs, quote, urlsplit

import httpx
from pydantic import SecretStr

from agent_bot.agent.errors import AgentError
from agent_bot.plugins.bilibili.contracts import Account, CommentsArguments, ReplyArguments
from agent_bot.services.request_deadline import remaining_timeout

API = 'https://api.bilibili.com'
PASSPORT = 'https://passport.bilibili.com'
MEMBER = 'https://member.bilibili.com'


class BilibiliClient:
    """宿主持有 HTTP 生命周期；每次显式构造 Cookie，避免账号和 CDN 串用。"""

    def __init__(self, http: httpx.AsyncClient):
        """依赖注入客户端，测试只使用 MockTransport。"""
        self.http = http

    async def request(self, method: str, url: str, *, account: Account | None = None,
                      raw: bool = False, **kwargs) -> dict:
        """不跟随重定向，不重试写请求，不把平台错误正文带出边界。"""
        response = await self.response(method, url, account=account, **kwargs)
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError('invalid body')
            if raw:
                if body.get('OK') != 1:
                    raise AgentError('BILIBILI_UPSTREAM_REJECTED')
                return body
            code = body.get('code')
            if code in {-101, -111}:
                raise AgentError('BILIBILI_LOGIN_EXPIRED', status_code=409)
            if code in {-352, -412, 86095}:
                raise AgentError('BILIBILI_VERIFICATION_REQUIRED', status_code=409)
            if code in {-509, 12019}:
                raise AgentError('BILIBILI_RATE_LIMITED', status_code=429)
            if code != 0:
                raise AgentError('BILIBILI_UPSTREAM_REJECTED')
            data = body.get('data')
            if not isinstance(data, dict):
                raise ValueError('invalid data')
            return data
        except (ValueError, TypeError, KeyError):
            raise AgentError('BILIBILI_INVALID_RESPONSE') from None

    async def response(self, method: str, url: str, *, account: Account | None = None,
                       **kwargs) -> httpx.Response:
        """Cookie 只发送到固定平台源站，上传 CDN 只接收临时上传令牌。"""
        try:
            headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'}
            headers.update(kwargs.pop('headers', {}))
            request = self.http.build_request(method, url, headers=headers,
                                             timeout=remaining_timeout(30), **kwargs)
            request.headers.pop('cookie', None)
            if account is not None:
                if urlsplit(url).hostname not in {
                    'api.bilibili.com', 'passport.bilibili.com', 'member.bilibili.com',
                }:
                    raise AgentError('BILIBILI_INVALID_RESPONSE')
                httpx.Cookies(account.cookie_values()).set_cookie_header(request)
            response = await self.http.send(request, follow_redirects=False)
            if response.status_code == 429:
                raise AgentError('BILIBILI_RATE_LIMITED', status_code=429)
            if response.status_code in {403, 412}:
                raise AgentError('BILIBILI_VERIFICATION_REQUIRED', status_code=409)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError:
            raise AgentError('BILIBILI_UPSTREAM_REJECTED') from None
        except httpx.RequestError:
            raise AgentError('BILIBILI_NETWORK_ERROR') from None

    async def generate_qr(self) -> dict:
        """二维码 URL 仅由管理页绘制，不使用外部图片服务。"""
        data = await self.request('GET', PASSPORT + '/x/passport-login/web/qrcode/generate')
        url = urlsplit(str(data.get('url', '')))
        if url.scheme != 'https' or url.hostname not in {
            'passport.bilibili.com', 'passport.biligame.com',
        } or not isinstance(data.get('qrcode_key'), str):
            raise AgentError('BILIBILI_INVALID_RESPONSE')
        return {'url': data['url'], 'qrcode_key': data['qrcode_key']}

    async def poll_qr(self, key: str) -> tuple[str, Account | None]:
        """网页登录确认通常把凭据放在回调 URL，也支持 Set-Cookie。"""
        response = await self.response('GET', PASSPORT + '/x/passport-login/web/qrcode/poll',
                                       params={'qrcode_key': key})
        try:
            body = response.json()
            if body['code'] != 0:
                raise AgentError('BILIBILI_UPSTREAM_REJECTED')
            data = body['data']
            state = {86101: 'waiting', 86090: 'scanned', 86038: 'expired', 0: 'confirmed'}.get(
                data['code'])
            if state is None:
                raise AgentError('BILIBILI_VERIFICATION_REQUIRED', status_code=409)
            if state != 'confirmed':
                return state, None
            values = parse_qs(urlsplit(data.get('url', '')).query)
            cookies = {}
            for name in ('SESSDATA', 'bili_jct', 'DedeUserID'):
                value = response.cookies.get(name) or values.get(name, [None])[0]
                if not value or any(c in value for c in '\r\n;\0'):
                    raise ValueError('missing cookie')
                if name == 'SESSDATA':
                    # 平台 Cookie 保持 URL 编码；回调解析不得把 %2C 变成裸逗号。
                    value = quote(value, safe='%')
                cookies[name] = value
            return state, Account(uid=cookies['DedeUserID'], name='',
                                  cookies=SecretStr(json.dumps(cookies)))
        except (ValueError, KeyError, TypeError):
            raise AgentError('BILIBILI_INVALID_RESPONSE') from None

    async def login_status(self, account: Account) -> dict:
        """失效与网络失败分开；不修改存储的凭据。"""
        try:
            data = await self.request('GET', API + '/x/web-interface/nav', account=account)
        except AgentError as error:
            if error.code == 'BILIBILI_LOGIN_EXPIRED':
                return {'logged_in': False, 'status': 'expired'}
            raise
        if not data.get('isLogin'):
            return {'logged_in': False, 'status': 'expired'}
        try:
            uid, name = str(int(data['mid'])), str(data['uname'])
            if uid != account.uid or not 1 <= len(name) <= 200:
                raise ValueError('account mismatch')
        except (KeyError, ValueError, TypeError):
            raise AgentError('BILIBILI_INVALID_RESPONSE') from None
        return {'logged_in': True, 'status': 'logged_in', 'uid': uid, 'name': name}

    async def aid(self, account: Account, video_id: str) -> int:
        """BV 通过公开视频信息解析，AV 不需要额外查询。"""
        if video_id.startswith('av'):
            return int(video_id[2:])
        data = await self.request('GET', API + '/x/web-interface/view', account=account,
                                  params={'bvid': video_id})
        try:
            return int(data['aid'])
        except (KeyError, TypeError, ValueError):
            raise AgentError('BILIBILI_INVALID_RESPONSE') from None

    async def comments(self, account: Account, args: CommentsArguments) -> dict:
        """只投影评论必要字段，避免把平台原始资料交给模型。"""
        aid = await self.aid(account, args.video_id)
        params = {'type': 1, 'oid': aid, 'pn': args.page, 'ps': args.page_size, 'sort': 0}
        if args.root is not None:
            params['root'] = args.root
        path = '/x/v2/reply/reply' if args.root else '/x/v2/reply'
        data = await self.request('GET', API + path, account=account, params=params)
        try:
            items = [{
                'rpid': int(item['rpid']), 'root': int(item.get('root') or item['rpid']),
                'uid': str(item['mid']), 'name': str(item['member']['uname'])[:200],
                'message': str(item['content']['message'])[:4000],
            } for item in (data.get('replies') or [])[:args.page_size]]
            return {'aid': aid, 'page': args.page, 'comments': items,
                    'has_more': args.page * args.page_size < int(
                        data.get('page', {}).get('count', 0))}
        except (TypeError, ValueError, KeyError):
            raise AgentError('BILIBILI_INVALID_RESPONSE') from None

    async def reply(self, account: Account, args: ReplyArguments) -> dict:
        """root 是楼层根 ID，parent 是实际回复对象；禁止自动改为顶层评论。"""
        aid = await self.aid(account, args.video_id)
        data = await self.request('POST', API + '/x/v2/reply/add', account=account, data={
            'type': 1, 'oid': aid, 'root': args.root, 'parent': args.parent,
            'message': args.message, 'csrf': account.cookie_values()['bili_jct'],
        })
        try:
            return {'rpid': int(data['rpid']), 'aid': aid}
        except (KeyError, ValueError, TypeError):
            raise AgentError('BILIBILI_INVALID_RESPONSE') from None
