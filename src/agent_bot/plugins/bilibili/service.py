"""扫码状态、账号替换和外部写操作互斥的业务边界。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from typing import Any

from agent_bot.agent.errors import AgentError
from agent_bot.database_pool import finish_database_write
from agent_bot.media.contracts import MediaOwner
from agent_bot.media.store import MediaStore
from agent_bot.plugins.bilibili.client import BilibiliClient
from agent_bot.plugins.bilibili.contracts import (
    Account,
    CommentsArguments,
    DynamicPublishArguments,
    MyVideosArguments,
    PublishArguments,
    ReplyArguments,
)
from agent_bot.plugins.bilibili.dynamic import prepare_dynamic_images, publish_dynamic
from agent_bot.plugins.bilibili.store import BilibiliStore
from agent_bot.plugins.bilibili.upload import publish_video


class BilibiliService:
    """二维码可替换，账号变更与写操作串行；不创建评论轮询任务。"""

    def __init__(self, store: BilibiliStore, client: BilibiliClient) -> None:
        """HTTP 和数据库由插件注入，锁只保护本进程的生命周期。"""
        self.store, self.client = store, client
        self._state_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._qr: dict[str, Any] | None = None

    async def status(self) -> dict:
        """实际检查平台登录，网络失败不改变已保存账号。"""
        account = await self.store.account()
        value = await self.client.login_status(account) if account else {
            'logged_in': False, 'status': 'not_logged_in'}
        return {**value, 'credential_set': account is not None, 'checked_at': int(time.time())}

    async def generate_qr(self) -> dict:
        """申请完成后发布新的本地会话，旧会话的迟到确认不得落库。"""
        async with self._state_lock:
            data = await self.client.generate_qr()
            self._qr = {**data, 'session_id': secrets.token_urlsafe(32),
                        'expires_at': int(time.time()) + 180, 'status': 'waiting'}
            return self._public_qr()

    def _public_qr(self) -> dict:
        """平台轮询 key 不返回页面，成功后不继续公开扫码 URL。"""
        return {key: self._qr[key] for key in ('session_id', 'expires_at', 'status', 'url')}

    def _current_qr(self, session_id: str) -> dict:
        """检查必须在状态锁内执行，不能仅在平台请求之前校验。"""
        if (self._qr is None or self._qr['session_id'] != session_id
                or self._qr['expires_at'] <= time.time()):
            raise AgentError('BILIBILI_QR_EXPIRED', status_code=409)
        return self._qr

    async def poll_qr(self, session_id: str) -> dict:
        """不持有状态锁等待手机确认，确认提交前再次校验会话。"""
        async with self._state_lock:
            qr = self._current_qr(session_id)
            if qr['status'] in {'confirmed', 'expired'}:
                return {'status': qr['status']}
            key = qr['qrcode_key']
        state, candidate = await self.client.poll_qr(key)
        if state == 'confirmed':
            status = await self.client.login_status(candidate)
            if not status.get('logged_in'):
                raise AgentError('BILIBILI_LOGIN_EXPIRED', status_code=409)
            candidate = candidate.model_copy(update={'name': status['name']})
            async with self._write_lock, self._state_lock:
                qr = self._current_qr(session_id)
                if qr['status'] != 'confirmed':
                    await self.store.save_account(candidate)
                    qr.update(status='confirmed', url='')
            return {'status': 'confirmed'}
        async with self._state_lock:
            qr = self._current_qr(session_id)
            # 并发轮询的旧 waiting 响应不能覆盖已提交的 confirmed。
            if qr['status'] not in {'confirmed', 'expired'}:
                qr['status'] = state
            return {'status': qr['status']}

    async def clear(self) -> None:
        """等待当前外部写操作收尾，然后撤销账号和所有未完成扫码。"""
        async with self._write_lock, self._state_lock:
            self._qr = None
            await self.store.clear_account()

    async def my_videos(self, args: MyVideosArguments) -> dict:
        """视频列表属于当前已保存账号，只查询已发布稿件，不接受模型覆盖 UID。"""
        return await self.client.my_videos(await self.require_account(), args)

    async def comments(self, args: CommentsArguments) -> dict:
        """读取工具必须先有已保存账号，平台仍验证凭据是否有效。"""
        return await self.client.comments(await self.require_account(), args)

    async def require_account(self) -> Account:
        """没有凭据时明确要求扫码，不从环境或旧连接猜测账号。"""
        account = await self.store.account()
        if account is None:
            raise AgentError('BILIBILI_NOT_LOGGED_IN', status_code=409)
        return account

    async def write(self, tool: str,
                    args: PublishArguments | ReplyArguments | DynamicPublishArguments, *,
                    owner: MediaOwner, request_id: str,
                    media_store: MediaStore | None = None) -> dict:
        """先验证媒体归属，再持久占位；中断后的 pending 不自动重发。"""
        if tool == 'bilibili_publish_video':
            if media_store is None:
                raise AgentError('MEDIA_NOT_INITIALIZED', status_code=503)
            video = await media_store.describe(args.video_asset_id, owner=owner)
            cover = await media_store.describe(args.cover_asset_id, owner=owner)
            if video.mime_type != 'video/mp4' or cover.mime_type not in {
                'image/png', 'image/jpeg', 'image/webp',
            }:
                raise AgentError('BILIBILI_INVALID_MEDIA', status_code=422)
        dynamic_images = ()
        if tool == 'bilibili_publish_dynamic' and isinstance(args, DynamicPublishArguments):
            if media_store is None:
                raise AgentError('MEDIA_NOT_INITIALIZED', status_code=503)
            dynamic_images = await prepare_dynamic_images(media_store, args.image_asset_ids, owner)
        async with self._write_lock:
            account = await self.require_account()
            signature = json.dumps([account.uid, owner.model_dump(), request_id, tool,
                                    args.model_dump()], sort_keys=True, ensure_ascii=False)
            key = hashlib.sha256(signature.encode()).hexdigest()
            cached = await self.store.begin(key, account.uid, tool)
            if cached is not None:
                return cached
            if tool == 'bilibili_reply_comment' and isinstance(args, ReplyArguments):
                try:
                    result = await self.client.reply(account, args)
                except AgentError as error:
                    if error.code in {'BILIBILI_NETWORK_ERROR', 'BILIBILI_INVALID_RESPONSE'}:
                        # 响应丢失不能证明评论未发送，保留占位并要求先核对。
                        raise AgentError('BILIBILI_RESULT_UNKNOWN', status_code=409) from None
                    raise
            elif tool == 'bilibili_publish_video' and isinstance(args, PublishArguments):
                async with media_store.open_content(args.video_asset_id, owner=owner) as stream:
                    async with media_store.open_content(args.cover_asset_id, owner=owner) as image:
                        cover_data = await finish_database_write(image.read)
                    result = await publish_video(self.client, account, args, stream,
                                                 video.size_bytes, cover_data, cover.mime_type)
            elif tool == 'bilibili_publish_dynamic' and isinstance(args, DynamicPublishArguments):
                result = await publish_dynamic(self.client, account, args, dynamic_images)
            else:
                raise AgentError('AGENT_TOOL_UNAUTHORIZED', status_code=403)
            try:
                await self.store.complete(key, result)
            except Exception:
                raise AgentError('BILIBILI_RESULT_UNKNOWN', status_code=409) from None
            return result
