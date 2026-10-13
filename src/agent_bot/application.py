"""装配统一包中的 NoneBot 机器人、管理 API 和静态界面。"""

import os

import nonebot
from fastapi import FastAPI
from nonebot.adapters.onebot.v11 import Adapter

from agent_bot.admin.config import AdminConfig
from agent_bot.admin.routes import install_admin
from agent_bot.admin.runtime import PluginRuntimeProvider
from agent_bot.bootstrap import (
    inject_runtime_config,
    nonebot_init_kwargs,
    scrub_runtime_environment,
    validate_onebot_driver,
)
from agent_bot.settings import VersionedSettings


def create_application(stored: VersionedSettings, admin: AdminConfig) -> FastAPI:
    """先注册机器人生命周期，再添加管理接口及静态界面。"""
    if "fastapi" not in stored.config.driver:
        raise ValueError("agentBot management requires the FastAPI driver")
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

    install_admin(app, plugin.get_settings_store(), PluginRuntimeProvider(plugin), admin)
    from agent_bot.static import install_static

    install_static(app)
    return app
