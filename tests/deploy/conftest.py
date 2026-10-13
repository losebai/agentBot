"""部署脚本测试只使用临时目录和命令替身，不启动机器人。"""

import pytest


@pytest.fixture(scope="session", autouse=True)
def _nonebot_init():
    """静态部署文件不需要 NoneBot 初始化或本地 .env。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def after_nonebot_init():
    """隔离插件加载，禁止连接数据库和 Redis。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def nonebug_init():
    """脚本测试不持有应用生命周期。"""
    yield
