"""托管失败独立于管理服务，不静默换模式，不记录上游原文。"""

import time

import pytest

from agent_bot.config import RuntimeConfig


def test_plugin_can_explicitly_start_and_retry_without_saving_static_mode(tmp_path):
    """外部默认值不阻止插件显式启动，失败后可再次点击，不会自动重试。"""
    from agent_bot.napcat.supervisor import NapCatSupervisor

    supervisor = NapCatSupervisor(tmp_path, RuntimeConfig(
        onebot_v11_access_token="offline-onebot-32-characters-at-least-secret",
        chat_session_secret="offline-session-32-characters-at-least-secret",
    ))
    for _ in range(2):
        supervisor.start("http://127.0.0.1:8080/", managed=True)
        supervisor._thread.join(timeout=5)
        assert supervisor.snapshot().error_code == "PAYLOAD_INVALID"
    assert supervisor.config.agent_bot_napcat_mode == "external"
    assert not (tmp_path / ".env").exists()
    supervisor.stop()


def test_external_disabled_and_missing_payload(tmp_path):
    """外部模式和禁用 QQ 不启动；显式托管缺资源必须报错。"""
    from agent_bot.napcat.supervisor import NapCatSupervisor

    values = {"onebot_v11_access_token": "offline-onebot-32-characters-at-least-secret",
              "chat_session_secret": "offline-session-32-characters-at-least-secret"}
    external = NapCatSupervisor(tmp_path, RuntimeConfig(**values))
    external.start("http://127.0.0.1:8080/")
    assert external.snapshot().mode == "external"
    disabled = NapCatSupervisor(tmp_path, RuntimeConfig(
        **values, agent_bot_napcat_mode="managed", app_enabled_plugins=[]))
    disabled.start("http://127.0.0.1:8080/")
    assert disabled.snapshot().state == "plugin_disabled"
    managed = NapCatSupervisor(tmp_path, RuntimeConfig(**values, agent_bot_napcat_mode="managed"))
    managed.start("http://127.0.0.1:8080/")
    limit = time.monotonic() + 5
    while managed.snapshot().state == "starting" and time.monotonic() < limit:
        time.sleep(0.02)
    assert managed.snapshot().error_code == "PAYLOAD_INVALID"
    managed.stop()
    managed.stop()
    assert values["onebot_v11_access_token"] not in (tmp_path / "logs/napcat.log").read_text()


def test_occupied_webui_port_fails_without_spawning_qq(tmp_path):
    """真实回环端口由外部实例持有时，托管停止在启动第三方程序之前。"""
    import socket

    from agent_bot.napcat.supervisor import NapCatSupervisor
    from napcat_fakes import make_payload

    make_payload(tmp_path / "runtime/napcat-node")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        supervisor = NapCatSupervisor(tmp_path, RuntimeConfig(
            onebot_v11_access_token="offline-onebot-32-characters-at-least-secret",
            chat_session_secret="offline-session-32-characters-at-least-secret",
            agent_bot_napcat_mode="managed", agent_bot_napcat_webui_port=listener.getsockname()[1],
        ))
        supervisor.start("http://127.0.0.1:8080/")
        limit = time.monotonic() + 5
        while supervisor.snapshot().state == "starting" and time.monotonic() < limit:
            time.sleep(0.02)
        assert supervisor.snapshot().error_code == "PORT_UNAVAILABLE"
        assert not (tmp_path / "data/napcat/config").exists()
        supervisor.stop()


def test_unresponsive_owned_webui_still_obeys_startup_deadline(tmp_path, monkeypatch):
    """监听属于本实例但 HTTP 始终失败时，也必须到期报错并释放资源。"""
    from types import SimpleNamespace

    import httpx

    from agent_bot.napcat import supervisor as module
    from napcat_fakes import make_payload

    closed = []

    class FakeJob:
        """只替代进程边界，配置、锁和工作线程仍走真实路径。"""

        def spawn(self, launch):
            """没有启动任何第三方可执行程序。"""
            return SimpleNamespace(close=lambda: closed.append("process"))

        def active_processes(self):
            """模拟活着的本实例。"""
            return 1

        def owns_listener(self, host, port):
            """监听归属成立，但并不代表 HTTP 成功。"""
            return True

        def close(self):
            """观察 Job 是否释放。"""
            closed.append("job")

    def fail_http(*args, **kwargs):
        """固定模拟无响应，不连接回环或外部网络。"""
        raise httpx.ConnectError("offline fixture")

    make_payload(tmp_path / "runtime/napcat-node")
    monkeypatch.setattr("agent_bot.napcat.windows_process.WindowsJob", FakeJob)
    monkeypatch.setattr("agent_bot.desktop_settings.check_port_available", lambda *_: None)
    ticks = iter([0, 31])
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(ticks, 31)))
    monkeypatch.setattr(httpx.Client, "get", fail_http)
    supervisor = module.NapCatSupervisor(tmp_path, RuntimeConfig(
        onebot_v11_access_token="offline-onebot-32-characters-at-least-secret",
        chat_session_secret="offline-session-32-characters-at-least-secret",
        agent_bot_napcat_mode="managed",
    ))
    supervisor.start("http://127.0.0.1:8080/")
    try:
        limit = time.monotonic() + 2
        while supervisor.snapshot().state == "starting" and time.monotonic() < limit:
            time.sleep(0.02)
        assert supervisor.snapshot().error_code == "WEBUI_UNAVAILABLE"
    finally:
        supervisor.stop()
    assert closed == ["process", "job"]


@pytest.mark.parametrize("save_error", [False, True])
def test_node_starts_without_registry_and_stops_owned_processes(tmp_path, monkeypatch, save_error):
    """无需系统 QQ 或打开管理页，后台记住账号；停止时释放进程和锁。"""
    import winreg
    from types import SimpleNamespace

    import httpx

    from agent_bot.napcat import supervisor as module
    from agent_bot.napcat.contracts import NapCatLogin
    from agent_bot.napcat.webui import WebUIError
    from napcat_fakes import make_payload

    closed = []
    launches = []
    remembered = []

    class FakeWebUI:
        """模拟原生扫码已在线，只观察后台是否主动保存，不访问真实 QQ。"""

        auto_login_configured = False

        def __init__(self, *args):
            """不初始化 HTTP 会话或读取本机凭据。"""

        def login(self, *, refresh=False):
            """记录没有前端请求时发生的保存。"""
            remembered.append(True)
            if save_error:
                raise WebUIError("离线模拟保存失败")
            self.auto_login_configured = True
            return NapCatLogin("online")

    class FakeJob:
        """模拟后台 Node 的就绪生命周期，不运行第三方代码或连接 QQ。"""

        def spawn(self, launch):
            """记录固定命令，返回所属进程观察句柄。"""
            launches.append(launch)
            return SimpleNamespace(close=lambda: closed.append("process"))

        def active_processes(self):
            """在取消前保持所属进程存活。"""
            return 1

        def owns_listener(self, host, port):
            """仅模拟本实例的登录页监听。"""
            return True

        def close(self):
            """记录 Job 释放，不影响其他进程。"""
            closed.append("job")

    def forbidden_registry(*args, **kwargs):
        """若仍尝试定位系统 QQ，就立即使测试失败。"""
        raise AssertionError("system QQ registration must not be read")

    make_payload(tmp_path / "runtime/napcat-node")
    monkeypatch.setattr(winreg, "OpenKey", forbidden_registry)
    monkeypatch.setattr("agent_bot.napcat.windows_process.WindowsJob", FakeJob)
    monkeypatch.setattr("agent_bot.desktop_settings.check_port_available", lambda *_: None)
    monkeypatch.setattr(httpx.Client, "get", lambda *_: SimpleNamespace(status_code=200))
    monkeypatch.setattr(module, "NapCatWebUI", FakeWebUI)
    supervisor = module.NapCatSupervisor(tmp_path, RuntimeConfig(
        onebot_v11_access_token="offline-onebot-32-characters-at-least-secret",
        chat_session_secret="offline-session-32-characters-at-least-secret",
        agent_bot_napcat_mode="managed",
    ))
    supervisor.start("http://127.0.0.1:8081/")
    try:
        limit = time.monotonic() + 5
        while supervisor.snapshot().state == "starting" and time.monotonic() < limit:
            time.sleep(0.02)
        assert supervisor.snapshot().state == "running"
        assert supervisor.snapshot().webui_ready
        assert launches[0].command[0].endswith("node.exe")
        while not remembered and time.monotonic() < limit:
            time.sleep(0.02)
        assert remembered == [True]
        if save_error:
            while supervisor.snapshot().error_code is None and time.monotonic() < limit:
                time.sleep(0.02)
            assert supervisor.snapshot().state == "running"
            assert supervisor.snapshot().error_code == "AUTO_LOGIN_FAILED"
    finally:
        supervisor.stop()
    assert closed == ["process", "job"]
    assert supervisor.snapshot().state == "stopped"
