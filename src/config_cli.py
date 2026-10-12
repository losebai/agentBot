"""离线数据库设置管理命令。"""

from __future__ import annotations

import argparse
import getpass as getpass_module
import json
from collections.abc import Mapping, Sequence
from typing import Any

from agent_bot.bootstrap import (
    database_params,
    load_bootstrap_config,
    load_legacy_runtime_config,
)
from agent_bot.config import CONFIG_DESCRIPTIONS, RuntimeConfig
from agent_bot.settings import (
    SENSITIVE_COLLECTION_SETTING_NAMES,
    SENSITIVE_SETTING_NAMES,
    SettingsStore,
    runtime_config_to_plain_values,
)

_FIELD_BY_ENV_NAME = {name.upper(): name for name in RuntimeConfig.model_fields}
_ENV_NAME_BY_FIELD = {field: env_name for env_name, field in _FIELD_BY_ENV_NAME.items()}
_OPTIONAL_SETTING_NAMES = frozenset(
    name for name, field in RuntimeConfig.model_fields.items() if field.default is None
)


def getpass(prompt: str = "Value: ") -> str:
    """可替换的安全输入边界。"""

    return getpass_module.getpass(prompt)


def run(
    argv: Sequence[str],
    *,
    env_file: str | tuple[str, ...] | None = None,
) -> int:
    """执行离线设置子命令；任何配置值都不会出现在错误输出中。"""

    parser = _build_parser()
    try:
        arguments = parser.parse_args(list(argv))
        if arguments.command == "import-env":
            return _import_env(env_file)
        if arguments.command == "list":
            return _list_settings(env_file)
        if arguments.command == "set":
            return _set_setting(env_file, arguments.name, arguments.value)
        if arguments.command == "set-secret":
            return _set_secrets(env_file, arguments.names)
        if arguments.command == "clear":
            return _clear_settings(env_file, arguments.names)
    except (Exception, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code == 0:
            return 0
        print(f"设置命令失败：{type(exc).__name__}")
        return 1
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dispatch-chat settings")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("import-env")
    commands.add_parser("list")

    set_parser = commands.add_parser("set")
    set_parser.add_argument("name")
    set_parser.add_argument("value")

    secret_parser = commands.add_parser("set-secret")
    secret_parser.add_argument("names", nargs="+")

    clear_parser = commands.add_parser("clear")
    clear_parser.add_argument("names", nargs="+")
    return parser


def _import_env(
    env_file: str | tuple[str, ...] | None,
) -> int:
    bootstrap = load_bootstrap_config(env_file=env_file)
    config = load_legacy_runtime_config(env_file=env_file)
    store = SettingsStore(database_params(bootstrap))
    stored = store.initialize(config)
    _print_saved(stored.revision, imported=True)
    return 0


def _list_settings(
    env_file: str | tuple[str, ...] | None,
) -> int:
    stored, _ = _load_store(env_file)
    values = runtime_config_to_plain_values(stored.config)
    print(f"revision={stored.revision}")
    for field_name in RuntimeConfig.model_fields:
        env_name = _ENV_NAME_BY_FIELD[field_name]
        value = values[field_name]
        desc = CONFIG_DESCRIPTIONS.get(field_name, "")
        desc_str = f"  # {desc}" if desc else ""
        print(f"{env_name}={_display_value(field_name, value)}{desc_str}")
    print("数据库配置为唯一运行来源；运行中服务会自动刷新，启动期字段需重启。")
    return 0


def _set_setting(
    env_file: str | tuple[str, ...] | None,
    env_name: str,
    raw_value: str,
) -> int:
    field_name = _field_name(env_name)
    if field_name in SENSITIVE_SETTING_NAMES:
        raise ValueError("sensitive settings require a protected command")
    stored, store = _load_store(env_file)
    values = runtime_config_to_plain_values(stored.config)
    values[field_name] = _parse_value(raw_value)
    saved = store.save(values, expected_revision=stored.revision)
    _print_saved(saved.revision)
    return 0


def _set_secrets(
    env_file: str | tuple[str, ...] | None,
    env_names: Sequence[str],
) -> int:
    field_names = [_field_name(name) for name in env_names]
    if any(name not in SENSITIVE_SETTING_NAMES for name in field_names):
        raise ValueError("setting is not sensitive")
    stored, store = _load_store(env_file)
    values = runtime_config_to_plain_values(stored.config)
    for env_name, field_name in zip(env_names, field_names, strict=True):
        raw_value = getpass(f"请输入 {env_name} (不会回显)：")
        values[field_name] = (
            _parse_value(raw_value)
            if field_name in SENSITIVE_COLLECTION_SETTING_NAMES
            else raw_value
        )
    saved = store.save(values, expected_revision=stored.revision)
    _print_saved(saved.revision)
    return 0


def _clear_settings(
    env_file: str | tuple[str, ...] | None,
    env_names: Sequence[str],
) -> int:
    field_names = [_field_name(name) for name in env_names]
    if any(name not in _OPTIONAL_SETTING_NAMES for name in field_names):
        raise ValueError("only optional settings can be cleared")
    stored, store = _load_store(env_file)
    values = runtime_config_to_plain_values(stored.config)
    for field_name in field_names:
        values[field_name] = None
    saved = store.save(values, expected_revision=stored.revision)
    _print_saved(saved.revision)
    return 0


def _load_store(
    env_file: str | tuple[str, ...] | None,
) -> tuple[Any, SettingsStore]:
    bootstrap = load_bootstrap_config(env_file=env_file)
    store = SettingsStore(database_params(bootstrap))
    return store.load(), store


def _field_name(env_name: str) -> str:
    try:
        return _FIELD_BY_ENV_NAME[env_name.strip().upper()]
    except KeyError as exc:
        raise ValueError("unknown setting name") from exc


def _parse_value(raw_value: str) -> Any:
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError:
        return raw_value


def _display_value(field_name: str, value: Any) -> str:
    if field_name in SENSITIVE_SETTING_NAMES:
        if isinstance(value, list):
            return f"<count={len(value)}>"
        return "<empty>" if value is None or value == "" else "<set>"
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _print_saved(revision: int, *, imported: bool = False) -> None:
    action = "导入" if imported else "保存"
    print(
        f"配置已{action}，revision={revision}。运行中服务会自动刷新；"
        "启动期字段需重启 dispatch-chat。"
    )
