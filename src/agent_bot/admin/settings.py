"""运行配置写数据库、启动配置写文件，两者版本独立，秘密仅公开是否已设置。"""

from __future__ import annotations

from datetime import timedelta
from typing import Protocol, get_args

from pydantic import SecretStr

from agent_bot.admin.models import (
    BOOTSTRAP_SETTING_NAMES,
    HIDDEN_SETTING_NAMES,
    NATIVE_ONLY_STARTUP_SETTING_NAMES,
    STARTUP_SETTING_NAMES,
    SettingField,
    SettingsSnapshot,
    SettingsUpdate,
)
from agent_bot.bootstrap import BootstrapConfig
from agent_bot.config import CONFIG_DESCRIPTIONS, RuntimeConfig
from agent_bot.runtime_fields import RESTART_REQUIRED_FIELDS
from agent_bot.settings import (
    SECRET_SETTING_NAMES,
    SettingsRevisionConflictError,
    SettingsStore,
    SettingsValidationError,
    VersionedSettings,
    runtime_config_to_plain_values,
)
from agent_bot.startup_settings import StartupSettingsSnapshot, StartupSettingsStore
from agent_bot.static_config import DYNAMIC_SETTING_NAMES, STATIC_SETTING_NAMES, StaticConfig


class RuntimeProvider(Protocol):
    """Observe the live configuration without importing plugin globals into the API."""

    @property
    def config(self) -> RuntimeConfig: ...

    @property
    def revision(self) -> int: ...


class SettingsService:
    """Keep partial update semantics above the existing full-snapshot settings store."""

    def __init__(
        self, store: SettingsStore, runtime_provider: RuntimeProvider, *,
        startup_settings_store: StartupSettingsStore | None = None,
    ) -> None:
        """启动文件编辑能力必须显式注入，普通测试或只读调用不会访问本机配置文件。"""
        self.store = store
        self.runtime = runtime_provider
        self.startup = startup_settings_store

    def read(self) -> SettingsSnapshot:
        """Read persisted settings and redact every declared secret field."""
        return self._snapshot(self.store.load())

    def update(self, request: SettingsUpdate) -> SettingsSnapshot:
        """先验证完整候选，再在启动文件锁内提交动态值，文件失败不切换活动进程。"""
        if ('app_enabled_plugins' in request.values.model_fields_set
                and getattr(self.runtime, 'plugin_selection_locked', False)):
            raise SettingsValidationError(
                '独立进程的插件由启动命令固定，不能在网页修改', fields=('app_enabled_plugins',),
            )
        stored = self.store.load()
        if stored.revision != request.expected_revision:
            raise SettingsRevisionConflictError("settings revision conflict")
        submitted = request.values.model_fields_set
        # 托管进程的选择属于原生窗口，不能靠手工 HTTP 草稿绕过只读界面。
        if submitted & NATIVE_ONLY_STARTUP_SETTING_NAMES:
            raise SettingsValidationError(
                "NapCat startup settings are native only",
                fields=tuple(sorted(submitted & NATIVE_ONLY_STARTUP_SETTING_NAMES)),
            )
        startup_names = submitted & STARTUP_SETTING_NAMES
        if startup_names and (
            self.startup is None or not startup_names.issubset(self.startup.editable_names)
        ):
            raise SettingsValidationError(
                "startup settings are read only for this source",
                fields=tuple(sorted(startup_names)),
            )
        startup = self.startup.read() if self.startup is not None else None
        baseline = self._configured_runtime(stored.config, startup)
        merged = runtime_config_to_plain_values(baseline)
        for name in submitted - BOOTSTRAP_SETTING_NAMES:
            merged[name] = getattr(request.values, name)
        candidate = RuntimeConfig.model_validate(merged)
        bootstrap_values = self._bootstrap_values(startup)
        for name in submitted & BOOTSTRAP_SETTING_NAMES:
            bootstrap_values[name] = getattr(request.values, name)
        bootstrap = BootstrapConfig.model_validate(bootstrap_values)
        dynamic = {name: getattr(candidate, name) for name in DYNAMIC_SETTING_NAMES}
        if not startup_names:
            saved = self.store.save(dynamic, expected_revision=request.expected_revision)
            return self._snapshot(saved, startup_snapshot=startup)
        assert self.startup is not None and request.startup_revision is not None
        plain = runtime_config_to_plain_values(candidate) | _plain_bootstrap(bootstrap)
        values = {name: plain[name] for name in startup_names}
        with self.startup.edit(values, expected_revision=request.startup_revision) as after:
            saved = self.store.save(dynamic, expected_revision=request.expected_revision)
        # 使用事务给出的文件快照，不在文件锁内二次 read，也不在数据库提交后触发文件回滚。
        return self._snapshot(saved, startup_snapshot=after)

    def _configured_runtime(
        self, config: RuntimeConfig, startup: StartupSettingsSnapshot | None,
    ) -> RuntimeConfig:
        """文件使用启动时同款字段解析；动态值仍只来自当前所选数据库。"""
        if startup is None:
            return config
        static = StaticConfig.model_validate(startup.values).model_dump()
        return RuntimeConfig.model_validate(runtime_config_to_plain_values(config) | static)

    def _bootstrap_values(self, startup: StartupSettingsSnapshot | None) -> dict:
        """保留原始字段存在性，以复用 MySQL 必须显式提供密码的启动校验。"""
        if startup is not None:
            return {name: value for name, value in startup.values.items()
                    if name in BOOTSTRAP_SETTING_NAMES}
        params = self.store.params
        return {
            "chat_db_backend": params.backend, "chat_db_host": params.host,
            "chat_db_port": params.port, "chat_db_user": params.user,
            "chat_db_password": params.password, "chat_db_name": params.database,
            "chat_db_sqlite_path": params.sqlite_path,
        }

    def _snapshot(
        self, stored: VersionedSettings, *, startup_snapshot: StartupSettingsSnapshot | None = None,
    ) -> SettingsSnapshot:
        """公开可管理字段与待重启状态；隐藏旧入口，结构值判为 JSON，秘密继续脱敏。"""
        startup = startup_snapshot
        if startup is None and self.startup is not None:
            startup = self.startup.read()
        configured = self._configured_runtime(stored.config, startup)
        editable = ((self.startup.editable_names - NATIVE_ONLY_STARTUP_SETTING_NAMES)
                    if self.startup is not None else frozenset())
        running = self.runtime.config
        pending = {
            name
            for name in RESTART_REQUIRED_FIELDS
            if getattr(configured, name) != getattr(running, name)
        }
        pending.update(name for name in STATIC_SETTING_NAMES
                       if getattr(configured, name) != getattr(running, name))
        selection_locked = getattr(self.runtime, 'plugin_selection_locked', False)
        if selection_locked:
            # 数据库的全局选择不控制此进程，不能误报成重启该进程就能改变的字段。
            pending.discard('app_enabled_plugins')
        visible = configured.model_dump(mode="json", exclude=SECRET_SETTING_NAMES)
        fields = []
        for name, definition in RuntimeConfig.model_fields.items():
            if name in HIDDEN_SETTING_NAMES:
                continue
            nullable = type(None) in get_args(definition.annotation)
            if name in SECRET_SETTING_NAMES:
                value = getattr(configured, name)
                plain = value.get_secret_value() if hasattr(value, "get_secret_value") else value
                kind, displayed, is_set = "secret", None, bool(plain)
            else:
                displayed, is_set = visible.get(name), False
                actual_value = getattr(configured, name)
                if isinstance(actual_value, timedelta):
                    # 控件与配置说明使用秒，PATCH 数值仍由 RuntimeConfig 转回 timedelta。
                    displayed = actual_value.total_seconds()
                annotation = definition.annotation
                # 泛型参数是元素类型，不能把 dict[str, str] 或含标量输入的集合当标量。
                if isinstance(displayed, (dict, list)):
                    kind = "json"
                elif bool in (annotation, *get_args(annotation)):
                    kind = "boolean"
                elif isinstance(displayed, str):
                    kind = "string"
                elif isinstance(displayed, (int, float)):
                    kind = "number"
                elif int in (annotation, *get_args(annotation)) or float in (
                    annotation,
                    *get_args(annotation),
                ):
                    kind = "number"
                elif str in (annotation, *get_args(annotation)):
                    kind = "string"
                else:
                    kind = "json"
            fields.append(
                SettingField(
                    name=name,
                    description=CONFIG_DESCRIPTIONS.get(name, name),
                    kind=kind,
                    value=displayed,
                    nullable=nullable,
                    secret_is_set=is_set,
                    restart_required=(
                        name in RESTART_REQUIRED_FIELDS or name in STATIC_SETTING_NAMES
                    ),
                    read_only=(name in STATIC_SETTING_NAMES and name not in editable)
                    or (name == 'app_enabled_plugins' and selection_locked),
                    scope="static" if name in STATIC_SETTING_NAMES else "runtime",
                )
            )
        actual = self._bootstrap_values(None)
        bootstrap = _plain_bootstrap(
            BootstrapConfig.model_validate(self._bootstrap_values(startup))
        )
        descriptions = {
            "chat_db_backend": "数据库类型", "chat_db_sqlite_path": "SQLite 数据库文件",
            "chat_db_host": "MySQL 地址", "chat_db_port": "MySQL 端口",
            "chat_db_user": "MySQL 用户", "chat_db_name": "MySQL 数据库",
            "chat_db_password": "MySQL 密码",
        }
        for name, value in bootstrap.items():
            if startup is not None and value != actual[name]:
                pending.add(name)
            secret = name == "chat_db_password"
            fields.append(SettingField(
                name=name, description=descriptions[name],
                kind="secret" if secret else "number" if isinstance(value, int) else "string",
                value=None if secret else value, secret_is_set=bool(value) if secret else False,
                restart_required=True, read_only=name not in editable, scope="static",
            ))
        return SettingsSnapshot(
            database_revision=stored.revision,
            applied_revision=self.runtime.revision,
            startup_revision=startup.revision if startup is not None else None,
            restart_required_fields=sorted(pending),
            fields=fields,
        )


def _plain_bootstrap(config: BootstrapConfig) -> dict:
    """连接凭据只在内部用于校验与文件写入，不能使用会将 SecretStr 变成掩码的 JSON 转换。"""
    return {name: value.get_secret_value() if isinstance(value, SecretStr) else value
            for name, value in config.model_dump(by_alias=True).items()}
