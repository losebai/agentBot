"""桌面端口修改保留秘密与文件优先级，错误不得影响当前服务。"""

import socket

import pytest


def test_port_update_preserves_config_and_prod_precedence(tmp_path, monkeypatch):
    """修改高优先级文件中的端口，保留注释、秘密字面值及其他参数。"""
    from agent_bot.desktop_settings import connection_settings, save_port

    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("HOST", raising=False)
    original = "# keep\nCHAT_SESSION_SECRET='keep#${literal}'\nPORT=8080\n"
    (tmp_path / ".env").write_text(original, encoding="utf-8")
    secret = 'SECRET="line one\nPORT=inside-secret\nline three"\n'
    (tmp_path / ".env.prod").write_text(
        "# production\nPORT=invalid\n" + secret, encoding="utf-8",
    )
    with pytest.raises(ValueError):
        connection_settings(tmp_path)
    assert connection_settings(tmp_path, port_override=9010) == ("127.0.0.1", 9010)
    save_port(tmp_path, 9010)
    assert connection_settings(tmp_path) == ("127.0.0.1", 9010)
    assert (tmp_path / ".env").read_text("utf-8") == original
    assert "# production" in (tmp_path / ".env.prod").read_text("utf-8")
    assert secret in (tmp_path / ".env.prod").read_text("utf-8")


@pytest.mark.parametrize("port", [0, 65536, True, "8080"])
def test_invalid_port_does_not_write_config(tmp_path, port):
    """原生弹窗以外的调用也必须拒绝非法端口，不写入部分配置。"""
    from agent_bot.desktop_settings import save_port

    with pytest.raises(ValueError):
        save_port(tmp_path, port)
    assert not (tmp_path / ".env").exists()


def test_environment_port_cannot_be_silently_overridden(tmp_path, monkeypatch):
    """启动环境优先于文件时明确报错，避免保存后仍连接旧端口。"""
    from agent_bot.desktop_settings import save_port

    monkeypatch.setenv("PORT", "8123")
    with pytest.raises(ValueError, match="环境变量"):
        save_port(tmp_path, 9010)
    assert not (tmp_path / ".env").exists()


def test_occupied_port_is_reported_without_touching_other_listener():
    """探测只尝试绑定，不接管或关闭占用目标端口的其他进程。"""
    from agent_bot.desktop_settings import check_port_available

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with pytest.raises(ValueError, match=r"占用|使用"):
            check_port_available("127.0.0.1", listener.getsockname()[1])
        assert listener.getsockname()[1] > 0
