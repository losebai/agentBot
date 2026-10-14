"""静态配置写入保留秘密、版本冲突及文件和数据库失败边界，全部使用临时目录。"""

import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from dotenv import dotenv_values


def test_static_edit_preserves_other_values_and_serializes_structures(tmp_path):
    """列表和秘密以可解析字面值保存，最高优先级文件之外的内容保持原样。"""
    from agent_bot.startup_settings import StartupSettingsStore

    base = '# base\nCHAT_SESSION_SECRET="independent-session-secret-more-than-32-characters"\n'
    preserved = 'OTHER_SECRET="line one\nPORT=inside-secret\nline three"\n'
    original = "# prod\nPORT=8080\n" + preserved
    (tmp_path / ".env").write_text(base, encoding="utf-8")
    (tmp_path / ".env.prod").write_text(original, encoding="utf-8")
    store = StartupSettingsStore(
        tmp_path, {"port", "chat_mcp_allowed_hosts", "onebot_v11_access_token"},
    )
    initial = store.read()
    token = 'onebot-private-token-#-${literal}-\\-"-more-than-32-characters'
    changes = {"port": 9010, "chat_mcp_allowed_hosts": ["localhost:*"],
               "onebot_v11_access_token": token}
    with store.edit(changes, expected_revision=initial.revision) as saved:
        assert saved.revision != initial.revision
        assert saved.values["port"] == "9010"
    values = dotenv_values(tmp_path / ".env.prod", interpolate=False)
    assert values["onebot_v11_access_token".upper()] == token
    assert values["CHAT_MCP_ALLOWED_HOSTS"] == '["localhost:*"]'
    assert (tmp_path / ".env").read_text("utf-8") == base
    assert preserved in (tmp_path / ".env.prod").read_text("utf-8")


def test_static_edit_rolls_back_file_when_database_operation_fails(tmp_path):
    """context 中未完成的持久事务失败时恢复原字节，不能留下半次静态保存。"""
    from agent_bot.startup_settings import StartupSettingsStore

    original = "# original\nPORT=8080\n"
    (tmp_path / ".env").write_text(original, encoding="utf-8")
    store = StartupSettingsStore(tmp_path, {"port"})
    revision = store.read().revision
    with pytest.raises(RuntimeError, match="database-denied"):
        with store.edit({"port": 9010}, expected_revision=revision):
            raise RuntimeError("database-denied")
    assert (tmp_path / ".env").read_text("utf-8") == original
    assert store.read().revision == revision


@pytest.mark.skipif(os.name != "nt", reason="Windows 文件 DACL 验收")
def test_static_edit_preserves_restricted_windows_file_permissions(tmp_path):
    """只允许当前账户访问的启动文件，原子替换和回滚后不能重新继承宽松权限。"""
    security = pytest.importorskip("win32security")
    api = pytest.importorskip("win32api")
    from agent_bot.startup_settings import StartupSettingsStore

    target = tmp_path / ".env"
    original = b"PORT=8080\n"
    target.write_bytes(original)
    account = security.LookupAccountName(None, api.GetUserName())[0]
    dacl = security.ACL()
    dacl.AddAccessAllowedAce(security.ACL_REVISION, 0x1F01FF, account)
    security.SetNamedSecurityInfo(
        str(target), security.SE_FILE_OBJECT,
        security.DACL_SECURITY_INFORMATION | security.PROTECTED_DACL_SECURITY_INFORMATION,
        None, None, dacl, None,
    )

    def permissions():
        """直接查询实际 NTFS DACL，与配置写入代码的实现路径独立。"""
        descriptor = security.GetFileSecurity(str(target), security.DACL_SECURITY_INFORMATION)
        return security.ConvertSecurityDescriptorToStringSecurityDescriptor(
            descriptor, security.SDDL_REVISION_1, security.DACL_SECURITY_INFORMATION,
        )

    expected = permissions()
    store = StartupSettingsStore(tmp_path, {"port"})
    with store.edit({"port": 9010}, expected_revision=store.read().revision):
        pass
    assert permissions() == expected
    with pytest.raises(RuntimeError, match="database-denied"):
        with store.edit({"port": 9020}, expected_revision=store.read().revision):
            raise RuntimeError("database-denied")
    assert permissions() == expected
    assert target.read_bytes() == b"PORT=9010\n"


def test_static_revision_rejects_old_snapshot_after_another_editor(tmp_path):
    """文件内容版本覆盖外部编辑，旧页面不能抹掉其他字段的新值。"""
    from agent_bot.startup_settings import StartupSettingsConflictError, StartupSettingsStore

    (tmp_path / ".env").write_text("PORT=8080\n", encoding="utf-8")
    store = StartupSettingsStore(tmp_path, {"port", "log_level"})
    revision = store.read().revision
    (tmp_path / ".env").write_text("PORT=8080\nLOG_LEVEL=DEBUG\n", encoding="utf-8")
    with pytest.raises(StartupSettingsConflictError):
        with store.edit({"port": 9010}, expected_revision=revision):
            pass
    assert store.read().values["log_level"] == "DEBUG"


def test_environment_overrides_are_captured_before_runtime_scrubbing(tmp_path, monkeypatch):
    """启动环境覆盖不能被网页无效地写进文件，后续环境清理不改变来源判定。"""
    from agent_bot.startup_settings import StartupSettingsStore, StartupSettingsValidationError

    (tmp_path / ".env").write_text("PORT=8080\n", encoding="utf-8")
    monkeypatch.setenv("PORT", "8123")
    store = StartupSettingsStore(tmp_path, {"port", "log_level"})
    monkeypatch.delenv("PORT")
    assert "port" not in store.editable_names and "log_level" in store.editable_names
    state = store.read()
    assert state.values["port"] == "8123"
    with pytest.raises(StartupSettingsValidationError, match="环境变量"):
        with store.edit({"port": 9010}, expected_revision=state.revision):
            pass


def test_unlisted_fields_cannot_write_arbitrary_startup_values(tmp_path):
    """静态编辑字段白名单独立于用户传入名称，不能扩展到任意启动或秘密参数。"""
    from agent_bot.startup_settings import StartupSettingsStore, StartupSettingsValidationError

    (tmp_path / ".env").write_text("PORT=8080\n", encoding="utf-8")
    store = StartupSettingsStore(tmp_path, {"port"})
    with pytest.raises(StartupSettingsValidationError):
        with store.edit({"AGENT_BOT_ADMIN_TOKEN": "unauthorized"},
                        expected_revision=store.read().revision):
            pass
    assert (tmp_path / ".env").read_text("utf-8") == "PORT=8080\n"


def test_readers_do_not_observe_pending_static_values(tmp_path):
    """不同存储实例的读者必须等待写事务，不能把将回滚的值当作新启动版本。"""
    from agent_bot.startup_settings import StartupSettingsStore

    (tmp_path / ".env").write_text("PORT=8080\n", encoding="utf-8")
    writer = StartupSettingsStore(tmp_path, {"port"})
    reader = StartupSettingsStore(tmp_path, {"port"})
    revision = writer.read().revision
    entered, release = threading.Event(), threading.Event()
    reading_started, reading_completed = threading.Event(), threading.Event()

    def write():
        """保持暂存窗口直到测试释放，再模拟数据库写失败。"""
        with pytest.raises(RuntimeError):
            with writer.edit({"port": 9010}, expected_revision=revision):
                entered.set()
                assert release.wait(timeout=5)
                raise RuntimeError("database-denied")

    def read():
        """显式确认读线程已执行，才能检查写事务未释放时是否阻塞读取。"""
        reading_started.set()
        result = reader.read()
        reading_completed.set()
        return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(write)
        assert entered.wait(timeout=5)
        reading = executor.submit(read)
        try:
            assert reading_started.wait(timeout=5)
            assert not reading_completed.wait(timeout=0.1)
        finally:
            release.set()
        pending.result(timeout=5)
        assert reading.result(timeout=5).values["port"] == "8080"
