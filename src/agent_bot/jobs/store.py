"""以数据库事务仲裁任务幂等、配额、执行权和投递确认。"""

import re
import uuid
from datetime import datetime, timedelta, timezone

from agent_bot.ai.database import AiDatabase, query
from agent_bot.ai.errors import AiError
from agent_bot.jobs.contracts import JOB_ID, QQ_ID, JobRecord, JobView

LEASE_SECONDS = 360
MAX_ATTEMPTS = 3


class JobStore(AiDatabase):
    """所有写入均沿用等待线程完成的事务边界，不自动执行 DDL。"""

    def __init__(self, database, *, connect, clock=None):
        """可注入时间源，生产统一使用 UTC。"""
        super().__init__(database, connect=connect)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def now(self):
        """SQL 使用无时区 UTC DATETIME，比较不依赖机器本地时区。"""
        return self.clock().astimezone(timezone.utc).replace(tzinfo=None)

    def _view(self, row, *, internal=False, summary=False):
        """按明确白名单构造对外视图，内部字段不进入工具结果。"""
        model = JobRecord if internal else JobView
        values = {key: row[key] for key in model.model_fields if key != 'autonomy_profile_id'}
        reference = row.get('model', '')
        if reference.startswith('autonomy:'):
            values['autonomy_profile_id'] = reference.split(':')[1]
        if summary:
            values.update(prompt=values["prompt"][:256], result=None)
        return model.model_validate(values)

    def _id(self, value, pattern=JOB_ID):
        """查询入口也验证标识，非法输入不能形成任意 SQL 参数语义。"""
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise AiError("JOB_INVALID_ID", status_code=422)

    def _owned(self, connection, owner, id):
        """任务查找必须同时匹配机器人、会话和发起人。"""
        self._id(id)
        row = query(
            connection,
            "SELECT * FROM agent_bot_job WHERE id=%s AND owner_key=%s",
            (id, owner.owner_key),
            one=True,
        )
        if row is None:
            raise AiError("JOB_NOT_FOUND", status_code=404)
        return row

    def _schedule(self, command, now):
        """只为新任务验证绝对时间，已创建请求的重放不重新排期。"""
        try:
            run_at = (
                now + timedelta(seconds=command.delay_seconds)
                if command.delay_seconds is not None
                else datetime.fromisoformat(command.run_at.replace("Z", "+00:00"))
            )
            if command.run_at is not None:
                if run_at.tzinfo is None:
                    raise ValueError("timezone required")
                run_at = run_at.astimezone(timezone.utc).replace(tzinfo=None)
            if not now < run_at <= now + timedelta(days=365):
                raise ValueError("schedule out of range")
        except (ValueError, OverflowError) as error:
            raise AiError("JOB_TIME_INVALID", status_code=422) from error
        return run_at

    async def create(self, scope, command):
        """所有者行锁保护配额，数据库唯一键保护一次请求只创建一次。"""
        if command.action != "create":
            raise AiError("JOB_COMMAND_INVALID", status_code=422)

        def write(connection):
            """先锁所有者再读取活动任务，避免可重复读快照导致配额竞争。"""
            with connection.cursor() as cursor:
                # 重复键更新取得排他锁，避免重复 INSERT 的共享锁升级发生竞争。
                cursor.execute(
                    "INSERT INTO agent_bot_job_owner (owner_key) VALUES (%s) "
                    "ON DUPLICATE KEY UPDATE owner_key=VALUES(owner_key)",
                    (scope.owner_key,),
                )
            query(
                connection,
                "SELECT owner_key FROM agent_bot_job_owner WHERE owner_key=%s FOR UPDATE",
                (scope.owner_key,),
                one=True,
            )
            existing = query(
                connection,
                "SELECT * FROM agent_bot_job WHERE deduplication_key=%s FOR UPDATE",
                (scope.deduplication_key,),
                one=True,
            )
            if existing:
                return self._view(existing)
            now = self.now()
            run_at = self._schedule(command, now)
            active = query(
                connection,
                "SELECT id FROM agent_bot_job WHERE owner_key=%s "
                "AND status IN ('PENDING','RUNNING') FOR UPDATE",
                (scope.owner_key,),
            )
            if len(active) >= 50:
                raise AiError("JOB_ACTIVE_LIMIT", status_code=409)
            values = {
                "id": uuid.uuid4().hex,
                "owner_key": scope.owner_key,
                "deduplication_key": scope.deduplication_key,
                **scope.model_dump(exclude={"request_id"}),
                "prompt": command.prompt.strip(),
                "status": "PENDING",
                "result": None,
                "error_code": None,
                "run_at": run_at,
                "created_at": now,
                "started_at": None,
                "completed_at": None,
                "delivered_at": None,
            }
            with connection.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO agent_bot_job ({','.join(values)}) "
                    f"VALUES ({','.join(['%s'] * len(values))})",
                    tuple(values.values()),
                )
            return self._view(values)

        return await self.run(write, write=True)

    async def get(self, owner, id):
        """读取当前所有者的一项完整状态。"""
        return await self.run(lambda connection: self._view(self._owned(connection, owner, id)))

    async def list_owned(self, owner):
        """只返回最近二十条摘要，避免大结果挤占模型上下文。"""
        return await self.run(
            lambda connection: tuple(
                self._view(row, summary=True)
                for row in query(
                    connection,
                    "SELECT * FROM agent_bot_job WHERE owner_key=%s "
                    "ORDER BY created_at DESC,id DESC LIMIT 20",
                    (owner.owner_key,),
                )
            )
        )

    async def cancel(self, owner, id):
        """取消只阻止后续结果发布，不声称撤回已经发生的模型费用。"""

        def write(connection):
            """同一事务内验证归属并条件更新，终态任务保持原状态。"""
            self._owned(connection, owner, id)
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE agent_bot_job SET status='CANCELLED',completed_at=%s "
                    "WHERE id=%s AND owner_key=%s AND status IN ('PENDING','RUNNING')",
                    (self.now(), id, owner.owner_key),
                )
                cursor.execute(
                    "UPDATE agent_bot_job_attempt SET status='CANCELLED',completed_at=%s "
                    "WHERE job_id=%s AND status='RUNNING'",
                    (self.now(), id),
                )
            return self._view(self._owned(connection, owner, id))

        return await self.run(write, write=True)

    async def claim_due(self):
        """领取到期或租约过期的任务，中断最多恢复至三次执行。"""
        now = self.now()

        def write(connection):
            """数据库决定唯一执行者，模型调用在提交抢占后开始。"""
            row = query(
                connection,
                "SELECT * FROM agent_bot_job WHERE (status='PENDING' AND run_at<=%s) "
                "OR (status='RUNNING' AND started_at<=%s) "
                "ORDER BY run_at,id LIMIT 1 FOR UPDATE",
                (now, now - timedelta(seconds=LEASE_SECONDS)),
                one=True,
            )
            if row is None:
                return None
            attempts = query(
                connection, "SELECT id FROM agent_bot_job_attempt WHERE job_id=%s",
                (row["id"],),
            )
            token = uuid.uuid4().hex
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE agent_bot_job_attempt SET status='INTERRUPTED',completed_at=%s,"
                    "error_code='EXECUTION_INTERRUPTED' WHERE job_id=%s AND status='RUNNING'",
                    (now, row["id"]),
                )
                if len(attempts) >= MAX_ATTEMPTS:
                    cursor.execute(
                        "UPDATE agent_bot_job SET status='FAILED',completed_at=%s,"
                        "error_code='RECOVERY_LIMIT_REACHED' WHERE id=%s",
                        (now, row["id"]),
                    )
                    return None
                cursor.execute(
                    "INSERT INTO agent_bot_job_attempt "
                    "(id,job_id,status,started_at,lease_expires_at) "
                    "VALUES (%s,%s,'RUNNING',%s,%s)",
                    (token, row["id"], now, now + timedelta(seconds=LEASE_SECONDS)),
                )
                cursor.execute(
                    "UPDATE agent_bot_job SET status='RUNNING',started_at=%s,"
                    "error_code=NULL,completed_at=NULL WHERE id=%s",
                    (now, row["id"]),
                )
            row["status"] = "RUNNING"
            row["lease_token"] = token
            return self._view(row, internal=True)

        return await self.run(write, write=True)

    async def finish(self, id, *, lease_token, result=None, error_code=None):
        """仅有效租约能提交结果；中断回到待执行，取消和旧租约不能覆盖新状态。"""
        self._id(id)
        self._id(lease_token)
        if error_code is None and (
            not isinstance(result, str) or not result.strip() or len(result) > 64000
        ):
            raise AiError("JOB_RESULT_INVALID", status_code=502)

        def write(connection):
            """先锁任务再检查租约，与领取、取消共用相同锁顺序。"""
            now = self.now()
            job = query(connection, "SELECT status FROM agent_bot_job WHERE id=%s FOR UPDATE",
                        (id,), one=True)
            attempt = query(
                connection, "SELECT id FROM agent_bot_job_attempt WHERE id=%s AND job_id=%s "
                "AND status='RUNNING' AND lease_expires_at>%s",
                (lease_token, id, now), one=True,
            )
            if job is None or job["status"] != "RUNNING" or attempt is None:
                return False
            interrupted = error_code == "EXECUTION_INTERRUPTED"
            status = "PENDING" if interrupted else "FAILED" if error_code else "SUCCEEDED"
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE agent_bot_job_attempt SET status=%s,error_code=%s,completed_at=%s "
                    "WHERE id=%s",
                    ("INTERRUPTED" if interrupted else status, error_code, now, lease_token),
                )
                cursor.execute(
                    "UPDATE agent_bot_job SET status=%s,result=%s,error_code=%s,completed_at=%s "
                    "WHERE id=%s AND status='RUNNING'",
                    (
                        status,
                        None if error_code else result,
                        error_code,
                        None if interrupted else now,
                        id,
                    ),
                )
            return True

        return await self.run(write, write=True)

    async def pending_results(self, bot_id, after_id=None):
        """只读读取一页成功未确认结果，不以轮询替代投递确认。"""
        self._id(bot_id, QQ_ID)
        if after_id is not None:
            self._id(after_id)
        return await self.run(
            lambda connection: tuple(
                self._view(row)
                for row in query(
                    connection,
                    "SELECT * FROM agent_bot_job WHERE bot_id=%s AND status='SUCCEEDED' "
                    "AND delivered_at IS NULL AND id>%s ORDER BY id LIMIT 10",
                    (bot_id, after_id or ""),
                )
            )
        )

    async def inspect(self, id):
        """本机运维入口读取任务与执行历史，不暴露系统提示、幂等摘要或凭据。"""
        self._id(id)

        def read(connection):
            """历史与任务在同一读事务中返回，不改变任务和投递状态。"""
            row = query(connection, "SELECT * FROM agent_bot_job WHERE id=%s", (id,), one=True)
            if row is None:
                raise AiError("JOB_NOT_FOUND", status_code=404)
            return {
                "job": self._view(row).model_dump(mode="json", by_alias=True),
                "delivered_at": row["delivered_at"],
                "attempts": query(
                    connection, "SELECT * FROM agent_bot_job_attempt WHERE job_id=%s "
                    "ORDER BY started_at,id", (id,),
                ),
            }

        return await self.run(read)

    async def acknowledge(self, bot_id, id):
        """仅机器人匹配的成功任务可幂等确认，保留历史记录。"""
        self._id(bot_id, QQ_ID)
        self._id(id)

        def write(connection):
            """成功发送后的确认不会改变任务结果，也不接受跨机器人操作。"""
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE agent_bot_job SET delivered_at=%s WHERE id=%s AND bot_id=%s "
                    "AND status='SUCCEEDED' AND delivered_at IS NULL",
                    (self.now(), id, bot_id),
                )

        await self.run(write, write=True)
