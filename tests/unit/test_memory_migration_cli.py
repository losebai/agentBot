"""迁移命令复用启动解析，环境覆盖与相对路径均在离线库中验证。"""

import importlib.util
from pathlib import Path

import pytest


def _module():
    """加载工具模块而不实际执行迁移入口。"""
    spec = importlib.util.spec_from_file_location(
        "memory_migration_cli", Path("tools/migrate_memory_lifecycle.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_uses_environment_override_and_bootstrap_validation(tmp_path, monkeypatch):
    """环境指定SQLite时不能被文件中的MySQL配置覆盖而连接错误后端。"""
    module = _module()
    env_file = tmp_path / "startup.env"
    env_file.write_text(
        "CHAT_DB_BACKEND=mysql\nCHAT_DB_USER=file-user\nCHAT_DB_PASSWORD=file-key\n",
        encoding="utf-8",
    )
    database = tmp_path / "override.db"
    database.touch()
    monkeypatch.setenv("CHAT_DB_BACKEND", "sqlite")
    monkeypatch.setenv("CHAT_DB_SQLITE_PATH", str(database))
    monkeypatch.setattr("sys.argv", ["migrate", "--env-file", str(env_file)])
    received = []
    monkeypatch.setattr(
        module,
        "migrate_memory_lifecycle",
        lambda params, **kwargs: received.append((params, kwargs)) or {},
    )
    module.main()
    assert received[0][0].backend == "sqlite"
    assert received[0][0].sqlite_path == str(database)
    assert received[0][1] == {"dry_run": True, "verify_only": False}


def test_cli_resolves_relative_sqlite_path_from_startup_directory(tmp_path, monkeypatch):
    """指定启动文件后，其相对数据库路径不能随调用者cwd切换到另一数据库。"""
    module = _module()
    env_file = tmp_path / "startup.env"
    env_file.write_text(
        "CHAT_DB_BACKEND=sqlite\nCHAT_DB_SQLITE_PATH=business.db\n", encoding="utf-8"
    )
    (tmp_path / "business.db").touch()
    monkeypatch.delenv("CHAT_DB_BACKEND", raising=False)
    monkeypatch.delenv("CHAT_DB_SQLITE_PATH", raising=False)
    monkeypatch.setattr("sys.argv", ["migrate", "--env-file", str(env_file), "--verify-only"])
    received = []
    monkeypatch.setattr(
        module, "migrate_memory_lifecycle", lambda params, **kwargs: received.append(params) or {}
    )
    module.main()
    assert Path(received[0].sqlite_path) == tmp_path / "business.db"


@pytest.mark.parametrize("flags", [["--apply", "--dry-run"], ["--apply", "--verify-only"]])
def test_cli_rejects_conflicting_execution_modes(tmp_path, monkeypatch, flags):
    """写入和只读标识冲突时必须在连接数据库之前失败。"""
    module = _module()
    monkeypatch.setattr("sys.argv", ["migrate", "--sqlite-path", str(tmp_path / "db"), *flags])
    monkeypatch.setattr(
        module,
        "migrate_memory_lifecycle",
        lambda *_args, **_kwargs: pytest.fail("conflicting modes must not access the database"),
    )
    with pytest.raises(SystemExit):
        module.main()
