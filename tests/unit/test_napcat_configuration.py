"""验证托管参数属于启动配置，不能被数据库运行值接管。"""

import pytest
from pydantic import ValidationError

from agent_bot.config import RuntimeConfig
from agent_bot.static_config import DYNAMIC_SETTING_NAMES, STATIC_SETTING_NAMES


def config(**values):
    """仅提供隔离密钥，不读取本机配置。"""
    return RuntimeConfig(
        onebot_v11_access_token="isolated-onebot-token-at-least-32-characters",
        chat_session_secret="isolated-session-secret-at-least-32-characters", **values,
    )


@pytest.fixture
def installed_qq(tmp_path):
    """模拟载荷目录之外的本机 QQ，配置生成不能修改其程序文件。"""
    executable = tmp_path / "Installed QQ/QQ.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"offline-installed-qq")
    return executable


def test_napcat_defaults_and_static_ownership():
    """旧部署不因更新自动启动 QQ，端口和模式只属于启动配置。"""
    current = config()
    assert current.agent_bot_napcat_mode == "external"
    assert current.agent_bot_napcat_webui_port == 6099
    for name in ("agent_bot_napcat_mode", "agent_bot_napcat_webui_port"):
        assert name in STATIC_SETTING_NAMES
        assert name not in DYNAMIC_SETTING_NAMES


@pytest.mark.parametrize("values", [
    {"agent_bot_napcat_mode": "automatic"},
    {"agent_bot_napcat_webui_port": 0},
    {"agent_bot_napcat_webui_port": 65536},
])
def test_napcat_rejects_invalid_startup_values(values):
    """非法端口和隐式模式不得变成静默默认值。"""
    with pytest.raises(ValidationError):
        config(**values)


def test_owned_config_inherits_current_token_and_preserves_webui_secret(tmp_path, installed_qq):
    """重启时才使用新 Token；首次账号通过默认文件继承，WebUI 密钥独立保留。"""
    import json

    from agent_bot.napcat.configuration import configure_instance
    from agent_bot.napcat.payload import load_payload
    from napcat_fakes import make_payload

    payload = load_payload(make_payload(tmp_path / "runtime/napcat"))
    current = config(agent_bot_napcat_mode="managed")
    launch = configure_instance(tmp_path, payload, current, "http://[::1]:8088/",
                                qq_executable=installed_qq)
    assert launch.command[1] == str(installed_qq)
    assert installed_qq.read_bytes() == b"offline-installed-qq"
    folder = tmp_path / "data/napcat/config"
    onebot = json.loads((folder / "onebot11.json").read_text())
    client = onebot["network"]["websocketClients"][0]
    assert client["url"] == "ws://[::1]:8088/onebot/v11/ws"
    assert client["token"] == current.onebot_v11_access_token.get_secret_value()
    assert client["messagePostFormat"] == "array"
    webui = json.loads((folder / "webui.json").read_text())
    assert webui["host"] == "::"
    assert webui["token"] != client["token"]
    assert launch.environment["NAPCAT_DISABLE_MULTI_PROCESS"] == "1"
    assert client["token"] not in repr(launch)
    (folder / "onebot11_12345.json").write_text(json.dumps(onebot))
    configure_instance(tmp_path, payload, current, "http://127.0.0.1:8090/",
                       qq_executable=installed_qq)
    assert json.loads((folder / "webui.json").read_text())["token"] == webui["token"]
    assert json.loads((folder / "onebot11_12345.json").read_text())[
        "network"]["websocketClients"][0]["url"].endswith(":8090/onebot/v11/ws")


def test_configuration_failure_does_not_overwrite_existing_files(
    tmp_path, monkeypatch, installed_qq,
):
    """一份账号配置写入失败时还原此前已更新的文件。"""
    import os

    from agent_bot.napcat.configuration import ConfigurationError, configure_instance
    from agent_bot.napcat.payload import load_payload
    from napcat_fakes import make_payload

    payload = load_payload(make_payload(tmp_path / "runtime/napcat"))
    current = config(agent_bot_napcat_mode="managed")
    configure_instance(tmp_path, payload, current, "http://127.0.0.1:8080/",
                       qq_executable=installed_qq)
    folder = tmp_path / "data/napcat/config"
    originals = {p: p.read_bytes() for p in folder.iterdir()}
    replace = os.replace

    def fail_webui(source, target):
        """仅中断目标写入，回滚其他文件继续使用真实原子替换。"""
        if str(target).endswith("webui.json"):
            raise OSError("isolated write error")
        return replace(source, target)

    monkeypatch.setattr(os, "replace", fail_webui)
    with pytest.raises(ConfigurationError):
        configure_instance(tmp_path, payload, current, "http://127.0.0.1:8090/",
                           qq_executable=installed_qq)
    for path, original in originals.items():
        assert path.read_bytes() == original
