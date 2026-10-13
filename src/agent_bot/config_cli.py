"""离线数据库设置管理命令。"""

from __future__ import annotations

import argparse
import getpass as getpass_module
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_bot.bootstrap import (
    database_params,
    load_bootstrap_config,
    load_legacy_runtime_config,
    load_static_config,
)
from agent_bot.cli_errors import describe_config_error
from agent_bot.config import CONFIG_DESCRIPTIONS, RuntimeConfig
from agent_bot.settings import (
    SENSITIVE_COLLECTION_SETTING_NAMES,
    SENSITIVE_SETTING_NAMES,
    SettingsStore,
    runtime_config_to_plain_values,
)
from agent_bot.static_config import STATIC_SETTING_NAMES, StaticSettingError, static_values

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
        if arguments.command == "init":
            return _initialize_defaults(env_file)
        if arguments.command == "export-static":
            return _export_static(env_file, arguments.output)
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
        print(f"设置命令失败：{describe_config_error(exc)}")
        return 1
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chat settings")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("import-env")
    commands.add_parser("init")
    export_parser = commands.add_parser("export-static")
    export_parser.add_argument("output")
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
    store = SettingsStore(
        database_params(bootstrap), static_config=load_static_config(env_file=env_file),
    )
    stored = store.initialize(config)
    _print_saved(stored.revision, imported=True)
    return 0


def _initialize_defaults(env_file: str | tuple[str, ...] | None) -> int:
    """新安装只写入默认运行值，静态参数与秘密必须由用户显式提供。"""
    bootstrap = load_bootstrap_config(env_file=env_file)
    static = load_static_config(env_file=env_file)
    store = SettingsStore(database_params(bootstrap), static_config=static)
    stored = store.initialize(RuntimeConfig.model_validate(static_values(static)))
    _print_saved(stored.revision, imported=True)
    return 0


def _export_static(env_file: str | tuple[str, ...] | None, output: str) -> int:
    """停服期间导出历史静态值，禁止覆盖文件或向终端输出任何配置值。"""
    bootstrap = load_bootstrap_config(env_file=env_file)
    values = SettingsStore(database_params(bootstrap)).export_static_values()
    target = Path(output)
    # 独占创建并限制 POSIX 权限；Windows 权限由部署目录 ACL 管理。
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write("# 历史静态配置导出，含敏感凭据；停服期间合并到启动配置，勿提交。\n")
        for name, value in sorted(values.items()):
            stream.write(f"{name.upper()}={_dotenv_value(value)}\n")
    print(f"静态配置已导出至 {target}；文件包含敏感凭据，请在停服期间合并并限制访问。")
    return 0


def _dotenv_value(value: Any) -> str:
    """分别编码 dotenv 标量和 JSON 集合，保留历史密钥与命令的全部字面字符。"""
    if value is None:
        return ""
    if isinstance(value, str):
        # dotenv 不解码 JSON 的 Unicode 转义，标量必须使用其原生转义规则。
        escapes = {"\\": "\\\\", '"': '\\"', "\a": "\\a", "\b": "\\b",
                   "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\v": "\\v"}
        return '"' + value.translate(str.maketrans(escapes)) + '"'
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, (list, dict)):
        # dotenv 单引号会解码反斜线，必须先保护 JSON 自己的转义。
        return "'" + encoded.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return encoded


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
    print("运行配置以数据库为准；静态配置来自启动文件，只读且修改后需重启。")
    return 0


def _set_setting(
    env_file: str | tuple[str, ...] | None,
    env_name: str,
    raw_value: str,
) -> int:
    """允许显式修复无效旧快照，但写回前仍验证完整配置。"""
    field_name = _field_name(env_name)
    if field_name in SENSITIVE_SETTING_NAMES:
        raise ValueError("sensitive settings require a protected command")
    values, revision, store = _load_values_store(env_file)
    values[field_name] = _parse_value(raw_value)
    saved = store.save(values, expected_revision=revision)
    _print_saved(saved.revision)
    return 0


def _set_secrets(
    env_file: str | tuple[str, ...] | None,
    env_names: Sequence[str],
) -> int:
    """秘密仅经隐藏输入读取，修复旧快照时保留整体验证和版本冲突检查。"""
    field_names = [_field_name(name) for name in env_names]
    if any(name not in SENSITIVE_SETTING_NAMES for name in field_names):
        raise ValueError("setting is not sensitive")
    values, revision, store = _load_values_store(env_file)
    for env_name, field_name in zip(env_names, field_names, strict=True):
        raw_value = getpass(f"请输入 {env_name} (不会回显)：")
        values[field_name] = (
            _parse_value(raw_value)
            if field_name in SENSITIVE_COLLECTION_SETTING_NAMES
            else raw_value
        )
    saved = store.save(values, expected_revision=revision)
    _print_saved(saved.revision)
    return 0


def _clear_settings(
    env_file: str | tuple[str, ...] | None,
    env_names: Sequence[str],
) -> int:
    """只清空可选字段，候选快照不满足跨字段约束时拒绝写入。"""
    field_names = [_field_name(name) for name in env_names]
    if any(name not in _OPTIONAL_SETTING_NAMES for name in field_names):
        raise ValueError("only optional settings can be cleared")
    values, revision, store = _load_values_store(env_file)
    for field_name in field_names:
        values[field_name] = None
    saved = store.save(values, expected_revision=revision)
    _print_saved(saved.revision)
    return 0


def _load_store(
    env_file: str | tuple[str, ...] | None,
) -> tuple[Any, SettingsStore]:
    """查看配置时使用已验证快照，避免将无效配置展示为可运行状态。"""
    bootstrap = load_bootstrap_config(env_file=env_file)
    store = SettingsStore(
        database_params(bootstrap), static_config=load_static_config(env_file=env_file),
    )
    return store.load(), store


def _load_values_store(
    env_file: str | tuple[str, ...] | None,
) -> tuple[dict[str, Any], int, SettingsStore]:
    """修改命令读取原始值用于修复，不绕过保存时的权限、校验或版本锁。"""
    bootstrap = load_bootstrap_config(env_file=env_file)
    store = SettingsStore(
        database_params(bootstrap), static_config=load_static_config(env_file=env_file),
    )
    values, revision = store.load_values()
    return values, revision, store


def _field_name(env_name: str) -> str:
    """离线运行配置命令也禁止改写静态字段。"""
    try:
        name = _FIELD_BY_ENV_NAME[env_name.strip().upper()]
    except KeyError as exc:
        raise ValueError("unknown setting name") from exc
    if name in STATIC_SETTING_NAMES:
        raise StaticSettingError("static settings require editing startup configuration")
    return name


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
        "静态字段请修改启动文件并重启。"
    )
