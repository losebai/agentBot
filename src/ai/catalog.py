"""以事务维护供应商、区分大小写的模型别名及媒体能力。"""

from datetime import datetime, timezone

from agent_bot.ai.contracts import (
    CatalogReadiness,
    ModelCreate,
    ModelUpdate,
    ModelView,
    Page,
    ProviderCreate,
    ProviderUpdate,
    ProviderView,
    ResolvedModel,
    SessionWrite,
)
from agent_bot.ai.database import AiDatabase, page_limit, query
from agent_bot.ai.errors import AiError
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
    "provider": ("name", "protocol", "base_url", "credential_ref", "enabled", "description"),
    "model": ("alias", "upstream_model", "provider_id", "enabled"),
}


class AiCatalog(AiDatabase):
    """模型目录与关联能力共享版本和提交边界。"""

    async def readiness(self, model: str | None = None) -> CatalogReadiness:
        """纯文本就绪不依赖可选媒体表。"""
        try:
            await self.run(
                lambda connection: [
                    query(connection, f"SELECT {','.join(columns)} FROM {table} LIMIT 0")
                    for table, columns in [
                        (
                            _TABLES["provider"],
                            set(ProviderView.model_fields) - {"credential_available"},
                        ),
                        (
                            _TABLES["model"],
                            {*_COLUMNS["model"], "id", "revision", "created_at", "updated_at"},
                        ),
                        ("agent_bot_ai_session", set(SessionWrite.model_fields)),
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
        return model_view(connection, row) if kind == "model" else ProviderView.model_validate(row)

    async def _list(self, kind: str, limit: int, cursor: str | None):
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
                f"SELECT * FROM {_TABLES[kind]} WHERE id>%s ORDER BY id LIMIT %s",
                (after, limit + 1),
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
            items=[_VIEW[kind].model_validate(row) for row in rows[:limit]],
            next_cursor=str(rows[limit - 1]["id"]) if len(rows) > limit else None,
        )

    async def list_providers(
        self, limit: int = 50, cursor: str | None = None
    ) -> Page[ProviderView]:
        return await self._list("provider", limit, cursor)

    async def list_models(self, limit: int = 50, cursor: str | None = None) -> Page[ModelView]:
        return await self._list("model", limit, cursor)

    async def _create(self, kind: str, body):
        """只持久化白名单列，嵌套能力在同事务写入独立表。"""

        def write(connection):
            """先锁供应商再写模型，统一配置写入的锁顺序。"""
            values = body.model_dump(include=set(_COLUMNS[kind]))
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
        def write(connection):
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
