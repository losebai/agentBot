"""启动升级使用独立临时库验证版本、事务、重入和数据保留。"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_bot import __version__
from agent_bot.database_upgrade import Migration, UpgradeError, upgrade_database
from agent_bot.settings import DatabaseParams

ROOT = Path(__file__).resolve().parents[2]


def resource(name: str) -> str:
    """测试显式读取仓库 SQL，生产入口不回退源码路径。"""
    return (ROOT / "sql" / name).read_text("utf-8")


@pytest.fixture
def legacy_database(tmp_path):
    """模拟尚无版本记录、没有 B站表的完整 0.5.5 至 0.5.7 基线。"""
    path = tmp_path / "business.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(resource("agent_bot_init.sqlite.sql"))
        for name in (
            "agent_bot_bilibili_account",
            "agent_bot_bilibili_operation",
            "agent_bot_schema_version",
            "agent_bot_schema_migration",
        ):
            connection.execute(f"DROP TABLE IF EXISTS {name}")
        connection.execute("CREATE TABLE private_business (value TEXT)")
        connection.execute("INSERT INTO private_business VALUES ('must-preserve')")
    return DatabaseParams(backend="sqlite", sqlite_path=str(path))


def query(params, statement):
    """仅用于读取测试创建的独立数据库。"""
    with sqlite3.connect(params.sqlite_path) as connection:
        return connection.execute(statement).fetchall()


def test_unversioned_database_upgrades_and_second_start_has_no_ddl(legacy_database):
    """首次升级补 B站及版本记录，第二次启动不重复执行 DDL 或更改数据。"""
    first = upgrade_database(legacy_database, read_resource=resource)
    assert first.current_version == __version__
    assert first.previous_version is None
    assert first.applied == ("bilibili-v1", "comfy-plugin-v1")
    assert query(legacy_database, "SELECT value FROM private_business") == [("must-preserve",)]
    assert query(legacy_database, "SELECT version FROM agent_bot_schema_version") == [
        (__version__,),
    ]
    second = upgrade_database(legacy_database, read_resource=resource)
    assert second.applied == () and second.previous_version == __version__
    assert query(legacy_database, "SELECT count(*) FROM agent_bot_schema_migration") == [(2,)]


def test_newer_database_is_rejected_without_changing_any_rows(legacy_database):
    """旧程序不得自动降级新版数据库。"""
    upgrade_database(legacy_database, read_resource=resource)
    with sqlite3.connect(legacy_database.sqlite_path) as connection:
        connection.execute("UPDATE agent_bot_schema_version SET version='99.0.0'")
        before = list(connection.iterdump())
    with pytest.raises(UpgradeError, match="高于"):
        upgrade_database(legacy_database, read_resource=resource)
    with sqlite3.connect(legacy_database.sqlite_path) as connection:
        assert list(connection.iterdump()) == before


def test_sqlite_failure_rolls_back_ddl_and_preserves_old_version(legacy_database):
    """DDL 和版本推进处于同一写事务，后续 SQL 出错不能留下半升级。"""
    from agent_bot.database_upgrade import MIGRATIONS

    upgrade_database(legacy_database, read_resource=resource)
    bad = Migration("0.6.0", "broken-test", "bad.sql", "bad.sql")

    def read(name):
        """以真实 SQL 执行中途失败，不通过 mock 假装回滚。"""
        return (
            "CREATE TABLE test_partial(id INTEGER); INSERT INTO nonexistent VALUES (1);"
            if name == "bad.sql"
            else resource(name)
        )

    with pytest.raises(UpgradeError, match="broken-test"):
        upgrade_database(
            legacy_database,
            read_resource=read,
            target_version="0.6.0",
            migrations=(*MIGRATIONS, bad),
        )
    assert query(legacy_database, "SELECT version FROM agent_bot_schema_version") == [
        (__version__,),
    ]
    assert not query(legacy_database, "SELECT name FROM sqlite_master WHERE name='test_partial'")


def test_applied_script_cannot_be_silently_rewritten(legacy_database):
    """已执行脚本的校验和改变时拒绝盖写流水。"""
    upgrade_database(legacy_database, read_resource=resource)

    def changed(name):
        """只修改已登记迁移内容，确保版本相同也会检测。"""
        return resource(name) + ("\n-- changed" if name == "bilibili-init.sqlite.sql" else "")

    with pytest.raises(UpgradeError, match="校验和"):
        upgrade_database(legacy_database, read_resource=changed)


def test_missing_script_fails_before_version_tables_are_created(legacy_database):
    """发行包缺 SQL 时明确停止，不跳过该迁移或先登记当前版本。"""

    def missing(name):
        """模拟 wheel 中缺失当前后端增量。"""
        if name == "bilibili-init.sqlite.sql":
            raise FileNotFoundError(name)
        return resource(name)

    with pytest.raises(UpgradeError, match="缺少"):
        upgrade_database(legacy_database, read_resource=missing)
    assert not query(
        legacy_database, "SELECT name FROM sqlite_master WHERE name='agent_bot_schema_version'"
    )


def test_unrecognized_old_schema_is_not_stamped_current(tmp_path):
    """缺少历史数据转换起点时，不能猜测版本或重新初始化。"""
    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "old.sqlite3"))
    with sqlite3.connect(params.sqlite_path) as connection:
        connection.execute("CREATE TABLE chat_setting(id INTEGER)")
        connection.execute("INSERT INTO chat_setting VALUES (77)")
    with pytest.raises(UpgradeError, match="旧版"):
        upgrade_database(params, read_resource=resource)
    assert query(params, "SELECT id FROM chat_setting") == [(77,)]
    assert not query(params, "SELECT name FROM sqlite_master WHERE name='agent_bot_schema_version'")


def test_two_startups_serialize_and_only_one_applies_migration(legacy_database):
    """写锁取得后重新读取版本，避免两个实例同时首次升级。"""

    def start():
        """每个线程使用自己的数据库连接模拟独立启动进程。"""
        return upgrade_database(legacy_database, read_resource=resource)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: start(), range(2)))
    assert sorted(len(result.applied) for result in results) == [0, 2]


def test_missing_existing_sqlite_file_is_not_created(tmp_path):
    """已有启动配置指向错误路径时，禁止把空新库当旧库升级。"""
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(UpgradeError, match="不存在"):
        upgrade_database(
            DatabaseParams(backend="sqlite", sqlite_path=str(path)), read_resource=resource
        )
    assert not path.exists()


def test_recorded_old_version_can_add_structure_required_by_new_version(legacy_database):
    """已记录版本的旧结构要先执行增量，再核对当前完整结构。"""
    from agent_bot.database_upgrade import MIGRATIONS

    upgrade_database(legacy_database, read_resource=resource)
    with sqlite3.connect(legacy_database.sqlite_path) as connection:
        creation = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='chat_workflow'"
        ).fetchone()[0]
        connection.execute("DROP TABLE chat_workflow")
    next_step = Migration("0.6.0", "new-workflow-table", "workflow.sql", "workflow.sql")
    result = upgrade_database(
        legacy_database,
        target_version="0.6.0",
        migrations=(*MIGRATIONS, next_step),
        read_resource=lambda name: creation if name == "workflow.sql" else resource(name),
    )
    assert result.applied == ("new-workflow-table",)
    assert result.current_version == "0.6.0"


def test_sql_splitter_preserves_quoted_semicolons_and_comment_markers():
    """防止把值中的分号切成另一条语句，注释不能改变字符串内容。"""
    from agent_bot.sql_scripts import split_sql

    assert split_sql(
        "-- ignored;\nINSERT INTO t VALUES ('a;--b'); /* skip; */ SELECT 'it''s';"
    ) == [
        "INSERT INTO t VALUES ('a;--b')",
        "SELECT 'it''s'",
    ]


def test_startup_upgrade_precedes_loading_runtime_settings(monkeypatch):
    """不能先加载依赖新表的配置，再尝试升级；失败必须阻止继续启动。"""
    from unittest.mock import Mock

    from agent_bot import bootstrap, database_upgrade

    events = []
    monkeypatch.setattr(
        database_upgrade, "upgrade_database", lambda params: events.append("upgrade")
    )
    monkeypatch.setattr(bootstrap, "load_static_config", lambda: object())
    store = Mock()
    store.load.side_effect = lambda: events.append("load") or "stored"
    monkeypatch.setattr(bootstrap, "SettingsStore", lambda *args, **kw: store)
    assert bootstrap.load_startup_settings(bootstrap.BootstrapConfig()) == "stored"
    assert events == ["upgrade", "load"]
    store.load.reset_mock()

    def fail(params):
        """升级失败后配置读取和服务装配均不应继续。"""
        raise UpgradeError("stop-before-start")

    monkeypatch.setattr(database_upgrade, "upgrade_database", fail)
    with pytest.raises(UpgradeError, match="stop-before-start"):
        bootstrap.load_startup_settings(bootstrap.BootstrapConfig())
    store.load.assert_not_called()


def test_database_upgrade_cli_reuses_startup_runner_without_initializing(monkeypatch, capsys):
    """显式升级命令不重新初始化数据库，也不启动 QQ。"""
    from unittest.mock import Mock

    from agent_bot import database_cli
    from agent_bot.database_upgrade import UpgradeResult

    params = DatabaseParams(backend="sqlite", sqlite_path="offline.sqlite3")
    monkeypatch.setattr(database_cli, "load_bootstrap_config", lambda: object())
    monkeypatch.setattr(database_cli, "database_params", lambda value: params)
    initialize = Mock()
    monkeypatch.setattr(database_cli, "initialize_database", initialize)
    upgrade = Mock(return_value=UpgradeResult("0.5.6", "0.5.7", ("bilibili-v1",)))
    monkeypatch.setattr(database_cli, "upgrade_database", upgrade)
    assert database_cli.run(["upgrade"]) == 0
    upgrade.assert_called_once_with(params)
    initialize.assert_not_called()
    assert "0.5.7" in capsys.readouterr().out
    upgrade.side_effect = UpgradeError("数据库版本高于当前程序版本")
    assert database_cli.run(["upgrade"]) == 1
    assert "高于" in capsys.readouterr().out


def test_named_model_index_must_have_the_correct_columns(legacy_database):
    """仅有同名唯一索引不足以证明完成模型身份升级。"""
    with sqlite3.connect(legacy_database.sqlite_path) as connection:
        connection.execute("DROP INDEX uq_model_provider_upstream")
        connection.execute(
            "CREATE UNIQUE INDEX uq_model_provider_upstream ON agent_bot_ai_model(alias)"
        )
    with pytest.raises(UpgradeError, match="模型身份"):
        upgrade_database(legacy_database, read_resource=resource)
    assert not query(
        legacy_database, "SELECT name FROM sqlite_master WHERE name='agent_bot_schema_version'"
    )
