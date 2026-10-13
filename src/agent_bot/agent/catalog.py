"""Agent 目录事务与包含模型媒体能力的不可变执行快照。"""

from datetime import datetime, timezone

from agent_bot.agent.catalog_rows import CREATE, RELATIONS, VIEW, decode, encode
from agent_bot.agent.contracts import (
    AgentReadiness,
    AgentRequest,
    AgentSnapshot,
    RunStart,
    StepWrite,
)
from agent_bot.agent.errors import AgentError
from agent_bot.ai.catalog import AiCatalog
from agent_bot.ai.contracts import Page, ResolvedModel
from agent_bot.ai.database import AiDatabase, page_limit, query, sql_columns
from agent_bot.ai.errors import AiError
from agent_bot.credential_values import row_values, write_credential
from agent_bot.media.catalog import model_view


class AgentCatalog(AiDatabase):
    """集中管理 Agent 配置、关联关系和同事务读取快照。"""
    def _get(self, connection, kind, id):
        row = query(
            connection, f"SELECT * FROM agent_bot_{kind} WHERE id=%s FOR UPDATE", (id,), one=True
        )
        if row is None:
            raise AgentError("AGENT_CONFIG_NOT_FOUND", status_code=404)
        values = decode(row)
        if kind == "agent":
            for field, related in RELATIONS.items():
                values[field] = tuple(
                    r[f"{related}_id"]
                    for r in query(
                        connection,
                        f"SELECT {related}_id FROM agent_bot_agent_{related} "
                        f"WHERE agent_id=%s ORDER BY {related}_id",
                        (id,),
                    )
                )
        return VIEW[kind].model_validate(row_values(values, VIEW[kind]))

    def _relations(self, connection, id, values):
        for field, related in RELATIONS.items():
            ids = values.pop(field)
            for related_id in ids:
                self._get(connection, related, related_id)
            with connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM agent_bot_agent_{related} WHERE agent_id=%s", (id,))
                for related_id in ids:
                    cursor.execute(
                        f"INSERT INTO agent_bot_agent_{related} "
                        f"(agent_id,{related}_id) VALUES (%s,%s)",
                        (id, related_id),
                    )

    async def _create(self, kind, body):
        def write(connection):
            values = body.model_dump()
            if kind == "mcp":
                write_credential(values, body)
            relations = {k: values.pop(k) for k in RELATIONS} if kind == "agent" else {}
            if kind == "agent":
                AiCatalog._get(self, connection, "model", values["model_id"], lock=True)
            stamp = datetime.now(timezone.utc).replace(tzinfo=None)
            values.update(revision=1, created_at=stamp, updated_at=stamp)
            values = encode(values)
            with connection.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO agent_bot_{kind} ({','.join(values)}) "
                    f"VALUES ({','.join(['%s'] * len(values))})",
                    tuple(values.values()),
                )
                id = cursor.lastrowid
            if kind == "agent":
                self._relations(connection, id, relations)
            return self._get(connection, kind, id)

        return await self.run(write, write=True)

    async def _update(self, kind, id, patch):
        changes = patch.model_dump(exclude_unset=True, exclude={"expected_revision"})
        if not changes:
            raise AgentError("AGENT_EMPTY_PATCH", status_code=422)

        def write(connection):
            old = self._get(connection, kind, id)
            if old.revision != patch.expected_revision:
                raise AgentError("AGENT_CONFIG_CONFLICT", status_code=409)
            values = (
                CREATE[kind]
                .model_validate(
                    {
                        **old.model_dump(include=set(CREATE[kind].model_fields)),
                        **changes,
                    }
                )
                .model_dump()
            )
            if kind == "agent":
                AiCatalog._get(self, connection, "model", values["model_id"], lock=True)
                self._relations(connection, id, values)
            if kind == "mcp":
                write_credential(values, patch, old)
            values["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
            values = encode(values)
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE agent_bot_{kind} SET "
                    + ",".join(f"{key}=%s" for key in values)
                    + ",revision=revision+1 WHERE id=%s AND revision=%s",
                    (*values.values(), id, patch.expected_revision),
                )
            return self._get(connection, kind, id)

        return await self.run(write, write=True)

    async def _delete(self, kind, id, revision):
        def write(connection):
            old = self._get(connection, kind, id)
            if old.enabled or old.revision != revision:
                raise AgentError("AGENT_CONFIG_CONFLICT", status_code=409)
            if kind != "agent" and query(
                connection,
                f"SELECT agent_id FROM agent_bot_agent_{kind} WHERE {kind}_id=%s LIMIT 1",
                (id,),
                one=True,
            ):
                raise AgentError("AGENT_CONFIG_IN_USE", status_code=409)
            with connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM agent_bot_{kind} WHERE id=%s", (id,))

        await self.run(write, write=True)

    async def _list(self, kind, limit, cursor):
        page_limit(limit)
        if cursor is not None and (
            not cursor.isascii() or not cursor.isdecimal() or len(cursor) > 20
        ):
            raise AgentError("AGENT_INVALID_PAGE", status_code=422)
        after = int(cursor or 0)

        def read(connection):
            rows = query(
                connection,
                f"SELECT id FROM agent_bot_{kind} WHERE id>%s ORDER BY id LIMIT %s",
                (after, limit + 1),
            )
            return Page[VIEW[kind]](
                items=[self._get(connection, kind, r["id"]) for r in rows[:limit]],
                next_cursor=str(rows[limit - 1]["id"]) if len(rows) > limit else None,
            )

        return await self.run(read)

    async def create_agent(self, data):
        return await self._create("agent", data)

    async def update_agent(self, id, patch):
        return await self._update("agent", id, patch)

    async def delete_agent(self, id, expected_revision):
        return await self._delete("agent", id, expected_revision)

    async def get_agent(self, id):
        return await self.run(lambda c: self._get(c, "agent", id))

    async def list_agents(self, *, limit=50, cursor=None):
        return await self._list("agent", limit, cursor)

    async def create_skill(self, data):
        return await self._create("skill", data)

    async def update_skill(self, id, patch):
        return await self._update("skill", id, patch)

    async def delete_skill(self, id, expected_revision):
        return await self._delete("skill", id, expected_revision)

    async def get_skill(self, id):
        return await self.run(lambda c: self._get(c, "skill", id))

    async def list_skills(self, *, limit=50, cursor=None):
        return await self._list("skill", limit, cursor)

    async def create_mcp(self, data):
        return await self._create("mcp", data)

    async def update_mcp(self, id, patch):
        return await self._update("mcp", id, patch)

    async def delete_mcp(self, id, expected_revision):
        return await self._delete("mcp", id, expected_revision)

    async def get_mcp(self, id):
        return await self.run(lambda c: self._get(c, "mcp", id))

    async def list_mcps(self, *, limit=50, cursor=None):
        return await self._list("mcp", limit, cursor)

    async def decision_mcp(self, code):
        """只解析已启用的固定决策服务，白名单收紧为 laya_predict。"""
        def read(connection):
            """目录代码来自服务器配置，查询参数不包含模型提供的地址。"""
            server = self._by_code(connection, "mcp", code)
            if server.category != "general" or not (
                {"laya_predict", "*"} & set(server.allowed_tools)
            ):
                raise AgentError("AUTO_SPEAK_TOOL_UNAVAILABLE", status_code=503)
            return server.model_copy(update={"allowed_tools": ("laya_predict",), "required": True})
        return await self.run(read)

    def _by_code(self, connection, kind, code):
        row = query(connection, f"SELECT id FROM agent_bot_{kind} WHERE code=%s", (code,), one=True)
        if row is None:
            raise AgentError("AGENT_CONFIG_NOT_FOUND", status_code=404)
        item = self._get(connection, kind, row["id"])
        if not item.enabled:
            raise AgentError("AGENT_CONFIG_DISABLED", status_code=409)
        return item

    async def snapshot(self, request: AgentRequest, *, search_code=None) -> AgentSnapshot:
        """在同一读取事务取得不可变配置，包含显式媒体能力。"""

        def read(connection):
            """一致性读取不持有行锁，避免与目录配置写入形成反向锁序。"""
            agent = (
                self._by_code(connection, "agent", request.agent_code)
                if request.agent_code
                else None
            )
            if agent:
                model = AiCatalog._get(self, connection, "model", agent.model_id)
            else:
                row = query(
                    connection,
                    "SELECT * FROM agent_bot_ai_model WHERE alias=%s",
                    (request.model,),
                    one=True,
                )
                if row is None:
                    raise AgentError("AGENT_MODEL_UNAVAILABLE", status_code=400)
                model = model_view(connection, row)
            provider = AiCatalog._get(self, connection, "provider", model.provider_id)
            if not model.enabled or not provider.enabled:
                raise AgentError("AGENT_MODEL_UNAVAILABLE", status_code=400)
            skills = (
                tuple(self._get(connection, "skill", id) for id in agent.skill_ids) if agent else ()
            )
            mcps = tuple(self._get(connection, "mcp", id) for id in agent.mcp_ids) if agent else ()
            if request.mcp_services is not None:
                if agent and not set(request.mcp_services) <= {m.code for m in mcps}:
                    raise AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403)
                mcps = tuple(
                    self._by_code(connection, "mcp", code) for code in request.mcp_services
                )
            if any(not item.enabled for item in (*skills, *mcps)):
                raise AgentError("AGENT_CONFIG_DISABLED", status_code=409)
            revisions = {f"skill:{s.id}": s.revision for s in skills}
            revisions.update({f"mcp:{m.id}": m.revision for m in mcps})
            revisions.update(
                {f"model:{model.id}": model.revision, f"provider:{provider.id}": provider.revision}
            )
            if agent:
                revisions[f"agent:{agent.id}"] = agent.revision
            selected = (
                request.tools
                if request.tools is not None
                else (agent.tools if agent else ("web_search",))
            )
            search = (
                self._by_code(connection, "mcp", search_code)
                if search_code and "web_search" in selected
                else None
            )
            if search is not None:
                if search.category != "search":
                    raise AgentError("AGENT_SEARCH_UNAVAILABLE", status_code=409)
                revisions[f"mcp:{search.id}"] = search.revision
            return AgentSnapshot(
                agent=agent,
                skills=skills,
                mcps=mcps,
                search_mcp=search,
                resolved=ResolvedModel(model=model, provider=provider),
                revisions=revisions,
            )

        return await self.run(read)

    async def readiness(self, agent_code=None):
        """引用列名检查本地目录及记录表，不为健康探测调用外部模型。"""
        try:

            def check(connection):
                """在同一只读事务检查必需结构，公开附件不要求新增历史表列。"""
                for kind, view in VIEW.items():
                    fields = set(view.model_fields) - {*RELATIONS}
                    query(
                        connection,
                        f"SELECT {sql_columns(sorted(fields))} FROM agent_bot_{kind} LIMIT 0",
                    )
                for kind in ("skill", "mcp"):
                    query(
                        connection, f"SELECT agent_id,{kind}_id FROM agent_bot_agent_{kind} LIMIT 0"
                    )
                query(
                    connection,
                    f"SELECT {sql_columns(set(RunStart.model_fields) - {'attachments'})} "
                    "FROM agent_bot_agent_run LIMIT 0",
                )
                query(
                    connection,
                    f"SELECT {sql_columns(StepWrite.model_fields)} "
                    "FROM agent_bot_agent_step LIMIT 0",
                )

            await self.run(check)
            if agent_code:
                await self.snapshot(
                    AgentRequest(
                        agent_code=agent_code,
                        prompt="readiness",
                        session_id="readiness",
                        request_id="readiness",
                    )
                )
            return AgentReadiness(
                initialized=True, agent_available=bool(agent_code), model_available=bool(agent_code)
            )
        except AiError as error:
            return AgentReadiness(initialized=error.code != "AI_NOT_INITIALIZED", code=error.code)
