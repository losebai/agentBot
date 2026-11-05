"""装配统一包中的 NoneBot 机器人、管理 API 和静态界面。"""

import os
from pathlib import Path

import nonebot
from fastapi import FastAPI

from agent_bot.admin.application_runtime import ApplicationRuntimeProvider
from agent_bot.admin.config import AdminConfig
from agent_bot.admin.models import STARTUP_SETTING_NAMES
from agent_bot.admin.routes import install_admin
from agent_bot.bootstrap import (
    inject_runtime_config,
    nonebot_init_kwargs,
    scrub_runtime_environment,
)
from agent_bot.core.application import assemble_runtime
from agent_bot.napcat.supervisor import NapCatSupervisor
from agent_bot.settings import VersionedSettings
from agent_bot.startup_settings import StartupSettingsStore


def create_application(
    stored: VersionedSettings, admin: AdminConfig, *, napcat: NapCatSupervisor | None = None,
    standalone_plugin: str | None = None,
) -> FastAPI:
    """独立进程只在内存固定插件选择；桌面可注入所属托管上下文。"""
    if standalone_plugin is not None:
        from agent_bot.plugins.catalog import BUILTIN_PLUGIN_IDS

        if standalone_plugin not in BUILTIN_PLUGIN_IDS:
            raise ValueError('unknown standalone plugin ID')
        stored = VersionedSettings(stored.config.model_copy(update={
            'app_enabled_plugins': [standalone_plugin],
        }), stored.revision)
    if "fastapi" not in stored.config.driver:
        raise ValueError("taobot management requires the FastAPI driver")
    # 环境字段随后为 NoneBot 安全清理，文件编辑能力须先捕获覆盖关系，防止虚假开放编辑。
    # 原生字段仍需参与只读快照；HTTP 写权限由 SettingsService 单独拒绝。
    startup = StartupSettingsStore(Path.cwd(), STARTUP_SETTING_NAMES)
    scrub_runtime_environment()
    nonebot.init(_env_file=(os.devnull,), **nonebot_init_kwargs(stored.config))
    driver = nonebot.get_driver()
    inject_runtime_config(driver.config, stored.config, revision=stored.revision)
    app = driver.server_app
    if not isinstance(app, FastAPI):
        raise ValueError("taobot management requires a FastAPI application")
    app.state.napcat = napcat
    if standalone_plugin is None:
        runtime, plugins = assemble_runtime(stored, driver, app)
    else:
        runtime, plugins = assemble_runtime(
            stored, driver, app, standalone_plugin=standalone_plugin,
        )
    app.state.runtime, app.state.plugins = runtime, plugins
    install_admin(
        app, runtime.settings_store,
        ApplicationRuntimeProvider(runtime, plugins, startup_settings_store=startup), admin,
    )
    from agent_bot.static import install_static

    install_static(app)
    return app
