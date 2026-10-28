"""独立插件命令固定进程能力，不修改主服务的配置和目录。"""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.unit.test_plugin_settings import valid_config

from agent_bot.settings import VersionedSettings


def test_plugin_command_dispatches_without_default_service(monkeypatch):
    """显式插件命令不经过默认全插件启动入口。"""
    from agent_bot import plugin_cli
    from agent_bot.__main__ import main

    run = Mock(return_value=0)
    monkeypatch.setattr(plugin_cli, 'run', run)
    assert main(['plugin', 'list']) == 0
    run.assert_called_once_with(['list'])


def test_plugin_list_is_offline(capsys):
    """列出目录不初始化数据库、不加载插件实例。"""
    from agent_bot.plugin_cli import run

    assert run(['list']) == 0
    text = capsys.readouterr().out
    assert all(name in text for name in ('qqbot', 'comfy', 'bilibili'))


def test_plugin_run_uses_own_directory_and_ephemeral_port(monkeypatch, tmp_path):
    """独立进程从指定目录启动，CLI 端口不写回文件或调用者环境。"""
    from agent_bot import __main__, plugin_cli

    original = Path.cwd()
    monkeypatch.setenv('PORT', '8080')
    directory = tmp_path / 'comfy'

    def start(*, standalone_plugin):
        """代替阻塞的服务循环，检查真正启动边界。"""
        assert standalone_plugin == 'comfy'
        assert Path.cwd() == directory
        assert os.environ['PORT'] == '8189'
        return 0

    monkeypatch.setattr(__main__, 'start_service', start)
    assert plugin_cli.run(['run', 'comfy', '--workdir', str(directory), '--port', '8189']) == 0
    assert Path.cwd() == original
    assert os.environ['PORT'] == '8080'
    assert not (directory / '.env').exists()


@pytest.mark.parametrize('args', [
    ['run', 'unknown', '--workdir', 'unused'],
    ['run', 'comfy'],
    ['run', 'comfy', '--workdir', 'unused', '--port', '0'],
    ['run', 'comfy', '--workdir', 'unused', '--port', '65536'],
])
def test_invalid_plugin_arguments_fail_before_initialization(args):
    """未知插件和端口在创建工作目录或加载数据库前拒绝。"""
    from agent_bot.plugin_cli import run

    with pytest.raises(SystemExit) as error:
        run(args)
    assert error.value.code == 2


def test_standalone_application_pins_selection_without_saving(monkeypatch, tmp_path):
    """显式进程选择不借用数据库中的其他插件，不写入运行配置。"""
    import nonebot
    from fastapi import FastAPI

    from agent_bot import application

    monkeypatch.chdir(tmp_path)
    config = valid_config(app_enabled_plugins=['qqbot', 'bilibili'])
    stored = VersionedSettings(config, 7)
    driver = SimpleNamespace(server_app=FastAPI(), config=SimpleNamespace())
    monkeypatch.setattr(nonebot, 'init', Mock())
    monkeypatch.setattr(nonebot, 'get_driver', lambda: driver)
    assembled = Mock(return_value=(Mock(), Mock()))
    monkeypatch.setattr(application, 'assemble_runtime', assembled)
    monkeypatch.setattr(application, 'install_admin', Mock())
    from agent_bot.admin.config import AdminConfig

    application.create_application(stored, AdminConfig(token=''), standalone_plugin='comfy')
    selected = assembled.call_args.args[0]
    assert selected.config.app_enabled_plugins == ['comfy']
    assert selected.revision == 7
    assert stored.config.app_enabled_plugins == ['qqbot', 'bilibili']
    assert assembled.call_args.kwargs['standalone_plugin'] == 'comfy'
