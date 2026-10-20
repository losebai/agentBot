"""模型身份CLI只访问显式选库，默认预览且不会意外新建数据库。"""

import importlib.util
from pathlib import Path

import pytest


def _module():
    """加载命令定义，避免运行真实入口。"""
    spec = importlib.util.spec_from_file_location(
        "model_identity_cli", "tools/migrate_model_identity.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "flags,expected",
    [
        ([], {"dry_run": True, "verify_only": False}),
        (["--apply"], {"dry_run": False, "verify_only": False}),
        (["--verify-only"], {"dry_run": False, "verify_only": True}),
    ],
)
def test_cli_explicit_modes_and_relative_env_path(tmp_path, monkeypatch, flags, expected):
    """启动环境沿用既有解析，数据库相对路径归属于启动文件目录。"""
    module = _module()
    startup = tmp_path / "startup.env"
    startup.write_text(
        "CHAT_DB_BACKEND=sqlite\nCHAT_DB_SQLITE_PATH=business.db\n", encoding="utf-8"
    )
    (tmp_path / "business.db").touch()
    monkeypatch.delenv("CHAT_DB_BACKEND", raising=False)
    monkeypatch.delenv("CHAT_DB_SQLITE_PATH", raising=False)
    monkeypatch.setattr("sys.argv", ["migrate", "--env-file", str(startup), *flags])
    received = []
    monkeypatch.setattr(
        module,
        "migrate_model_identity",
        lambda params, **kwargs: received.append((params, kwargs)) or {},
    )
    module.main()
    assert Path(received[0][0].sqlite_path) == tmp_path / "business.db"
    assert received[0][1] == expected


@pytest.mark.parametrize("flags", [[], ["--apply"], ["--apply", "--verify-only"]])
def test_cli_missing_database_and_conflicts_do_not_connect(tmp_path, monkeypatch, flags):
    """错误命令和不存在的文件在连接前失败，SQLite不能新建空库。"""
    module = _module()
    path = tmp_path / "missing.db"
    monkeypatch.setattr("sys.argv", ["migrate", "--sqlite-path", str(path), *flags])
    monkeypatch.setattr(
        module, "migrate_model_identity", lambda *_args, **_kwargs: pytest.fail("must not connect")
    )
    with pytest.raises(SystemExit):
        module.main()
    assert not path.exists()
