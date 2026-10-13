from __future__ import annotations

import getpass
import json
import re
from pathlib import Path
from unittest.mock import Mock

import pytest

import agent_bot.bot_cli as entrypoint
from _fake_mysql import FakeMySQL
from agent_bot import config_cli

ONEBOT_TOKEN = "cli-onebot-token-that-is-longer-than-thirty-two-characters"
SESSION_SECRET = "cli-session-secret-that-is-longer-than-thirty-two-characters"
ROLE_SECRET = "cli-private-ai-token"
TTS_TOKEN = "cli-private-tts-token"
NARAKA_COOKIE = (
    "pkey=cli-private-pkey; x_heybox_id=556677; x_pkey=cli-private-x-pkey; "
    "x_xhh_tokenid=cli-private-token-id"
)
AUTO_PROMPT = "cli private scheduled announcement prompt"
ONEBOT_SECRET = "cli-private-onebot-signing-secret"
PRIVATE_NICKNAME = "cli-private-bot-nickname"
PRIVATE_WS_URL = "wss://cli-onebot.example.test/private-ws"
PRIVATE_API_URL = "https://cli-onebot.example.test/private-api"
QQ_VALUES = {
    "123456",
    "123457",
    "654321",
    "654322",
    "765432",
    "876543",
    "940000000000009",
}


def _legacy_values() -> dict[str, str]:
    """旧 MySQL 配置显式选择后端，避免被新的 SQLite 启动默认值改变。"""
    return {
        "CHAT_DB_BACKEND": "mysql",
        "CHAT_DB_HOST": "db.example.test",
        "CHAT_DB_PORT": "3306",
        "CHAT_DB_USER": "chat",
        "CHAT_DB_PASSWORD": "db-secret",
        "CHAT_DB_NAME": "chat",
        "DRIVER": "~fastapi",
        "HOST": "127.0.0.1",
        "PORT": "8765",
        "LOG_LEVEL": "INFO",
        "SUPERUSERS": '["940000000000009"]',
        "NICKNAME": f'["{PRIVATE_NICKNAME}"]',
        "COMMAND_START": '["/", "!"]',
        "ONEBOT_V11_ACCESS_TOKEN": ONEBOT_TOKEN,
        "ONEBOT_V11_SECRET": ONEBOT_SECRET,
        "ONEBOT_V11_WS_URLS": f'["{PRIVATE_WS_URL}"]',
        "ONEBOT_V11_API_ROOTS": f'{{"cli-bot":"{PRIVATE_API_URL}"}}',
        "CHAT_SESSION_SECRET": SESSION_SECRET,
        "CHAT_NARAKA_DEFAULT_ROLE_ID": ROLE_SECRET,
        "CHAT_TTS_URL": "https://cli-tts.example.test/v1/audio/speech",
        "CHAT_TTS_TOKEN": TTS_TOKEN,
        "CHAT_MODEL": "cli-test-model",
        "CHAT_SYSTEM_PROMPT": "cli supplemental prompt",
        "CHAT_ALLOWED_USERS": '["123456", "123457"]',
        "CHAT_ALLOWED_GROUPS": '["654321", "654322"]',
        "CHAT_AUTO_SPEAK_ENABLED": "true",
        "CHAT_AUTO_SPEAK_BOT_ID": "765432",
        "CHAT_AUTO_SPEAK_GROUPS": '["876543"]',
        "CHAT_AUTO_SPEAK_TRIGGER_MODE": "interval",
        "CHAT_AUTO_SPEAK_PROMPT": AUTO_PROMPT,
        "CHAT_AUTO_SPEAK_INITIAL_DELAY_SECONDS": "10",
        "CHAT_AUTO_SPEAK_INTERVAL_SECONDS": "60",
        "CHAT_MCP_ENABLED": "true",
        "CHAT_AUTO_SPEAK_MCP_CODE": "decision",
        "CHAT_GROUP_RECENT_CONTEXT_ENABLED": "true",
        "CHAT_NARAKA_COOKIE": NARAKA_COOKIE,
        "CHAT_NARAKA_HEYBOX_ID": "556677",
    }


def _write_env(path: Path, values: dict[str, str]) -> None:
    path.write_text(
        "\n".join(
            f"{name}={json.dumps(value, ensure_ascii=False)}" for name, value in values.items()
        ),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _never_read_real_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in _legacy_values():
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def legacy_env(tmp_path: Path) -> Path:
    env_file = tmp_path / "legacy.env"
    _write_env(env_file, _legacy_values())
    return env_file


@pytest.fixture(autouse=True)
def _inject_fake_store(fake_mysql: FakeMySQL) -> None:
    """让 CLI 命令使用内存 fake，而不是真实 MySQL。"""

    _ = fake_mysql  # fixture 已经把 pymysql.connect 替换为共享实例。


def _invoke(
    capsys: pytest.CaptureFixture[str],
    env_file: Path,
    *argv: str,
) -> tuple[int, str]:
    return_code = config_cli.run(list(argv), env_file=env_file)
    captured = capsys.readouterr()
    return return_code, captured.out + captured.err


def _initialize(
    capsys: pytest.CaptureFixture[str],
    env_file: Path,
) -> None:
    return_code, output = _invoke(capsys, env_file, "import-env")
    assert return_code == 0
    assert _revision(output) == 1
    assert _has_restart_notice(output)


def _revision(output: str) -> int:
    match = re.search(r"\brevision\s*(?:=|:)?\s*(\d+)\b", output, re.IGNORECASE)
    assert match is not None, output
    return int(match.group(1))


def _has_restart_notice(output: str) -> bool:
    normalized = output.casefold()
    return "restart" in normalized or "重启" in output


def _line_for(output: str, name: str) -> str:
    prefix = f"{name}="
    line = next(line.strip() for line in output.splitlines() if line.strip().startswith(prefix))
    return line.partition("  #")[0]




def test_import_env_initializes_once_without_printing_configuration(
    legacy_env: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    return_code, output = _invoke(capsys, legacy_env, "import-env")

    assert return_code == 0
    assert _revision(output) == 1
    assert _has_restart_notice(output)
    for private_value in (
        ONEBOT_TOKEN,
        SESSION_SECRET,
        ROLE_SECRET,
        NARAKA_COOKIE,
        AUTO_PROMPT,
        "cli-test-model",
        "cli supplemental prompt",
        *QQ_VALUES,
    ):
        assert private_value not in output

    return_code, duplicate_output = _invoke(capsys, legacy_env, "import-env")
    assert return_code != 0
    assert all(
        value not in duplicate_output for value in (ONEBOT_TOKEN, SESSION_SECRET, ROLE_SECRET)
    )

    return_code, listed = _invoke(capsys, legacy_env, "list")
    assert return_code == 0
    assert _revision(listed) == 1


def test_list_displays_normal_values_but_redacts_sensitive_values_and_qq_ids(
    legacy_env: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _initialize(capsys, legacy_env)

    return_code, output = _invoke(capsys, legacy_env, "list")

    assert return_code == 0
    assert "CHAT_MODEL=cli-test-model" in output
    assert "PORT=8765" in output
    for field in (
        "ONEBOT_V11_ACCESS_TOKEN",
        "CHAT_SESSION_SECRET",
        "CHAT_NARAKA_DEFAULT_ROLE_ID",
        "CHAT_TTS_TOKEN",
        "CHAT_NARAKA_COOKIE",
        "CHAT_NARAKA_HEYBOX_ID",
        "CHAT_NARAKA_DEFAULT_ROLE_ID",
        "CHAT_AUTO_SPEAK_BOT_ID",
        "CHAT_AUTO_SPEAK_PROMPT",
        "ONEBOT_V11_SECRET",
        "ONEBOT_V11_API_ROOTS",
    ):
        assert _line_for(output, field).endswith("<set>")
    for field in (
        "CHAT_ALLOWED_USERS",
        "CHAT_ALLOWED_GROUPS",
        "CHAT_AUTO_SPEAK_GROUPS",
        "SUPERUSERS",
        "NICKNAME",
        "ONEBOT_V11_WS_URLS",
    ):
        assert re.search(r"\b[12]\b", _line_for(output, field))
    for private_value in (
        ONEBOT_TOKEN,
        SESSION_SECRET,
        ROLE_SECRET,
        TTS_TOKEN,
        NARAKA_COOKIE,
        AUTO_PROMPT,
        ONEBOT_SECRET,
        PRIVATE_NICKNAME,
        PRIVATE_WS_URL,
        PRIVATE_API_URL,
        "556677",
        "cli_private_role",
    ):
        assert private_value not in output
    assert all(qq_id not in output for qq_id in QQ_VALUES)


def test_set_validates_entire_snapshot_and_invalid_change_keeps_revision(
    legacy_env: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _initialize(capsys, legacy_env)

    return_code, output = _invoke(capsys, legacy_env, "set", "CHAT_HISTORY_TURNS", "12")
    assert return_code == 0
    assert _revision(output) == 2
    assert _has_restart_notice(output)

    return_code, output = _invoke(capsys, legacy_env, "set", "CHAT_CHUNK_CHARS", "7000")
    assert return_code != 0

    return_code, listed = _invoke(capsys, legacy_env, "list")
    assert return_code == 0
    assert _revision(listed) == 2
    assert "CHAT_HISTORY_TURNS=12" in listed
    assert "CHAT_CHUNK_CHARS=1200" in listed


def test_set_updates_independent_image_generation_model(
    legacy_env: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _initialize(capsys, legacy_env)

    return_code, output = _invoke(
        capsys,
        legacy_env,
        "set",
        "CHAT_IMAGE_MODEL",
        "private-image-generation-model",
    )

    assert return_code == 0
    assert _revision(output) == 2
    return_code, listed = _invoke(capsys, legacy_env, "list")
    assert return_code == 0
    assert "CHAT_IMAGE_MODEL=private-image-generation-model" in listed


def test_set_and_list_keyword_at_replies_as_json(
    legacy_env: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _initialize(capsys, legacy_env)
    rules = '{"在吗":"我在。","帮助":"私聊直接发送内容，群聊请先 @机器人"}'

    return_code, output = _invoke(
        capsys,
        legacy_env,
        "set",
        "CHAT_KEYWORD_AT_REPLIES",
        rules,
    )

    assert return_code == 0
    assert _revision(output) == 2
    return_code, listed = _invoke(capsys, legacy_env, "list")
    assert return_code == 0
    assert _line_for(listed, "CHAT_KEYWORD_AT_REPLIES") == (
        "CHAT_KEYWORD_AT_REPLIES=" + rules
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CHAT_NOT_A_SETTING", "value-that-must-not-be-stored"),
        ("ONEBOT_V11_ACCESS_TOKEN", "value-that-must-not-be-printed-or-stored"),
        ("CHAT_TTS_TOKEN", "tts-token-must-use-hidden-input"),
        ("CHAT_ALLOWED_USERS", '["111111"]'),
        ("CHAT_AUTO_SPEAK_PROMPT", "private prompt passed through ordinary set"),
    ],
)
def test_set_rejects_unknown_and_sensitive_fields_without_revision_change(
    legacy_env: Path,
    capsys: pytest.CaptureFixture[str],
    name: str,
    value: str,
) -> None:
    _initialize(capsys, legacy_env)

    return_code, output = _invoke(capsys, legacy_env, "set", name, value)

    assert return_code != 0
    assert value not in output
    return_code, listed = _invoke(capsys, legacy_env, "list")
    assert return_code == 0
    assert _revision(listed) == 1


def test_set_secret_reads_getpass_and_never_echoes_value(
    legacy_env: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialize(capsys, legacy_env)
    new_secret = "replacement-private-ai-token"
    read_secret = Mock(return_value=new_secret)
    monkeypatch.setattr(getpass, "getpass", read_secret)
    if callable(getattr(config_cli, "getpass", None)):
        monkeypatch.setattr(config_cli, "getpass", read_secret)

    return_code, output = _invoke(capsys, legacy_env, "set-secret", "CHAT_NARAKA_DEFAULT_ROLE_ID")

    assert return_code == 0
    assert _revision(output) == 2
    assert _has_restart_notice(output)
    assert new_secret not in output
    read_secret.assert_called_once()
    return_code, listed = _invoke(capsys, legacy_env, "list")
    assert return_code == 0
    assert _line_for(listed, "CHAT_NARAKA_DEFAULT_ROLE_ID").endswith("<set>")
    assert new_secret not in listed


def test_set_secret_preserves_scalar_text_that_looks_like_json(
    legacy_env: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialize(capsys, legacy_env)
    numeric_token = "123456"
    monkeypatch.setattr(config_cli, "getpass", Mock(return_value=numeric_token))

    return_code, output = _invoke(
        capsys,
        legacy_env,
        "set-secret",
        "CHAT_NARAKA_DEFAULT_ROLE_ID",
    )

    assert return_code == 0
    assert _revision(output) == 2
    assert numeric_token not in output


def test_set_secret_updates_cross_validated_fields_atomically(
    legacy_env: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialize(capsys, legacy_env)
    assert (
        _invoke(
            capsys,
            legacy_env,
            "clear",
            "CHAT_NARAKA_COOKIE",
            "CHAT_NARAKA_HEYBOX_ID",
        )[0]
        == 0
    )
    replacement_cookie = (
        "pkey=replacement; x_heybox_id=778899; x_pkey=replacement; x_xhh_tokenid=replacement"
    )
    replacement_id = "778899"
    read_secret = Mock(side_effect=[replacement_cookie, replacement_id])
    monkeypatch.setattr(config_cli, "getpass", read_secret)

    return_code, output = _invoke(
        capsys,
        legacy_env,
        "set-secret",
        "CHAT_NARAKA_COOKIE",
        "CHAT_NARAKA_HEYBOX_ID",
    )

    assert return_code == 0
    assert _revision(output) == 3
    assert replacement_cookie not in output
    assert replacement_id not in output
    assert read_secret.call_count == 2


def test_clear_allows_only_optional_fields_and_preserves_revision_on_failure(
    legacy_env: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _initialize(capsys, legacy_env)

    return_code, output = _invoke(capsys, legacy_env, "clear", "CHAT_NARAKA_DEFAULT_ROLE_ID")
    assert return_code == 0
    assert _revision(output) == 2
    assert _has_restart_notice(output)

    return_code, output = _invoke(capsys, legacy_env, "clear", "ONEBOT_V11_ACCESS_TOKEN")
    assert return_code != 0
    assert ONEBOT_TOKEN not in output

    return_code, listed = _invoke(capsys, legacy_env, "list")
    assert return_code == 0
    assert _revision(listed) == 2
    assert _line_for(listed, "CHAT_NARAKA_DEFAULT_ROLE_ID").endswith("<empty>")
    assert _line_for(listed, "ONEBOT_V11_ACCESS_TOKEN").endswith("<set>")


@pytest.mark.parametrize(
    ("name", "value"),
    [("CHAT_AGENT_MAX_STEPS", "4"), ("CHAT_LLM_ENABLE_THINKING", "false")],
)
def test_chat_reasoning_settings_can_be_saved_and_cleared(
    legacy_env: Path, capsys: pytest.CaptureFixture[str], name: str, value: str,
) -> None:
    """普通聊天轮次和思考开关可通过设置命令持久化并恢复默认行为。"""
    _initialize(capsys, legacy_env)

    return_code, output = _invoke(capsys, legacy_env, "set", name, value)
    assert return_code == 0
    assert _revision(output) == 2
    return_code, listed = _invoke(capsys, legacy_env, "list")
    assert return_code == 0
    assert _line_for(listed, name) == f"{name}={value}"

    return_code, output = _invoke(capsys, legacy_env, "clear", name)
    assert return_code == 0
    assert _revision(output) == 3
    return_code, listed = _invoke(capsys, legacy_env, "list")
    assert return_code == 0
    assert _line_for(listed, name) == f"{name}=None"


def test_module_entrypoint_dispatches_settings_without_starting_bot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_run = Mock(return_value=23)
    run_bot = Mock()
    monkeypatch.setattr(config_cli, "run", settings_run)
    monkeypatch.setattr(entrypoint, "run_bot", run_bot)

    assert entrypoint.main(["settings", "list"]) == 23

    settings_run.assert_called_once_with(["list"])
    run_bot.assert_not_called()


def test_import_failure_identifies_invalid_field_without_echoing_value(monkeypatch, capsys):
    """导入错误必须指出配置字段，且不输出 Pydantic 输入或校验器上下文。"""
    from pydantic import ValidationError

    from agent_bot.config import RuntimeConfig

    private = "private-invalid-session-secret"
    with pytest.raises(ValidationError) as caught:
        RuntimeConfig(onebot_v11_access_token=ONEBOT_TOKEN, chat_session_secret=private)
    monkeypatch.setattr(config_cli, "_import_env", Mock(side_effect=caught.value))

    assert config_cli.run(["import-env"]) == 1
    output = capsys.readouterr().out
    assert "CHAT_SESSION_SECRET" in output
    assert private not in output
    assert ONEBOT_TOKEN not in output


def test_list_failure_explains_database_connection_without_driver_details(monkeypatch, capsys):
    """底层连接失败只显示错误类别与待检查项，不回显供应商消息中的凭据。"""
    from pymysql.err import OperationalError

    from agent_bot.settings import SettingsError

    failure = SettingsError("settings database operation failed")
    failure.__cause__ = OperationalError(2003, "private-database-password")
    monkeypatch.setattr(config_cli, "_list_settings", Mock(side_effect=failure))

    assert config_cli.run(["list"]) == 1
    output = capsys.readouterr().out
    assert "MySQL" in output and "CHAT_DB_HOST" in output
    assert "private-database-password" not in output


def test_invalid_saved_configuration_reports_fields_instead_of_database_failure(
    legacy_env, capsys, fake_mysql,
):
    """已开启自动发言的旧快照缺决策配置时，应指出依赖且不泄漏身份或秘密。"""
    _initialize(capsys, legacy_env)
    for row in fake_mysql.table:
        if row['config_key'] == 'chat_auto_speak_mcp_code':
            row['config_value'] = 'null'

    code, output = _invoke(capsys, legacy_env, 'list')

    assert code == 1
    assert 'CHAT_AUTO_SPEAK_MCP_CODE' in output
    assert '配置数据库操作失败' not in output
    assert ONEBOT_TOKEN not in output and SESSION_SECRET not in output
    assert all(value not in output for value in QQ_VALUES)


def test_set_can_repair_invalid_saved_configuration(legacy_env, capsys, fake_mysql):
    """显式关闭自动发言可修复旧快照，保留其他值并正常递增版本。"""
    from agent_bot.bootstrap import database_params, load_bootstrap_config, load_static_config
    from agent_bot.settings import SettingsStore

    _initialize(capsys, legacy_env)
    for row in fake_mysql.table:
        if row['config_key'] == 'chat_auto_speak_mcp_code':
            row['config_value'] = 'null'

    code, output = _invoke(capsys, legacy_env, 'set', 'CHAT_AUTO_SPEAK_ENABLED', 'false')

    assert code == 0, output
    assert _revision(output) == 2
    stored = SettingsStore(
        database_params(load_bootstrap_config(env_file=str(legacy_env))),
        static_config=load_static_config(env_file=str(legacy_env)),
    ).load()
    assert stored.config.chat_auto_speak_enabled is False
    assert stored.config.chat_auto_speak_mcp_code is None
    assert stored.config.chat_model == 'cli-test-model'
    assert stored.config.onebot_v11_access_token.get_secret_value() == ONEBOT_TOKEN
    assert stored.config.chat_session_secret.get_secret_value() == SESSION_SECRET


def test_invalid_snapshot_repair_still_validates_whole_candidate(legacy_env, capsys, fake_mysql):
    """读取未验证快照不等于放宽写入校验，无关修改不能保存无效配置。"""
    from copy import deepcopy

    _initialize(capsys, legacy_env)
    for row in fake_mysql.table:
        if row['config_key'] == 'chat_auto_speak_mcp_code':
            row['config_value'] = 'null'
    original = deepcopy(fake_mysql.table)

    code, _ = _invoke(capsys, legacy_env, 'set', 'CHAT_MODEL', 'different-model')

    assert code == 1
    assert fake_mysql.table == original
