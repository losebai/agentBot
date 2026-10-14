"""装配统一包中的 NoneBot 机器人、管理 API 和静态界面。"""

import os
from pathlib import Path

import nonebot
from fastapi import FastAPI
from nonebot.adapters.onebot.v11 import Adapter

from agent_bot.admin.config import AdminConfig
from agent_bot.admin.models import STARTUP_SETTING_NAMES
from agent_bot.admin.routes import install_admin
from agent_bot.admin.runtime import PluginRuntimeProvider
from agent_bot.bootstrap import (
    inject_runtime_config,
    nonebot_init_kwargs,
    scrub_runtime_environment,
    validate_onebot_driver,
)
from agent_bot.settings import VersionedSettings
from agent_bot.startup_settings import StartupSettingsStore


def create_application(stored: VersionedSettings, admin: AdminConfig) -> FastAPI:
    """先注册机器人生命周期，再添加管理接口及静态界面。"""
    if "fastapi" not in stored.config.driver:
        raise ValueError("agentBot management requires the FastAPI driver")
    # 环境字段随后为 NoneBot 安全清理，文件编辑能力须先捕获覆盖关系，防止虚假开放编辑。
    startup = StartupSettingsStore(Path.cwd(), STARTUP_SETTING_NAMES)
    scrub_runtime_environment()
    nonebot.init(_env_file=(os.devnull,), **nonebot_init_kwargs(stored.config))
    driver = nonebot.get_driver()
    validate_onebot_driver(driver, stored.config)
    inject_runtime_config(driver.config, stored.config, revision=stored.revision)
    driver.register_adapter(Adapter)
    app = driver.server_app
    if not isinstance(app, FastAPI):
        raise ValueError("agentBot management requires a FastAPI application")
    if nonebot.load_plugin("agent_bot.plugin") is None:
        raise RuntimeError("failed to load the Chat plugin")
    from agent_bot.plugin import application as plugin

    install_admin(app, plugin.get_settings_store(),
                  PluginRuntimeProvider(plugin, startup_settings_store=startup), admin)
    from agent_bot.static import install_static

    install_static(app)
    return app
