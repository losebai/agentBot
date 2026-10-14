"""页面写启动配置但不切换活动进程，版本锁与数据库回滚使用临时文件验收。"""

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from agent_bot.admin.config import AdminConfig
from agent_bot.admin.models import STARTUP_SETTING_NAMES, SettingsUpdate
from agent_bot.admin.routes import install_admin
from agent_bot.admin.runtime import PluginRuntimeProvider
from agent_bot.admin.settings import SettingsService
from agent_bot.settings import SettingsError, SettingsRevisionConflictError, SettingsValidationError
from agent_bot.startup_settings import StartupSettingsStore
from agent_bot.static_config import STATIC_SETTING_NAMES

NEW_TOKEN = "new-file-token-at-least-thirty-two-characters"


class StartupTokens:
    """替代父任务实现的文件协议，真实配置服务与数据库仍由生产代码负责。"""

    def __init__(self, path: Path, token: str, session_secret: str) -> None:
        """只建立测试目录文件，保留附加行用于回滚和秘密披露检查。"""
        self.path = path
        self.path.write_text(
            f"# preserved configuration\nONEBOT_V11_ACCESS_TOKEN={token}\n"
            f"CHAT_SESSION_SECRET={session_secret}\n"
        )
        self.editable_names = STATIC_SETTING_NAMES | frozenset({
            "chat_db_backend", "chat_db_host", "chat_db_port", "chat_db_user",
            "chat_db_password", "chat_db_name", "chat_db_sqlite_path",
        })
        self.before_write = None

    def read(self):
        """按完整文件内容计算版本，便于模拟桌面或其他窗口写入造成的冲突。"""
        content = self.path.read_bytes()
        values = dict(line.split("=", 1) for line in content.decode().splitlines() if "=" in line)
        return SimpleNamespace(values={name.lower(): value for name, value in values.items()},
                               revision=hashlib.sha256(content).hexdigest())

    @contextmanager
    def edit(self, values: dict, *, expected_revision: str):
        """暂存后围绕真实数据库保存，任一失败恢复原文件且不回显 Token。"""
        original = self.path.read_bytes()
        current = self.read()
        if current.revision != expected_revision:
            raise SettingsRevisionConflictError("startup settings revision conflict")
        for name, value in values.items():
            current.values[name] = (
                json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict, bool))
                else "" if value is None else str(value)
            )
        self.path.write_text("# preserved configuration\n" + "".join(
            f"{name.upper()}={value}\n" for name, value in current.values.items()
        ))
        try:
            if self.before_write is not None:
                self.before_write()
            yield self.read()
        except BaseException:
            self.path.write_bytes(original)
            raise


@pytest.fixture
def startup_tokens(tmp_path, admin_runtime):
    """令临时文件初值与活动认证一致，默认不会产生待重启提示。"""
    return StartupTokens(
        tmp_path / ".env", admin_runtime.config.onebot_v11_access_token.get_secret_value(),
        admin_runtime.config.chat_session_secret.get_secret_value(),
    )


def manager(admin_store, admin_runtime, startup_tokens):
    """文件写入能力必须显式注入，无能力的普通替身保持 Token 只读。"""
    return SettingsService(admin_store, admin_runtime, startup_settings_store=startup_tokens)


def request(startup_tokens, **values):
    """提交当前文件版本和数据库版本，只使用现有 PATCH 配置协议。"""
    return SettingsUpdate(expected_revision=1, startup_revision=startup_tokens.read().revision,
                          values=values)


def test_startup_fields_are_writable_and_redacted(admin_store, admin_runtime, startup_tokens):
    """具备显式文件能力时静态字段可写，仍需重启且秘密不回显。"""
    snapshot = manager(admin_store, admin_runtime, startup_tokens).read()
    fields = {field.name: field for field in snapshot.fields}
    token = fields["onebot_v11_access_token"]
    assert token.scope == "static" and not token.read_only and token.restart_required
    assert token.value is None and token.secret_is_set
    assert not fields["chat_session_secret"].read_only and not fields["port"].read_only
    assert snapshot.startup_revision == startup_tokens.read().revision
    assert (
        admin_runtime.config.onebot_v11_access_token.get_secret_value()
        not in snapshot.model_dump_json()
    )


def test_combined_patch_saves_file_and_dynamic_config_without_reconnecting(
    admin_store, admin_runtime, startup_tokens, fake_mysql,
):
    """一个 PATCH 保存两种介质并推进数据库版本，但活动 Token 保持原值。"""
    current = admin_runtime.config.onebot_v11_access_token
    result = manager(admin_store, admin_runtime, startup_tokens).update(request(
        startup_tokens, onebot_v11_access_token=NEW_TOKEN, chat_llm_temperature=0.4,
    ))
    assert startup_tokens.read().values["onebot_v11_access_token"] == NEW_TOKEN
    assert admin_store.load().config.chat_llm_temperature == 0.4
    assert admin_store.load().config.onebot_v11_access_token == current
    assert admin_runtime.config.onebot_v11_access_token == current
    assert result.database_revision == 2 and result.applied_revision == 1
    assert "onebot_v11_access_token" in result.restart_required_fields
    assert result.startup_revision == startup_tokens.read().revision
    assert NEW_TOKEN not in result.model_dump_json()
    assert "onebot_v11_access_token" not in {row["config_key"] for row in fake_mysql.table}


def test_token_only_advances_database_revision(admin_store, admin_runtime, startup_tokens):
    """Token 单独保存仍统一推进版本，旧全局配置窗口不能继续覆盖本次操作。"""
    original = admin_store.load().config.chat_llm_temperature
    result = manager(admin_store, admin_runtime, startup_tokens).update(request(
        startup_tokens, onebot_v11_access_token=NEW_TOKEN,
    ))
    assert result.database_revision == 2
    assert admin_store.load().config.chat_llm_temperature == original


def test_read_reflects_latest_file_token_without_changing_database(
    admin_store, admin_runtime, startup_tokens,
):
    """桌面或其他窗口更新文件后，页面仍展示实际文件版本及等待重启状态。"""
    with startup_tokens.edit({"onebot_v11_access_token": NEW_TOKEN},
                             expected_revision=startup_tokens.read().revision):
        pass
    snapshot = manager(admin_store, admin_runtime, startup_tokens).read()
    assert "onebot_v11_access_token" in snapshot.restart_required_fields
    assert snapshot.database_revision == 1


@pytest.mark.parametrize("values", [
    {"onebot_v11_access_token": "short"},
    {"onebot_v11_access_token": NEW_TOKEN + "\ninvalid"},
    {"chat_db_password": "**********"},
    {"onebot_v11_access_token": NEW_TOKEN, "chat_llm_temperature": -1},
])
def test_invalid_combined_patch_does_not_write_either_store(
    admin_store, admin_runtime, startup_tokens, values,
):
    """完整候选校验先于文件暂存，其他动态值无效时也不提前修改 Token。"""
    original = startup_tokens.path.read_bytes()
    with pytest.raises(ValueError):
        manager(admin_store, admin_runtime, startup_tokens).update(
            request(startup_tokens, **values)
        )
    assert startup_tokens.path.read_bytes() == original
    assert admin_store.load_revision() == 1


def test_file_version_conflict_preserves_dynamic_config(admin_store, admin_runtime, startup_tokens):
    """文件版本与数据库版本独立仲裁，不允许覆盖桌面已修改的启动文件。"""
    original = startup_tokens.path.read_bytes()
    update = SettingsUpdate(expected_revision=1, startup_revision="0" * 64,
                            values={"onebot_v11_access_token": NEW_TOKEN})
    with pytest.raises(SettingsRevisionConflictError):
        manager(admin_store, admin_runtime, startup_tokens).update(update)
    assert startup_tokens.path.read_bytes() == original
    assert admin_store.load_revision() == 1


def test_database_failure_rolls_back_file(admin_store, admin_runtime, startup_tokens, monkeypatch):
    """文件暂存后的数据库保存失败须恢复原配置文件，不留下半次 Token 更新。"""
    original = startup_tokens.path.read_bytes()

    def fail(*args, **kwargs):
        """只替代明确失败的数据库边界，不修改实际保存逻辑。"""
        raise SettingsError("isolated write failed")

    monkeypatch.setattr(admin_store, "save", fail)
    with pytest.raises(SettingsError):
        manager(admin_store, admin_runtime, startup_tokens).update(request(
            startup_tokens, onebot_v11_access_token=NEW_TOKEN,
        ))
    assert startup_tokens.path.read_bytes() == original
    assert admin_store.load_revision() == 1


def test_readonly_startup_source_cannot_be_bypassed(admin_store, admin_runtime, startup_tokens):
    """环境覆盖或不可编辑文件不能借直接 HTTP 候选值绕过只读边界。"""
    startup_tokens.editable_names = frozenset()
    original = startup_tokens.path.read_bytes()
    current = manager(admin_store, admin_runtime, startup_tokens)
    field = next(row for row in current.read().fields if row.name == "onebot_v11_access_token")
    assert field.read_only
    with pytest.raises(SettingsValidationError):
        current.update(request(startup_tokens, onebot_v11_access_token=NEW_TOKEN))
    assert startup_tokens.path.read_bytes() == original
    assert admin_store.load_revision() == 1


def test_non_token_static_fields_are_saved_without_runtime_switch(
    admin_store, admin_runtime, startup_tokens, fake_mysql,
):
    """端口和会话密钥保存到同一启动文件，活动配置与数据库静态列不变。"""
    result = manager(admin_store, admin_runtime, startup_tokens).update(request(
        startup_tokens, port=8765, chat_session_secret="new-session-secret-more-than-32-characters",
    ))
    fields = {row.name: row for row in result.fields}
    assert fields["port"].value == 8765 and fields["chat_session_secret"].value is None
    assert {"port", "chat_session_secret"}.issubset(result.restart_required_fields)
    assert admin_runtime.config.port == admin_store.load().config.port == 8080
    assert {"port", "chat_session_secret"}.isdisjoint(
        {row["config_key"] for row in fake_mysql.table}
    )


def test_database_target_is_written_without_switching_active_connection(
    admin_store, admin_runtime, startup_tokens,
):
    """数据库目标只供重启后使用，当前保存继续提交到原权威数据库。"""
    result = manager(admin_store, admin_runtime, startup_tokens).update(request(
        startup_tokens, chat_db_backend="sqlite", chat_db_sqlite_path="data/new-target.sqlite3",
    ))
    fields = {row.name: row for row in result.fields}
    assert fields["chat_db_backend"].value == "sqlite"
    assert fields["chat_db_sqlite_path"].value == "data/new-target.sqlite3"
    assert "chat_db_backend" in result.restart_required_fields
    assert admin_store.params.backend == "mysql" and admin_store.load_revision() == 2


def test_new_mysql_target_requires_explicit_credentials(admin_store, admin_runtime, startup_tokens):
    """切换到 MySQL 复用启动校验，缺失连接凭据时不能写出重启必失败的配置。"""
    original = startup_tokens.path.read_bytes()
    with pytest.raises(ValueError):
        manager(admin_store, admin_runtime, startup_tokens).update(request(
            startup_tokens, chat_db_backend="mysql", chat_db_user="new-account",
        ))
    assert startup_tokens.path.read_bytes() == original
    assert admin_store.load_revision() == 1


def test_database_version_conflict_after_file_staging_restores_file(
    admin_store, admin_runtime, startup_tokens,
):
    """文件暂存期间另一管理请求提交，当前请求恢复文件并保留对方数据库值。"""
    original = startup_tokens.path.read_bytes()
    candidate = admin_store.load().config.model_copy(update={"chat_llm_temperature": 0.8})
    startup_tokens.before_write = lambda: admin_store.save(candidate, expected_revision=1)
    with pytest.raises(SettingsRevisionConflictError):
        manager(admin_store, admin_runtime, startup_tokens).update(request(
            startup_tokens, onebot_v11_access_token=NEW_TOKEN, chat_llm_temperature=0.4,
        ))
    assert startup_tokens.path.read_bytes() == original
    assert admin_store.load_revision() == 2
    assert admin_store.load().config.chat_llm_temperature == 0.8


def test_provider_without_explicit_startup_capability_stays_readonly():
    """未装配应用的独立提供者不得自动读取当前目录的真实启动文件。"""
    assert PluginRuntimeProvider(SimpleNamespace()).startup_settings_store is None


def test_captured_environment_override_remains_readonly_after_scrubbing(
    admin_store, admin_runtime, startup_tokens, monkeypatch,
):
    """应用清理环境变量后仍保留启动来源，不允许保存被环境覆盖的无效文件值。"""
    monkeypatch.setenv("PORT", "9123")
    startup = StartupSettingsStore(startup_tokens.path.parent, STARTUP_SETTING_NAMES)
    monkeypatch.delenv("PORT")
    current = manager(admin_store, admin_runtime, startup)
    snapshot = current.read()
    port = next(row for row in snapshot.fields if row.name == "port")
    assert port.read_only and port.value == 9123 and "port" in snapshot.restart_required_fields
    original = startup_tokens.path.read_bytes()
    with pytest.raises(SettingsValidationError):
        current.update(request(startup, port=8765))
    assert startup_tokens.path.read_bytes() == original and admin_store.load_revision() == 1


@pytest.fixture
def real_startup(startup_tokens):
    """真实文件锁与原子写入仅作用于临时目录，不访问开发配置。"""
    return StartupSettingsStore(startup_tokens.path.parent, STARTUP_SETTING_NAMES)


@pytest.fixture
async def startup_api(real_startup, admin_runtime, admin_store):
    """真实 ASGI 路由与文件存储组合，数据库和运行时仍使用离线替身。"""
    admin_runtime.startup_settings_store = real_startup
    app = FastAPI()
    install_admin(app, admin_store, admin_runtime, AdminConfig(token=""))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://offline.test",
    ) as client:
        yield client


async def test_real_file_http_patch_preserves_runtime_and_redacts_secrets(
    startup_api, real_startup, admin_store, admin_runtime,
):
    """路由提交真实文件事务后可读新版本，不会嵌套文件锁或改变活动认证和连接。"""
    before = (await startup_api.get("/api/v1/settings")).json()
    running = admin_runtime.config.onebot_v11_access_token
    response = await startup_api.patch("/api/v1/settings", json={
        "expected_revision": before["database_revision"],
        "startup_revision": before["startup_revision"],
        "values": {"onebot_v11_access_token": NEW_TOKEN, "port": 8765,
                   "chat_llm_temperature": 0.4},
    })
    assert response.status_code == 200
    saved = response.json()
    assert saved["database_revision"] == 2 and saved["applied_revision"] == 1
    assert {"onebot_v11_access_token", "port"}.issubset(saved["restart_required_fields"])
    assert real_startup.read().values["onebot_v11_access_token"] == NEW_TOKEN
    assert NEW_TOKEN not in response.text
    assert saved["startup_revision"] != before["startup_revision"]
    assert admin_runtime.config.onebot_v11_access_token == running
    assert admin_store.params.backend == "mysql" and admin_store.load().config.port == 8080
    assert admin_store.load().config.chat_llm_temperature == 0.4
    assert (await startup_api.get("/api/v1/settings")).json() == saved


async def test_real_file_http_conflict_and_missing_revision_preserve_both_stores(
    startup_api, startup_tokens, admin_store,
):
    """文件版本过期返回冲突，缺失版本及密码掩码返回校验错误，均不提前写入。"""
    original = startup_tokens.path.read_bytes()
    for values, version, expected_status in [
        ({"port": 8765}, "0" * 64, 409),
        ({"port": 8765}, None, 422),
        ({"chat_db_password": "**********"}, "0" * 64, 422),
    ]:
        response = await startup_api.patch("/api/v1/settings", json={
            "expected_revision": 1, "startup_revision": version, "values": values,
        })
        assert response.status_code == expected_status
        assert startup_tokens.path.read_bytes() == original
        assert admin_store.load_revision() == 1


async def test_real_file_database_failure_restores_original_bytes(
    startup_api, startup_tokens, admin_store, monkeypatch,
):
    """真实文件暂存后数据库错误沿 API 返回 503，且文件原字节与版本恢复。"""
    before = (await startup_api.get("/api/v1/settings")).json()
    original = startup_tokens.path.read_bytes()

    def fail(*args, **kwargs):
        """仅模拟提交前故障，不触及生产数据库。"""
        raise SettingsError("isolated database save failure")

    monkeypatch.setattr(admin_store, "save", fail)
    response = await startup_api.patch("/api/v1/settings", json={
        "expected_revision": 1, "startup_revision": before["startup_revision"],
        "values": {"onebot_v11_access_token": NEW_TOKEN},
    })
    assert response.status_code == 503 and NEW_TOKEN not in response.text
    assert startup_tokens.path.read_bytes() == original and admin_store.load_revision() == 1


def test_editable_log_level_and_session_duration_use_usable_control_types(
    admin_store, admin_runtime, startup_tokens,
):
    """日志等级以文本编辑，会话有效期以标签声明的秒数读取和提交。"""
    current = manager(admin_store, admin_runtime, startup_tokens)
    fields = {row.name: row for row in current.read().fields}
    assert fields["log_level"].kind == "string" and fields["log_level"].value == "INFO"
    assert fields["session_expire_timeout"].kind == "number"
    assert fields["session_expire_timeout"].value == 120
    saved = current.update(request(startup_tokens, log_level="DEBUG", session_expire_timeout=180))
    fields = {row.name: row for row in saved.fields}
    assert fields["log_level"].value == "DEBUG"
    assert fields["session_expire_timeout"].value == 180
    assert admin_runtime.config.log_level == "INFO"
    assert admin_runtime.config.session_expire_timeout.total_seconds() == 120
