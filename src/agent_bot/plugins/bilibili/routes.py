"""B 站管理操作继承宿主认证与租约，二维码响应禁止缓存。"""

from typing import Any

from fastapi import APIRouter, Response
from pydantic import Field

from agent_bot.agent.errors import AgentError
from agent_bot.media.contracts import MediaOwner
from agent_bot.plugins.bilibili.contracts import DynamicPublishArguments
from agent_bot.plugins.bilibili.service import BilibiliService


class PublishDynamicRequest(DynamicPublishArguments):
    """HTTP 管理端显式提供上传会话和防重请求号；二者不进入模型参数。"""

    session_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
    request_id: str = Field(min_length=1, max_length=128)


def install_bilibili_routes(router: APIRouter, runtime: Any) -> None:
    """路由永远存在，但不能通过请求隐式加载禁用的插件。"""
    def service() -> BilibiliService:
        """仅访问运行中的插件，测试及非桌面模式不另建平台连接。"""
        manager = getattr(runtime, 'plugins', None)
        plugin = manager.running_plugin('bilibili') if manager is not None else None
        if plugin is None or plugin.service is None:
            raise AgentError('BILIBILI_DISABLED', status_code=503)
        return plugin.service

    @router.get('/bilibili/status')
    async def status(response: Response) -> dict:
        """账号状态不能作为浏览器或共享代理缓存。"""
        response.headers['Cache-Control'] = 'no-store'
        async with runtime.lease():
            return await service().status()

    @router.post('/bilibili/login/qr')
    async def generate(response: Response) -> dict:
        """必须由管理页显式申请二维码，不在插件启动时生成。"""
        response.headers['Cache-Control'] = 'no-store'
        async with runtime.lease():
            return await service().generate_qr()

    @router.get('/bilibili/login/qr/{session_id}')
    async def poll(session_id: str, response: Response) -> dict:
        """本地不透明会话 ID 替代平台轮询凭据。"""
        response.headers['Cache-Control'] = 'no-store'
        async with runtime.lease():
            return await service().poll_qr(session_id)

    @router.delete('/bilibili/login')
    async def clear(response: Response) -> dict:
        """只清除本地登录，不能声称远端所有设备退出。"""
        response.headers['Cache-Control'] = 'no-store'
        async with runtime.lease():
            await service().clear()
            return {'cleared': True}

    @router.post('/bilibili/dynamics', status_code=201)
    async def publish(body: PublishDynamicRequest, response: Response) -> dict:
        """认证管理员发布图文，复用图片上传归属和同一持久化防重业务路径。"""
        response.headers['Cache-Control'] = 'no-store'
        async with runtime.lease():
            current = service()
            resources = getattr(runtime, 'media_resources', None)
            if resources is None:
                raise AgentError('MEDIA_NOT_INITIALIZED', status_code=503)
            resources.require_ready()
            args = DynamicPublishArguments.model_validate(body.model_dump(
                exclude={'session_id', 'request_id'},
            ))
            return await current.write(
                'bilibili_publish_dynamic', args,
                owner=MediaOwner(entry='admin', session_id='admin_' + body.session_id),
                request_id=body.request_id, media_store=resources.store,
            )
