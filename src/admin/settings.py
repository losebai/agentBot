"""管理配置展示静态边界与动态版本，所有秘密仅公开是否已设置。"""

from typing import Protocol, get_args

from agent_bot.admin.models import SettingField, SettingsSnapshot, SettingsUpdate
from agent_bot.config import CONFIG_DESCRIPTIONS, RuntimeConfig
from agent_bot.runtime_fields import RESTART_REQUIRED_FIELDS
from agent_bot.settings import (
    SECRET_SETTING_NAMES,
    SettingsRevisionConflictError,
    SettingsStore,
    VersionedSettings,
    runtime_config_to_plain_values,
)
from agent_bot.static_config import STATIC_SETTING_NAMES


class RuntimeProvider(Protocol):
    """Observe the live configuration without importing plugin globals into the API."""

    @property
    def config(self) -> RuntimeConfig: ...

    @property
    def revision(self) -> int: ...


class SettingsService:
    """Keep partial update semantics above the existing full-snapshot settings store."""

    def __init__(self, store: SettingsStore, runtime_provider: RuntimeProvider) -> None:
        """Accept explicit persistence and runtime boundaries."""
        self.store = store
        self.runtime = runtime_provider

    def read(self) -> SettingsSnapshot:
        """Read persisted settings and redact every declared secret field."""
        return self._snapshot(self.store.load())

    def update(self, request: SettingsUpdate) -> SettingsSnapshot:
        """Merge only present fields, validate the full model and save with optimistic locking."""
        stored = self.store.load()
        if stored.revision != request.expected_revision:
            raise SettingsRevisionConflictError("settings revision conflict")
        merged = runtime_config_to_plain_values(stored.config)
        for name in request.values.model_fields_set:
            merged[name] = getattr(request.values, name)
        candidate = RuntimeConfig.model_validate(merged)
        saved = self.store.save(candidate, expected_revision=request.expected_revision)
        return self._snapshot(saved)

    def _snapshot(self, stored: VersionedSettings) -> SettingsSnapshot:
        """生成分组及只读元数据，秘密不回显，保存版本与生效版本分别展示。"""
        running = self.runtime.config
        pending = sorted(
            name
            for name in RESTART_REQUIRED_FIELDS
            if getattr(stored.config, name) != getattr(running, name)
        )
        visible = stored.config.model_dump(mode="json", exclude=SECRET_SETTING_NAMES)
        fields = []
        for name, definition in RuntimeConfig.model_fields.items():
            nullable = type(None) in get_args(definition.annotation)
            if name in SECRET_SETTING_NAMES:
                value = getattr(stored.config, name)
                plain = value.get_secret_value() if hasattr(value, "get_secret_value") else value
                kind, displayed, is_set = "secret", None, bool(plain)
            else:
                displayed, is_set = visible.get(name), False
                annotation = definition.annotation
                if bool in (annotation, *get_args(annotation)):
                    kind = "boolean"
                elif int in (annotation, *get_args(annotation)) or float in (
                    annotation,
                    *get_args(annotation),
                ):
                    kind = "number"
                elif isinstance(displayed, str) or str in (annotation, *get_args(annotation)):
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
                    restart_required=name in STATIC_SETTING_NAMES,
                    read_only=name in STATIC_SETTING_NAMES,
                    scope="static" if name in STATIC_SETTING_NAMES else "runtime",
                )
            )
        params = self.store.params
        bootstrap = {"chat_db_backend": ("数据库类型", params.backend)}
        if params.backend == "sqlite":
            bootstrap["chat_db_sqlite_path"] = ("SQLite 数据库文件", params.sqlite_path)
        else:
            bootstrap.update({
                "chat_db_host": ("MySQL 地址", params.host),
                "chat_db_port": ("MySQL 端口", params.port),
                "chat_db_user": ("MySQL 用户", params.user),
                "chat_db_name": ("MySQL 数据库", params.database),
            })
            fields.append(SettingField(
                name="chat_db_password", description="MySQL 密码", kind="secret",
                secret_is_set=bool(params.password), restart_required=True,
                read_only=True, scope="static",
            ))
        for name, (description, value) in bootstrap.items():
            fields.append(SettingField(
                name=name, description=description,
                kind="number" if isinstance(value, int) else "string", value=value,
                restart_required=True, read_only=True, scope="static",
            ))
        return SettingsSnapshot(
            database_revision=stored.revision,
            applied_revision=self.runtime.revision,
            restart_required_fields=pending,
            fields=fields,
        )
