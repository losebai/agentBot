"""机器人名称由管理配置保存，热刷新同步插件与 OneBot 的触发规则。"""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import nonebot
from nonebot.adapters.onebot.v11.bot import _check_nickname
from test_configuration_layers import config

from agent_bot.admin.models import SettingsUpdate
from agent_bot.admin.settings import SettingsService
from agent_bot.bootstrap import load_startup_settings
from agent_bot.config import RuntimeConfig
from agent_bot.database import initialize_database
from agent_bot.settings import DatabaseParams, SettingsStore


def test_names_are_editable_and_persisted_without_restart(tmp_path: Path) -> None:
    """管理端可保存多个触发名称，版本约束保留且不产生重启提示。"""
    params = DatabaseParams(backend='sqlite', sqlite_path=str(tmp_path / 'names.sqlite3'))
    initialize_database(params)
    initial = config()
    store = SettingsStore(params, static_config=initial)
    store.initialize(initial)
    manager = SettingsService(store, SimpleNamespace(config=initial, revision=1))
    field = next(row for row in manager.read().fields if row.name == 'nickname')
    assert field.scope == 'runtime' and not field.read_only and not field.restart_required
    snapshot = manager.update(SettingsUpdate(
        expected_revision=1, values={'nickname': [' 桃桃 ', '小桃', '桃桃']},
    ))
    assert store.load().config.nickname == {'桃桃', '小桃'}
    assert snapshot.database_revision == 2 and not snapshot.restart_required_fields
    manager.update(SettingsUpdate(expected_revision=2, values={'nickname': []}))
    assert store.load().config.nickname == set()


def test_saved_names_are_not_overridden_by_environment(tmp_path: Path, monkeypatch) -> None:
    """旧 NICKNAME 环境值不覆盖数据库中的名称配置。"""
    params = DatabaseParams(backend='sqlite', sqlite_path=str(tmp_path / 'names.sqlite3'))
    initialize_database(params)
    initial = config(nickname={'桃桃'})
    SettingsStore(params, static_config=config()).initialize(initial)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('CHAT_DB_BACKEND', 'sqlite')
    monkeypatch.setenv('CHAT_DB_SQLITE_PATH', params.sqlite_path)
    monkeypatch.setenv(
        'ONEBOT_V11_ACCESS_TOKEN', initial.onebot_v11_access_token.get_secret_value(),
    )
    monkeypatch.setenv('CHAT_SESSION_SECRET', initial.chat_session_secret.get_secret_value())
    monkeypatch.setenv('NICKNAME', '["错误的旧名称"]')
    assert load_startup_settings().config.nickname == {'桃桃'}


def test_name_change_is_applied_by_runtime_resolver() -> None:
    """名称变更可以进入新运行快照，无需等待重启。"""
    from agent_bot.plugin.runtime import effective_runtime_config

    current = config(nickname={'旧名称'})
    candidate = config(nickname={'新名称'})
    effective, pending = effective_runtime_config(candidate, current)
    assert effective.nickname == {'新名称'} and not pending


async def test_publishing_names_updates_adapter_and_plugin_rules(chat_plugin) -> None:
    """新名称触发且旧名称失效，避免适配器把旧名称残留为 to_me。"""
    from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message

    def group_event(text: str, *, to_me: bool):
        """构造未经适配器名称识别的群消息，不连接 QQ。"""
        return GroupMessageEvent(
            time=1, self_id=90001, post_type='message', sub_type='normal', user_id=10001,
            message_type='group', message_id=2, message=Message(text),
            original_message=Message(text), raw_message=text, font=0,
            sender={'user_id': 10001, 'nickname': 'tester', 'role': 'member'},
            group_id=20001, to_me=to_me,
        )

    original = chat_plugin._runtime_state
    driver = nonebot.get_driver()
    original_names = driver.config.nickname
    changed = replace(original, config=RuntimeConfig.model_validate({
        **original.config.model_dump(), 'nickname': {'桃桃', '小桃'},
    }))
    try:
        chat_plugin._publish_runtime_state(changed)
        assert driver.config.nickname == {'桃桃', '小桃'}
        event = group_event('小桃，帮我看看', to_me=False)
        _check_nickname(SimpleNamespace(config=driver.config), event)
        assert event.is_tome()
        assert await chat_plugin._targets_natural_chat(event)
        removed = replace(
            changed, config=changed.config.model_copy(update={'nickname': {'新名字'}}),
        )
        chat_plugin._publish_runtime_state(removed)
        event = group_event('桃桃，帮我看看', to_me=False)
        _check_nickname(SimpleNamespace(config=driver.config), event)
        assert not event.is_tome()
        assert not await chat_plugin._targets_natural_chat(event)
        assert await chat_plugin._targets_natural_chat(group_event('请问新名字在吗', to_me=False))
    finally:
        chat_plugin._publish_runtime_state(original)
        driver.config.nickname = original_names
