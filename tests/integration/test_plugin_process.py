"""真实本地插件子进程并行验收，只使用独立临时 SQLite 和回环 HTTP。"""

import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from agent_bot.static_config import STATIC_SETTING_NAMES


def seed_existing_directory(directory: Path) -> None:
    """预置其他插件选择，证明独立启动不会把共享选择写成当前目标。"""
    from agent_bot.config import RuntimeConfig
    from agent_bot.database import initialize_database
    from agent_bot.settings import DatabaseParams, SettingsStore

    config = RuntimeConfig(
        app_enabled_plugins=['qqbot', 'bilibili'],
        onebot_v11_access_token='offline-onebot-' + 'o' * 40,
        chat_session_secret='offline-session-' + 's' * 40,
    )
    (directory / '.env').write_text(
        'DRIVER=~fastapi+~websockets\nCHAT_DB_BACKEND=sqlite\n'
        'ONEBOT_V11_ACCESS_TOKEN=' + config.onebot_v11_access_token.get_secret_value()
        + '\nCHAT_SESSION_SECRET=' + config.chat_session_secret.get_secret_value() + '\n',
        encoding='utf-8',
    )
    (directory / 'data').mkdir()
    params = DatabaseParams(backend='sqlite', sqlite_path=str(directory / 'data/agent_bot.sqlite3'))
    initialize_database(params)
    SettingsStore(params, static_config=config).initialize(config)


def saved_plugins(directory: Path) -> list[str]:
    """只读取测试自己的临时库，不使用开发者连接参数。"""
    with sqlite3.connect(directory / 'data/agent_bot.sqlite3') as connection:
        row = connection.execute("SELECT config_value FROM chat_setting "
                                 "WHERE config_key='app_enabled_plugins'").fetchone()
        return json.loads(row[0])


def unused_port() -> int:
    """只分配回环测试端口；被并行占用时由真实监听明确失败。"""
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0))
        return server.getsockname()[1]


async def ready(client: httpx.AsyncClient, process: subprocess.Popen) -> None:
    """有界等待子进程健康，不向外部服务发送请求或执行自动重启。"""
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        assert process.poll() is None, '插件子进程提前退出，请检查临时工作目录日志'
        try:
            result = await client.get('/health/ready')
            if result.status_code == 200:
                return
        except httpx.TransportError:
            pass
        await asyncio.sleep(0.1)
    raise AssertionError('插件子进程未在 40 秒内就绪')


@pytest.mark.integration
async def test_plugins_run_as_separate_processes_without_global_selection_changes(tmp_path):
    """三个 CLI 进程各自服务独立插件，停止其中一个不影响另外两个。"""
    environment = {key: value for key, value in os.environ.items()
                   if key.casefold() not in STATIC_SETTING_NAMES
                   and not key.upper().startswith(('CHAT_DB_', 'CHAT_REDIS_', 'CHAT_QUEUE_',
                                                   'AGENT_BOT_', 'ONEBOT_'))}
    processes, logs, clients = [], [], []
    try:
        for plugin_id in ('comfy', 'bilibili', 'qqbot'):
            directory = tmp_path / plugin_id
            directory.mkdir()
            if plugin_id == 'comfy':
                await asyncio.to_thread(seed_existing_directory, directory)
            port = unused_port()
            log = (directory / 'worker.log').open('w', encoding='utf-8')
            logs.append(log)
            process = await asyncio.to_thread(
                subprocess.Popen,
                [sys.executable, '-m', 'agent_bot', 'plugin', 'run', plugin_id,
                 '--workdir', str(directory), '--port', str(port)],
                cwd=tmp_path, env=environment, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
            processes.append(process)
            client = httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', timeout=1)
            clients.append(client)
            await ready(client, process)
            plugins = (await client.get('/api/v1/plugins')).json()
            assert [item['id'] for item in plugins if item['enabled']] == [plugin_id]
            assert next(item for item in plugins if item['id'] == plugin_id)['status'] == 'running'
            assert not any(item['restart_required'] for item in plugins)
            settings = (await client.get('/api/v1/settings')).json()
            fields = {field['name']: field for field in settings['fields']}
            assert fields['app_enabled_plugins']['read_only']
            assert fields['port']['read_only']
            assert fields['port']['value'] == port
            assert (directory / 'data/queue.sqlite3').exists() == (plugin_id == 'qqbot')
            result = await client.patch('/api/v1/settings', json={
                'expected_revision': settings['database_revision'],
                'values': {'app_enabled_plugins': ['qqbot']},
            })
            assert result.status_code == 422
            before = (directory / '.env').read_bytes()
            result = await client.patch('/api/v1/settings', json={
                'expected_revision': settings['database_revision'],
                'startup_revision': settings['startup_revision'], 'values': {'port': 8099},
            })
            assert result.status_code == 422
            assert (directory / '.env').read_bytes() == before
            if plugin_id == 'comfy':
                assert await asyncio.to_thread(saved_plugins, directory) == ['qqbot', 'bilibili']
            workflows = await client.get('/api/v1/workflows')
            assert workflows.status_code == (200 if plugin_id == 'comfy' else 503)
        assert len({process.pid for process in processes}) == 3
        processes[0].terminate()
        await asyncio.to_thread(processes[0].wait, timeout=10)
        assert (await clients[1].get('/health/ready')).status_code == 200
        assert (await clients[2].get('/health/ready')).status_code == 200
        assert all((tmp_path / name / 'data/agent_bot.sqlite3').is_file()
                   for name in ('comfy', 'bilibili', 'qqbot'))
    finally:
        for client in clients:
            await client.aclose()
        for process in processes:
            if process.poll() is None:
                process.terminate()
                await asyncio.to_thread(process.wait, timeout=10)
        for log in logs:
            log.close()
