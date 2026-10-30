"""WebUI 替身验证本机二维码桥接，不启动真实 QQ 或泄露管理凭据。"""

import hashlib
import json

import httpx
import pytest


def test_login_bridge_authenticates_once_and_keeps_secrets_private(tmp_path, monkeypatch):
    """轮询复用服务端凭据，仅返回二维码及状态，在线后移除二维码。"""
    from agent_bot.napcat import webui

    folder = tmp_path / "data/napcat/config"
    folder.mkdir(parents=True)
    (folder / "webui.json").write_text('{"token":"private-local-token"}')
    calls = []
    online = False

    def reply(request):
        """只允许固定回环地址与三种操作，保留鉴权协议的精确断言。"""
        calls.append(request.url.path)
        assert request.url.host == "127.0.0.1" and request.url.port == 6199
        if request.url.path == "/api/auth/login":
            assert json.loads(request.content) == {
                "hash": hashlib.sha256(b"private-local-token.napcat").hexdigest(),
            }
            return httpx.Response(200, json={
                "code": 0, "data": {"Credential": "private-credential"},
            })
        assert request.headers["Authorization"] == "Bearer private-credential"
        if request.url.path == "/api/QQLogin/GetQuickLoginQQ":
            return httpx.Response(200, json={"code": 0, "data": ""})
        if request.url.path == "/api/QQLogin/GetQQLoginInfo":
            return httpx.Response(200, json={"code": 0, "data": {"uin": "123456"}})
        if request.url.path == "/api/QQLogin/SetQuickLoginQQ":
            assert json.loads(request.content) == {"uin": "123456"}
            return httpx.Response(200, json={"code": 0, "data": None})
        return httpx.Response(200, json={"code": 0, "data": {
            "isLogin": online, "coreReady": online, "qrLoginAccepted": False,
            "loginPhase": "ready" if online else "waiting_qrcode",
            "qrcodeurl": "https://txz.qq.com/p?k=offline-qr", "loginError": "",
        }})

    original = httpx.Client
    monkeypatch.setattr(webui.httpx, "Client", lambda **kw: original(
        **kw, transport=httpx.MockTransport(reply),
    ))
    client = webui.NapCatWebUI(tmp_path, 6199, lambda: True)
    result = client.login()
    assert result.status == "waiting"
    assert result.qr_code_url == "https://txz.qq.com/p?k=offline-qr"
    client.login(refresh=True)
    assert calls.count("/api/QQLogin/RefreshQRcode") == 1
    online = True
    assert client.login(refresh=True).qr_code_url is None
    assert client.login().status == "online"
    assert calls.count("/api/QQLogin/SetQuickLoginQQ") == 1
    assert calls.count("/api/QQLogin/RefreshQRcode") == 1
    assert calls.count("/api/auth/login") == 1
    assert "private" not in repr(result)
    assert client.auto_login_configured


def test_existing_auto_login_account_is_not_overwritten(tmp_path, monkeypatch):
    """已有后台账号配置优先，默认记住本次账号不能覆盖用户选择。"""
    from agent_bot.napcat import webui

    folder = tmp_path / "data/napcat/config"
    folder.mkdir(parents=True)
    (folder / "webui.json").write_text('{"token":"private-local-token"}')
    calls = []

    def reply(request):
        """模拟已登录另一个账号，但后台已经明确选择默认账号。"""
        calls.append(request.url.path)
        if request.url.path == "/api/auth/login":
            data = {"Credential": "private-credential"}
        elif request.url.path == "/api/QQLogin/CheckLoginStatus":
            data = {"isLogin": True, "coreReady": True}
        elif request.url.path == "/api/QQLogin/GetQuickLoginQQ":
            data = "654321"
        else:
            raise AssertionError("Existing account must not be changed")
        return httpx.Response(200, json={"code": 0, "data": data})

    original = httpx.Client
    monkeypatch.setattr(webui.httpx, "Client", lambda **kw: original(
        **kw, transport=httpx.MockTransport(reply),
    ))
    assert webui.NapCatWebUI(tmp_path, 6199, lambda: True).login().status == "online"
    assert "/api/QQLogin/SetQuickLoginQQ" not in calls


def test_login_bridge_refuses_unowned_listener_without_network(tmp_path):
    """所属进程已停止时不能把本地凭据交给后来占用同端口的程序。"""
    from agent_bot.napcat.webui import NapCatWebUI, WebUIError

    with pytest.raises(WebUIError):
        NapCatWebUI(tmp_path, 6199, lambda: False).login()


def test_webui_error_does_not_echo_upstream_secrets(tmp_path, monkeypatch):
    """上游正文或异常可能含凭据，只公开项目固定文案。"""
    from agent_bot.napcat import webui

    folder = tmp_path / "data/napcat/config"
    folder.mkdir(parents=True)
    (folder / "webui.json").write_text('{"token":"private-local-token"}')
    original = httpx.Client
    monkeypatch.setattr(webui.httpx, "Client", lambda **kw: original(
        **kw, transport=httpx.MockTransport(lambda _: httpx.Response(
            200, json={"code": -1, "message": "private-local-token"},
        )),
    ))
    with pytest.raises(webui.WebUIError) as error:
        webui.NapCatWebUI(tmp_path, 6199, lambda: True).login()
    assert "private" not in str(error.value)
