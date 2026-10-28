"""图文动态只上传已授权图片，最终发布结果不确定时禁止自动重发。"""

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from agent_bot.agent.errors import AgentError
from agent_bot.database_pool import finish_database_write
from agent_bot.media.config import MAX_IMAGE_BYTES, MAX_TOTAL_IMAGE_BYTES
from agent_bot.media.contracts import MediaOwner
from agent_bot.media.errors import MediaError
from agent_bot.media.inputs import ImagePreparer
from agent_bot.media.store import MediaStore
from agent_bot.plugins.bilibili.client import API, BilibiliClient
from agent_bot.plugins.bilibili.contracts import Account, DynamicPublishArguments


@dataclass(frozen=True, slots=True)
class DynamicImage:
    """仅在单次请求内持有已校验原图，默认 repr 不包含图片内容。"""

    mime_type: str
    data: bytes = field(repr=False)


async def prepare_dynamic_images(
    media_store: MediaStore, ids: list[str], owner: MediaOwner,
) -> tuple[DynamicImage, ...]:
    """先授权整批元数据再校验所有内容，任何坏图都不能导致部分上传。"""
    records = [await media_store.describe(id, owner=owner) for id in ids]
    if any(record.mime_type not in {'image/png', 'image/jpeg'} for record in records):
        raise AgentError('BILIBILI_INVALID_DYNAMIC_IMAGE', status_code=422)
    if (any(record.size_bytes > MAX_IMAGE_BYTES for record in records)
            or sum(record.size_bytes for record in records) > MAX_TOTAL_IMAGE_BYTES):
        raise MediaError('MEDIA_TOO_LARGE', status_code=413)
    images = []
    for record in records:
        async with media_store.open_content(record.id, owner=owner) as stream:
            # 等待文件读取及解码线程结束后才能归还文件租约，避免取消时关闭仍在读取的句柄。
            raw = await finish_database_write(stream.read)
            await ImagePreparer().prepare_async(raw, record.mime_type)
        images.append(DynamicImage(mime_type=record.mime_type, data=raw))
    return tuple(images)


async def publish_dynamic(
    client: BilibiliClient, account: Account, args: DynamicPublishArguments,
    images: tuple[DynamicImage, ...],
) -> dict:
    """固定源站上传图片并提交纯文本组件；模型不能指定上传 URL 或 Cookie。"""
    csrf = account.cookie_values()['bili_jct']
    pics = []
    for number, image in enumerate(images, start=1):
        suffix = 'png' if image.mime_type == 'image/png' else 'jpg'
        result = await client.request(
            'POST', API + '/x/dynamic/feed/draw/upload_bfs', account=account,
            data={'category': 'daily', 'biz': 'new_dyn', 'csrf': csrf},
            files={'file_up': (f'image-{number}.{suffix}', image.data, image.mime_type)},
        )
        try:
            url = result['image_url']
            if not isinstance(url, str):
                raise ValueError('invalid image address')
            target = urlsplit(url)
            width, height = int(result['image_width']), int(result['image_height'])
            # 返回的 CDN 链接只作为发布元数据；不会下载它或向 CDN 发送账号凭据。
            if (target.scheme not in {'https', 'http'} or not target.hostname
                    or not target.hostname.endswith('.hdslb.com')
                    or target.username or target.password or width < 1 or height < 1):
                raise ValueError('invalid uploaded image')
            pics.append({'img_src': url, 'img_width': width, 'img_height': height,
                         'img_size': len(image.data) / 1024})
        except (KeyError, ValueError, TypeError):
            raise AgentError('BILIBILI_INVALID_RESPONSE') from None
    payload = {'dyn_req': {'scene': 2, 'pics': pics, 'content': {'contents': [
        {'raw_text': args.text, 'type': 1, 'biz_id': ''},
    ]}}}
    try:
        result = await client.request(
            'POST', API + '/x/dynamic/feed/create/dyn', account=account,
            params={'csrf': csrf}, json=payload, uncertain_write=True,
        )
        if 'result' in result and result['result'] != 0:
            raise AgentError('BILIBILI_UPSTREAM_REJECTED')
        dynamic_id = result['dyn_id_str']
        if (not isinstance(dynamic_id, str) or not dynamic_id.isascii()
                or not dynamic_id.isdecimal() or not 1 <= len(dynamic_id) <= 30
                or dynamic_id.startswith('0')):
            raise ValueError('invalid dynamic id')
    except AgentError as error:
        if error.code not in {'BILIBILI_NETWORK_ERROR', 'BILIBILI_INVALID_RESPONSE'}:
            raise
        raise AgentError('BILIBILI_RESULT_UNKNOWN', status_code=409) from None
    except (KeyError, ValueError, TypeError):
        raise AgentError('BILIBILI_RESULT_UNKNOWN', status_code=409) from None
    return {'dynamic_id': dynamic_id, 'url': 'https://t.bilibili.com/' + dynamic_id,
            'status': 'submitted'}
