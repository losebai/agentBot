"""单视频 UPOS 分块上传，登录 Cookie 与 CDN 临时令牌严格分离。"""

import base64
from pathlib import PurePosixPath
from typing import BinaryIO
from urllib.parse import urlsplit

from agent_bot.agent.errors import AgentError
from agent_bot.database_pool import finish_database_write
from agent_bot.plugins.bilibili.client import MEMBER, BilibiliClient
from agent_bot.plugins.bilibili.contracts import Account, PublishArguments


async def publish_video(client: BilibiliClient, account: Account, args: PublishArguments,
                        stream: BinaryIO, size: int,
                        cover: bytes, cover_type: str) -> dict:
    """逐块读取媒体租约，只有提交返回 aid/bvid 后才报告已提交审核。"""
    pre = await client.request('GET', MEMBER + '/preupload', account=account, raw=True,
                               params={'name': 'video.mp4', 'size': size, 'r': 'upos',
                                       'profile': 'ugcfx/bup', 'ssl': 1, 'version': '2.14.0',
                                       'build': '2100400', 'upcdn': 'bda2',
                                       'probe_version': '20221109'})
    try:
        endpoint = str(pre['endpoint'])
        endpoint = 'https:' + endpoint if endpoint.startswith('//') else endpoint
        parsed = urlsplit(endpoint)
        path = str(pre['upos_uri'])
        if (parsed.scheme != 'https' or parsed.username or parsed.password
                or parsed.port not in {None, 443} or parsed.path not in {'', '/'}
                or parsed.query or parsed.fragment or not parsed.hostname
                or not parsed.hostname.endswith('.bilivideo.com')
                or not path.startswith('upos://')
                or any(part in {'.', '..'} for part in path[7:].split('/'))
                or any(c in path for c in '?\\#\r\n')):
            raise ValueError('invalid upload target')
        url = endpoint.rstrip('/') + '/' + path[7:]
        chunk_size = int(pre['chunk_size'])
        if not 1 <= chunk_size <= 16 * 1024 * 1024 or not str(pre['auth']):
            raise ValueError('invalid chunk size')
        headers = {'x-upos-auth': str(pre['auth'])}
        data = await client.request('POST', url, raw=True, headers=headers, params={
            'uploads': '', 'output': 'json', 'profile': 'ugcfx/bup', 'filesize': size,
            'partsize': chunk_size, 'biz_id': pre['biz_id'],
        })
        upload_id = data['upload_id']
        parts = []
        chunks = (size + chunk_size - 1) // chunk_size
        for index in range(chunks):
            # 取消必须等待本次文件读取结束，租约才能安全关闭句柄。
            chunk = await finish_database_write(lambda: stream.read(chunk_size))
            if len(chunk) != min(chunk_size, size - index * chunk_size):
                raise ValueError('truncated asset')
            response = await client.response('PUT', url, content=chunk, headers=headers, params={
                'partNumber': index + 1, 'uploadId': upload_id, 'chunk': index, 'chunks': chunks,
                'size': len(chunk), 'start': index * chunk_size,
                'end': index * chunk_size + len(chunk), 'total': size,
            })
            if response.text not in {'', 'MULTIPART_PUT_SUCCESS'}:
                raise AgentError('BILIBILI_UPSTREAM_REJECTED')
            parts.append({'partNumber': index + 1, 'eTag': response.headers.get('etag', 'etag')})
        completed = await client.request('POST', url, raw=True, headers=headers,
                                         params={'output': 'json', 'name': 'video.mp4',
                                                 'profile': 'ugcfx/bup', 'uploadId': upload_id,
                                                 'biz_id': pre['biz_id']}, json={'parts': parts})
        filename = str(PurePosixPath(completed['key'].lstrip('/')).with_suffix(''))
        csrf = account.cookie_values()['bili_jct']
        cover_data = await client.request('POST', MEMBER + '/x/vu/web/cover/up', account=account,
                                          data={'csrf': csrf, 'cover': 'data:' + cover_type
                                                + ';base64,' + base64.b64encode(cover).decode()})
        metadata = {'title': args.title, 'desc': args.description, 'tid': args.tid,
                    'tag': ','.join(args.tags), 'copyright': args.copyright,
                    'source': args.source, 'cover': cover_data['url'], 'csrf': csrf,
                    'desc_format_id': 9999, 'recreate': -1,
                    'videos': [{'filename': filename, 'title': args.title, 'desc': ''}]}
    except (KeyError, TypeError, ValueError):
        raise AgentError('BILIBILI_INVALID_RESPONSE') from None
    # 最终提交一旦发出，超时无法证明是否成功，因此禁止隐式重试。
    try:
        result = await client.request('POST', MEMBER + '/x/vu/web/add/v3', account=account,
                                       params={'csrf': csrf}, json=metadata)
        aid, bvid = int(result['aid']), str(result['bvid'])
        if aid < 1 or not bvid.startswith('BV') or len(bvid) != 12:
            raise ValueError('missing archive id')
    except AgentError as error:
        if error.code not in {'BILIBILI_NETWORK_ERROR', 'BILIBILI_INVALID_RESPONSE'}:
            raise
        raise AgentError('BILIBILI_RESULT_UNKNOWN', status_code=409) from None
    except (KeyError, TypeError, ValueError):
        raise AgentError('BILIBILI_RESULT_UNKNOWN', status_code=409) from None
    return {'aid': aid, 'bvid': bvid, 'url': 'https://www.bilibili.com/video/' + bvid,
            'status': 'submitted_for_review'}
