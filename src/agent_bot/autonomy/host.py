"""独立Agent HTTP宿主：提供对话界面和后台循环，不创建机器人或QQ连接。"""

import asyncio
from contextlib import asynccontextmanager
from importlib.metadata import distribution
from types import SimpleNamespace
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from agent_bot.admin.auth import require_token
from agent_bot.admin.config import AdminConfig
from agent_bot.admin.errors import classify_api_error
from agent_bot.admin.http import ManagementMiddleware
from agent_bot.admin.models import SettingsUpdate
from agent_bot.admin.settings import SettingsService
from agent_bot.admin.validation import describe_validation_error
from agent_bot.ai.catalog import AiCatalog
from agent_bot.ai.errors import AiError
from agent_bot.autonomy.routes import install_autonomy_routes
from agent_bot.autonomy.worker import serve
from agent_bot.bootstrap import database_params, load_bootstrap_config, load_startup_settings
from agent_bot.database_pool import create_database_pool, finish_database_write
from agent_bot.integrations.tagged_memory_store import TaggedMemoryStore
from agent_bot.settings import SettingsRevisionConflictError, SettingsStore


def create_host() -> FastAPI:
    """启动前完成迁移；应用关闭取消循环并等待在途事务和连接池释放。"""
    @asynccontextmanager
    async def lifespan(app):
        """使用统一配置权威和数据库，独立循环的退出由宿主统一回收。"""
        params = database_params(load_bootstrap_config())
        stored = await finish_database_write(load_startup_settings)
        pool = create_database_pool(params)
        runtime = SimpleNamespace(config=stored.config, revision=stored.revision, local_only=True)

        @asynccontextmanager
        async def lease(*, read_only=False):
            """独立宿主没有机器人热切换，生命周期内共享连接池。"""
            yield runtime

        runtime.lease = lease
        runtime.tagged_memory_store = TaggedMemoryStore(
            params, connect=pool.connect,
            secret=stored.config.chat_session_secret.get_secret_value())
        store = SettingsStore(params, connect=pool.connect, static_config=stored.config)
        app.state.runtime = runtime
        app.state.settings = SettingsService(store, runtime)
        app.state.catalog = AiCatalog(params, connect=pool.connect)
        ready = asyncio.Event()
        task = asyncio.create_task(
            serve(ready=ready, local_only=True), name='independent-agent-loop')
        app.state.worker = task
        waiting = asyncio.create_task(ready.wait())
        try:
            await asyncio.wait((task, waiting), return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                await task
                raise RuntimeError('independent worker stopped before ready')
            yield
        finally:
            waiting.cancel()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            finally:
                await pool.aclose()

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(ManagementMiddleware)
    admin = AdminConfig.from_environment()

    async def authorize(authorization: Annotated[str | None, Header()] = None):
        """复用管理员认证，独立界面不提供任意进程或命令执行能力。"""
        require_token(authorization, admin.token.get_secret_value())

    @app.exception_handler(AiError)
    async def domain_error(request, error):
        """只返回受控错误码，数据库或模型的原始异常不得进入响应。"""
        status, code, message = classify_api_error(error)
        return JSONResponse({'code': code, 'message': message}, status_code=status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        """隐藏Pydantic原始输入，非法配置不能把密钥或正文回显到错误日志。"""
        return JSONResponse({'code': 'INVALID_REQUEST',
                             'message': describe_validation_error(error, request)}, status_code=422)

    @app.exception_handler(SettingsRevisionConflictError)
    async def settings_conflict(request, error):
        """并发暂停修改返回冲突，页面须刷新后重新操作。"""
        return JSONResponse({'code': 'SETTINGS_CONFLICT', 'message': '配置版本已变化，请刷新'},
                            status_code=409)

    @app.get('/api/v1/auth')
    async def auth():
        """公开登录模式及独立宿主标识，不公开凭据。"""
        return {'password_required': bool(admin.token.get_secret_value()), 'standalone': True}

    router = APIRouter(prefix='/api/v1', dependencies=[Depends(authorize)])

    @router.get('/settings')
    async def settings():
        """策略页面读取脱敏配置快照，无需平台适配器在线。"""
        return await finish_database_write(app.state.settings.read)

    @router.patch('/settings')
    async def update_settings(body: SettingsUpdate):
        """独立网页只允许宿主总暂停和事件归档，不能修改平台或进程配置。"""
        allowed = {'chat_active_actions_paused', 'chat_autonomy_capture_enabled'}
        if not body.values.model_fields_set <= allowed:
            raise AiError('AUTONOMY_ACTION_NOT_ALLOWED', status_code=403)
        return await finish_database_write(lambda: app.state.settings.update(body))

    @router.get('/ai/models')
    async def models(cursor: str | None = None):
        """只读取已配置模型目录，不发现或调用上游模型。"""
        return await app.state.catalog.list_models(limit=100, cursor=cursor)

    class RuntimeProxy:
        """路由注册时尚未启动，通过请求时属性转交当前独立运行时。"""

        def __getattr__(self, name):
            """仅转发内部代码定义的运行时字段。"""
            return getattr(app.state.runtime, name)

    install_autonomy_routes(router, RuntimeProxy())
    app.include_router(router)

    @app.get('/')
    async def home():
        """首页进入独立界面，不装配机器人管理页面。"""
        return RedirectResponse('/index.html?standalone=1')

    @app.get('/health/ready')
    async def ready():
        """循环提前退出时拒绝报告就绪，数据库失败不能被HTTP掩盖。"""
        return JSONResponse({'ready': not app.state.worker.done()},
                            status_code=503 if app.state.worker.done() else 200)

    static = distribution('agent-bot').locate_file('agent_bot/static')
    app.mount('/', StaticFiles(directory=static, html=True))
    return app
