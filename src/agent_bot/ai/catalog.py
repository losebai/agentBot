"""以事务维护供应商、区分大小写的模型别名及媒体能力。"""

from datetime import datetime, timezone

import pymysql

from agent_bot.ai.contracts import (
    CatalogReadiness,
    ModelCreate,
    ModelImportResult,
    ModelUpdate,
    ModelView,
    Page,
    ProviderCreate,
    ProviderUpdate,
    ProviderView,
    ResolvedModel,
)
from agent_bot.ai.database import AiDatabase, page_limit, query, sql_columns
from agent_bot.ai.errors import AiError
from agent_bot.credential_values import row_values, write_credential
from agent_bot.media.catalog import (
    model_view,
    read_media_batch,
    validate_protocol,
    write_model_media,
)

_TABLES = {"provider": "agent_bot_ai_provider", "model": "agent_bot_ai_model"}
_CREATE = {"provider": ProviderCreate, "model": ModelCreate}
_VIEW = {"provider": ProviderView, "model": ModelView}
_COLUMNS = {
    "provider": (
        "name",
        "protocol",
        "base_url",
        "credential",
        "credential_required",
        "enabled",
        "description",
    ),
    "model": ("alias", "upstream_model", "provider_id", "enabled"),
}


class AiCatalog(AiDatabase):
    """模型目录与关联能力共享版本和提交边界。"""

    async def readiness(self, model: str | None = None) -> CatalogReadiness:
        """检查目录和统一调用归档，普通执行旧表退出后不再影响就绪。"""
        try:
            await self.run(
                lambda connection: [
                    query(connection, f"SELECT {sql_columns(columns)} FROM {table} LIMIT 0")
                    for table, columns in [
                        (
                            _TABLES["provider"],
                            set(ProviderView.model_fields),
                        ),
                        (
                            _TABLES["model"],
                            {*_COLUMNS["model"], "id", "revision", "created_at", "updated_at"},
                        ),
                        (
                            "agent_bot_call_record",
                            {"id", "source", "source_id", "execution_id", "session_Id",
                             "request_id", "model", "status", "duration", "data",
                             "created_at_utc", "create_time", "content", "replay_context",
                             "request_context"},
                        ),
                    ]
                ]
            )
            if model is not None:
                await self.resolve(model)
            return CatalogReadiness(
                initialized=True,
                model_available=model is not None,
                provider_available=model is not None,
            )
        except AiError as error:
            return CatalogReadiness(initialized=error.code != "AI_NOT_INITIALIZED", code=error.code)

    def _get(self, connection, kind: str, id: int, *, lock: bool = False):
        """同连接读取嵌套能力，供 AI 与 Agent 目录共同使用。"""
        row = query(
            connection,
            f"SELECT * FROM {_TABLES[kind]} WHERE id=%s" + (" FOR UPDATE" if lock else ""),
            (id,),
            one=True,
        )
        if row is None:
            raise AiError("AI_CONFIG_NOT_FOUND", status_code=404)
        return (
            model_view(connection, row)
            if kind == "model"
            else ProviderView.model_validate(row_values(row, ProviderView))
        )

    async def _list(self, kind: str, limit: int, cursor: str | None, provider_id=None):
        """按稳定主键分页并批量读取媒体能力。"""
        page_limit(limit)
        try:
            after = 0 if cursor is None else int(cursor)
            if after < 0:
                raise ValueError
        except ValueError as error:
            raise AiError("AI_INVALID_PAGE", status_code=422) from error

        def read(connection):
            """分页与能力查询保持在一次数据库事务中。"""
            rows = query(
                connection,
                f"SELECT * FROM {_TABLES[kind]} WHERE id>%s"
                + (" AND provider_id=%s" if provider_id is not None else "")
                + " ORDER BY id LIMIT %s",
                (after, provider_id, limit + 1) if provider_id is not None else (after, limit + 1),
            )
            if kind == "model":
                capabilities, initialized = read_media_batch(
                    connection, [row["id"] for row in rows]
                )
                for row in rows:
                    row.update(media=capabilities.get(row["id"], {}), media_initialized=initialized)
            return rows

        rows = await self.run(read)
        return Page[_VIEW[kind]](
            items=[
                _VIEW[kind].model_validate(row_values(row, _VIEW[kind])) for row in rows[:limit]
            ],
            next_cursor=str(rows[limit - 1]["id"]) if len(rows) > limit else None,
        )

    async def list_providers(
        self, limit: int = 50, cursor: str | None = None
    ) -> Page[ProviderView]:
        return await self._list("provider", limit, cursor)

    async def list_models(
        self, limit: int = 50, cursor: str | None = None, provider_id=None
    ) -> Page[ModelView]:
        """按供应商过滤已登记模型，避免相同上游 ID 的服务串用。"""
        return await self._list("model", limit, cursor, provider_id)

    async def get_provider(self, id: int, expected_revision: int):
        """管理员可以检查停用供应商，但必须基于当前版本发现模型。"""

        def read(connection):
            """密钥和连接信息在同一数据库读取中固定。"""
            provider = self._get(connection, "provider", id)
            if provider.revision != expected_revision:
                raise AiError("AI_CONFIG_CONFLICT", status_code=409)
            return provider

        return await self.run(read)

    async def import_models(self, provider_id, body):
        """事务内保留已登记项；同步可显式报告别名冲突，严格导入仍整批回滚。"""

        def write(connection):
            """沿用目录写入的供应商加锁顺序，保护导入依据的版本。"""
            query(connection, "SELECT id FROM agent_bot_ai_provider ORDER BY id FOR UPDATE")
            provider = self._get(connection, "provider", provider_id, lock=True)
            if provider.revision != body.expected_revision:
                raise AiError("AI_CONFIG_CONFLICT", status_code=409)
            imported, skipped, conflicts = [], [], []
            stamp = datetime.now(timezone.utc).replace(tzinfo=None)
            for item in body.models:
                exists = query(
                    connection,
                    "SELECT id FROM agent_bot_ai_model WHERE provider_id=%s AND upstream_model=%s",
                    (provider_id, item.upstream_model),
                    one=True,
                )
                if exists:
                    skipped.append(item.upstream_model)
                    continue
                # 别名全局唯一，供应商同名模型只能由管理员指定不同别名，不能自动改名。
                occupied = query(
                    connection,
                    "SELECT id FROM agent_bot_ai_model WHERE alias=%s",
                    (item.alias,),
                    one=True,
                )
                if occupied:
                    if not body.skip_alias_conflicts:
                        raise AiError("AI_MODEL_ALIAS_CONFLICT", status_code=409)
                    conflicts.append(item)
                    continue
                with connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO agent_bot_ai_model "
                        "(alias,upstream_model,provider_id,enabled,revision,created_at,updated_at) "
                        "VALUES (%s,%s,%s,0,1,%s,%s)",
                        (item.alias, item.upstream_model, provider_id, stamp, stamp),
                    )
                    id = cursor.lastrowid
                imported.append(self._get(connection, "model", id))
            return ModelImportResult(
                provider_id=provider_id,
                revision=provider.revision,
                imported=tuple(imported),
                skipped=tuple(skipped),
                conflicts=tuple(conflicts),
            )

        return await self.run(write, write=True)

    async def _create(self, kind: str, body):
        """只持久化白名单列，嵌套能力在同事务写入独立表。"""

        def write(connection):
            """先锁供应商再写模型，统一配置写入的锁顺序。"""
            values = body.model_dump(include=set(_COLUMNS[kind]))
            if kind == "provider":
                write_credential(values, body)
            stamp = datetime.now(timezone.utc).replace(tzinfo=None)
            values.update(created_at=stamp, updated_at=stamp)
            if kind == "model":
                provider = self._get(connection, "provider", values["provider_id"], lock=True)
                validate_protocol(provider.protocol, body.media)
            columns = list(values)
            with connection.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO {_TABLES[kind]} ({','.join(columns)},revision) "
                    f"VALUES ({','.join(['%s'] * len(columns))},1)",
                    tuple(values.values()),
                )
                id = cursor.lastrowid
            if kind == "model" and "media" in body.model_fields_set:
                write_model_media(connection, id, body.media)
            return self._get(connection, kind, id)

        return await self.run(write, write=True)

    async def create_provider(self, body: ProviderCreate) -> ProviderView:
        return await self._create("provider", body)

    async def create_model(self, body: ModelCreate) -> ModelView:
        return await self._create("model", body)

    async def _update(self, kind: str, id: int, body):
        """版本校验、协议兼容和能力替换必须原子提交。"""
        changes = body.model_dump(exclude_unset=True, exclude={"expected_revision"})
        if not changes:
            raise AiError("AI_EMPTY_PATCH", status_code=422)

        def write(connection):
            """按供应商主键统一加锁，避免协议切换与能力更新交错。"""
            query(connection, "SELECT id FROM agent_bot_ai_provider ORDER BY id FOR UPDATE")
            current = self._get(connection, kind, id, lock=True)
            if current.revision != body.expected_revision:
                raise AiError("AI_CONFIG_CONFLICT", status_code=409)
            validated = _CREATE[kind].model_validate(
                {
                    **current.model_dump(include=set(_CREATE[kind].model_fields)),
                    **changes,
                }
            )
            values = validated.model_dump(include=set(_COLUMNS[kind]))
            if kind == "provider":
                write_credential(values, body, current)
            values["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
            if kind == "model":
                provider = self._get(connection, "provider", values["provider_id"], lock=True)
                validate_protocol(provider.protocol, validated.media)
                if "media" in changes:
                    write_model_media(connection, id, validated.media)
            elif values["protocol"] == "ollama":
                rows = query(
                    connection, "SELECT id FROM agent_bot_ai_model WHERE provider_id=%s", (id,)
                )
                capabilities, _ = read_media_batch(connection, [row["id"] for row in rows])
                for capability in capabilities.values():
                    validate_protocol(values["protocol"], capability)
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {_TABLES[kind]} SET "
                    + ",".join(f"{name}=%s" for name in values)
                    + ",revision=revision+1 WHERE id=%s AND revision=%s",
                    (*values.values(), id, body.expected_revision),
                )
            return self._get(connection, kind, id)

        return await self.run(write, write=True)

    async def update_provider(self, id: int, body: ProviderUpdate) -> ProviderView:
        return await self._update("provider", id, body)

    async def update_model(self, id: int, body: ModelUpdate) -> ModelView:
        return await self._update("model", id, body)

    async def _delete(self, kind: str, id: int, expected_revision: int):
        """停用后才能删除，主模型及两项路由引用都返回明确的占用错误。"""
        def write(connection):
            """引用检查与删除同属事务，数据库外键仍防止并发引用被破坏。"""
            current = self._get(connection, kind, id, lock=True)
            if current.revision != expected_revision or current.enabled:
                raise AiError("AI_CONFIG_CONFLICT", status_code=409)
            if kind == "provider" and query(
                connection,
                "SELECT id FROM agent_bot_ai_model WHERE provider_id=%s LIMIT 1",
                (id,),
                one=True,
            ):
                raise AiError("AI_CONFIG_IN_USE", status_code=409)
            if kind == "model":
                # 独立 AI 目录可能尚未初始化 Agent 表，此时仍保持原删除能力。
                try:
                    referenced = query(
                        connection,
                        "SELECT id FROM agent_bot_agent WHERE model_id=%s "
                        "OR vision_model_id=%s OR fallback_model_id=%s LIMIT 1",
                        (id, id, id), one=True,
                    )
                except (pymysql.OperationalError, pymysql.ProgrammingError) as error:
                    if error.args[0] != 1146:
                        raise
                    referenced = None
                if referenced:
                    raise AiError("AI_CONFIG_IN_USE", status_code=409)
            with connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM {_TABLES[kind]} WHERE id=%s", (id,))

        await self.run(write, write=True)

    async def delete_provider(self, id: int, expected_revision: int) -> None:
        await self._delete("provider", id, expected_revision)

    async def delete_model(self, id: int, expected_revision: int) -> None:
        await self._delete("model", id, expected_revision)

    async def resolve(self, model: str) -> ResolvedModel:
        """解析不可变的模型、供应商和媒体能力快照。"""

        def read(connection):
            """所有快照字段来自同一读取事务。"""
            row = query(
                connection, "SELECT * FROM agent_bot_ai_model WHERE alias=%s", (model,), one=True
            )
            if row is None:
                raise AiError("AI_MODEL_NOT_FOUND", status_code=400)
            selected = model_view(connection, row)
            if not selected.enabled:
                raise AiError("AI_MODEL_DISABLED", status_code=400)
            provider = self._get(connection, "provider", selected.provider_id)
            if not provider.enabled:
                raise AiError("AI_PROVIDER_DISABLED", status_code=400)
            return ResolvedModel(model=selected, provider=provider)

        return await self.run(read)
