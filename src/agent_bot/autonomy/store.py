"""持续身份、事件、计划和步骤检查点的跨后端事务；网络调用不得进入这些事务。"""

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from agent_bot.ai.database import AiDatabase, query
from agent_bot.ai.errors import AiError
from agent_bot.autonomy.contracts import (
    EpisodeCreate,
    Plan,
    Policy,
    ProfileCreate,
    ProfileUpdate,
    SelfState,
    Step,
    TaskCreate,
)
from agent_bot.memory.policy import time_value

LEASE_SECONDS = 180
MODEL_ACTIONS = {"plan", "draft", "consolidate"}
TERMINAL = {"succeeded", "failed", "cancelled"}


def dumps(value: Any) -> str:
    """所有JSON使用统一稳定格式，时间显式序列化，不包含模型凭据。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def quiet(policy: Policy, now: datetime) -> bool:
    """静默区间按北京时间半开小时区间，起止相同时表示关闭静默。"""
    hour = (now + timedelta(hours=8)).hour
    start, end = policy.quiet_start_hour, policy.quiet_end_hour
    if start == end:
        return False
    return start <= hour < end if start < end else hour >= start or hour < end


def approval_policy(policy: Policy) -> dict:
    """审批固定实质行动策略；单纯暂停和恢复不会改变已审核的权限。"""
    return policy.model_dump(mode="json", exclude={"paused"})


class AutonomyStore(AiDatabase):
    """每次变更先锁身份行，模型预算、暂停和任务领取在同一事务内裁定。"""

    def __init__(self, params, *, connect, secret: str, clock=None, local_only=False):
        """密钥只派生命名空间和所有者摘要，不写入业务文档。"""
        super().__init__(params, connect=connect)
        self.namespace = hmac.new(secret.encode(), b"agent_bot:autonomy:v1", "sha256").hexdigest()
        self.local_only = local_only
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def evolution_subject(self, id: str) -> str:
        """独立主体的演进证据隔离于平台用户和其他Agent。"""
        return hashlib.sha256((self.namespace + ':local-evolution:' + id).encode()).hexdigest()

    async def evolution_profile(self, id: str) -> int | None:
        """从可信身份定位专属画像，网页不能指定别人的演进主体。"""
        def read(connection):
            """核对身份后仅查询其通用范围画像，角色画像不能混入。"""
            self._profile(connection, id)
            row = query(connection, 'SELECT id FROM agent_bot_evolution_state '
                        "WHERE scope_key='global' AND subject_key=%s",
                        (self.evolution_subject(id),), one=True)
            return row['id'] if row else None
        return await self.run(read)

    def now(self) -> datetime:
        """驱动写入无时区UTC，API文档中时间带显式UTC偏移。"""
        return time_value(self.clock())

    def _profile(self, connection, id: str, *, lock=False) -> dict:
        """命名空间隔离全部接口；模型、旧密钥或其他身份不能越界读取。"""
        row = query(
            connection,
            "SELECT * FROM agent_bot_autonomy_profile "
            "WHERE id=%s AND namespace=%s" + (" FOR UPDATE" if lock else ""),
            (id, self.namespace),
            one=True,
        )
        if row is None:
            raise AiError("AUTONOMY_NOT_FOUND", status_code=404)
        document = json.loads(row['document_json'])
        if self.local_only and document['owner'].get('channel') != 'local':
            raise AiError('AUTONOMY_NOT_FOUND', status_code=404)
        return {**row, "document": document}

    def _audit(self, connection, id: str, kind: str, document: dict, subject=None) -> None:
        """变更与审计一起提交，只有受控错误码进入审计，正文仅保留必要证据和设定。"""
        # 同一身份写入均持有父行锁；相同微秒或时钟回拨时仍按提交顺序分页。
        previous = query(connection,
                         'SELECT id FROM agent_bot_autonomy_audit WHERE profile_id=%s '
                         'ORDER BY id DESC LIMIT 1', (id,), one=True)
        stamp = self.now()
        if previous:
            last = datetime.strptime(previous['id'][:20], '%Y%m%d%H%M%S%f')
            stamp = max(stamp, last + timedelta(microseconds=1))
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO agent_bot_autonomy_audit "
                "(id,profile_id,kind,subject_id,document_json,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    stamp.strftime("%Y%m%d%H%M%S%f") + uuid.uuid4().hex[:12],
                    id,
                    kind,
                    subject,
                    dumps(document),
                    self.now(),
                ),
            )

    def _save_profile(self, connection, row: dict, *, wake=None) -> None:
        """后台进度不修改管理设定版本，避免频繁步骤使暂停或人工编辑无法提交。"""
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE agent_bot_autonomy_profile SET document_json=%s,revision=%s,"
                "next_wake_at=%s,updated_at=%s WHERE id=%s",
                (
                    dumps(row["document"]),
                    row["revision"],
                    wake if wake is not None else row["next_wake_at"],
                    self.now(),
                    row["id"],
                ),
            )

    def _view(self, row: dict) -> dict:
        """公开视图不包含内部命名空间或所有者散列，未完成目标由任务列表查询。"""
        return {"id": row["id"], "revision": row["revision"], **row["document"]}

    async def create_profile(self, body: ProfileCreate) -> dict:
        """唯一所有者键保证重启认领原身份；重复创建只返回原版本，不覆盖设定。"""

        def write(connection):
            """唯一键是并发创建的最终约束，失败由现有数据库错误协议返回。"""
            if self.local_only and body.owner.model_dump().get('channel') != 'local':
                raise AiError('AUTONOMY_LOCAL_CHANNEL_REQUIRED', status_code=409)
            if body.owner.model_dump().get('channel') == 'local' and body.policy.notify_qq:
                raise AiError('AUTONOMY_LOCAL_CHANNEL_REQUIRED', status_code=409)
            owner = hashlib.sha256((self.namespace + body.owner.owner_key).encode()).hexdigest()
            existing = query(
                connection,
                "SELECT id FROM agent_bot_autonomy_profile WHERE namespace=%s AND owner_key=%s",
                (self.namespace, owner),
                one=True,
            )
            if existing:
                return self._view(self._profile(connection, existing["id"]))
            id, now = uuid.uuid4().hex, self.now()
            document = {
                "owner": body.owner.model_dump(mode="json"),
                "identity": body.identity.model_dump(),
                "policy": body.policy.model_dump(),
                "state": SelfState().model_dump(),
                "usage": {},
            }
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO agent_bot_autonomy_profile "
                    "(id,namespace,owner_key,revision,document_json,next_wake_at,"
                    "created_at,updated_at) VALUES (%s,%s,%s,1,%s,%s,%s,%s)",
                    (id, self.namespace, owner, dumps(document), now, now, now),
                )
            self._audit(connection, id, "identity_created", document)
            return self._view(self._profile(connection, id))

        return await self.run(write, write=True)

    async def get_profile(self, id: str) -> dict:
        """唤醒与管理读取相同持久身份，不依赖上次聊天上下文。"""
        return await self.run(lambda db: self._view(self._profile(db, id)))

    async def for_owner(self, owner) -> dict | None:
        """真实会话按不可变绑定认领已有身份，普通聊天不能自动创建或切换身份。"""
        key = hashlib.sha256((self.namespace + owner.owner_key).encode()).hexdigest()

        def read(connection):
            """查找不使用昵称或模型给出的称谓，跨用户和密钥不会匹配。"""
            row = query(
                connection,
                "SELECT id FROM agent_bot_autonomy_profile WHERE namespace=%s AND owner_key=%s",
                (self.namespace, key),
                one=True,
            )
            return self._view(self._profile(connection, row["id"])) if row else None

        return await self.run(read)

    async def profiles(self, *, after: str = "", limit: int = 100) -> list[dict]:
        """稳定主键游标避免大量身份时只维护最前一页。"""

        def read(connection):
            """列表仅返回当前密钥空间，后台与管理共用有界分页。"""
            rows = query(
                connection,
                "SELECT id FROM agent_bot_autonomy_profile "
                "WHERE namespace=%s AND id>%s "
                + ("AND JSON_EXTRACT(document_json,'$.owner.channel')='local' "
                   if self.local_only else "")
                + "ORDER BY id LIMIT %s",
                (self.namespace, after, limit),
            )
            return [self._view(self._profile(connection, row["id"])) for row in rows]

        return await self.run(read)

    async def update_profile(self, id: str, body: ProfileUpdate) -> dict:
        """身份和策略版本一起审计；编辑状态时保留由执行器认领的在途任务标识。"""

        def write(connection):
            """所见版本必须匹配，设置变化后原待审计划不能自动获得新权限。"""
            row = self._profile(connection, id, lock=True)
            if row["revision"] != body.expected_revision:
                raise AiError("AUTONOMY_CONFLICT", status_code=409)
            for key in ("identity", "policy", "state"):
                value = getattr(body, key)
                if value is not None:
                    previous_task = row["document"]["state"]["current_task"]
                    row["document"][key] = value.model_dump()
                    if key == "state":
                        row["document"][key]["current_task"] = previous_task
            if (row['document']['owner'].get('channel') == 'local'
                    and row['document']['policy']['notify_qq']):
                raise AiError('AUTONOMY_LOCAL_CHANNEL_REQUIRED', status_code=409)
            row["revision"] += 1
            self._save_profile(connection, row, wake=self.now())
            self._audit(connection, id, "identity_updated", self._view(row))
            return self._view(row)

        return await self.run(write, write=True)

    async def pause(self, id: str | None = None) -> None:
        """无版本紧急暂停可覆盖并发编辑，空ID暂停当前命名空间全部身份。"""
        ids = [id] if id else []
        if id is None:
            after = ""
            while page := await self.profiles(after=after):
                ids.extend(row["id"] for row in page)
                after = page[-1]["id"]
        for profile_id in ids:

            def write(connection, profile_id=profile_id):
                """锁定单个身份使暂停与动作领取互斥，不撤销已经开始的外部副作用。"""
                row = self._profile(connection, profile_id, lock=True)
                row["document"]["policy"]["paused"] = True
                row["revision"] += 1
                self._save_profile(connection, row)
                self._audit(connection, profile_id, "paused", {"revision": row["revision"]})

            await self.run(write, write=True)

    async def ingest(self, id: str, body: EpisodeCreate) -> dict:
        """情节按真实事件去重；相同事件不同原文明确冲突，不覆盖原证据。"""

        return await self.run(lambda db: self._ingest(db, id, body), write=True)

    def _ingest(self, connection, id: str, body: EpisodeCreate) -> dict:
        """来源和任务共用身份锁，失败回滚整条入站消息。"""
        row = self._profile(connection, id, lock=True)
        episode_id = hashlib.sha256((id + ":" + body.event_id).encode()).hexdigest()[:32]
        existing = query(
            connection,
            "SELECT document_json FROM agent_bot_autonomy_episode WHERE id=%s",
            (episode_id,),
            one=True,
        )
        document = body.model_dump(mode="json")
        document.update(summary=None, forgotten=False, processed=False)
        if existing:
            original = json.loads(existing["document_json"])
            if original.get("text") != body.text:
                raise AiError("AUTONOMY_CONFLICT", status_code=409)
            return {"id": episode_id, **original}
        policy = Policy.model_validate(row["document"]["policy"])
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO agent_bot_autonomy_episode "
                "(id,profile_id,event_id,document_json,expires_at,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    episode_id,
                    id,
                    body.event_id,
                    dumps(document),
                    self.now() + timedelta(days=policy.episode_retention_days),
                    self.now(),
                ),
            )
        self._save_profile(connection, row, wake=self.now())
        self._audit(connection, id, "episode_ingested", {"episode_id": episode_id})
        return {"id": episode_id, **document}

    async def send_message(self, id: str, request_id: str, text: str) -> dict:
        """独立网页消息直接创建回复任务，暂停仍可存入队列且不依赖任务发现开关。"""
        def write(connection):
            """用户正文与回复检查点原子写入，重试同键同文只认领一次。"""
            profile = self._profile(connection, id, lock=True)
            if profile['document']['owner'].get('channel') != 'local':
                raise AiError('AUTONOMY_LOCAL_CHANNEL_REQUIRED', status_code=409)
            episode = self._ingest(connection, id, EpisodeCreate(
                event_id=request_id, text=text, occurred_at=self.now().replace(tzinfo=timezone.utc),
            ))
            # 网页消息已有显式回复任务，自动发现不应重复创建同一请求。
            document = {key: value for key, value in episode.items() if key != 'id'}
            document['task_discovered'] = True
            with connection.cursor() as cursor:
                cursor.execute('UPDATE agent_bot_autonomy_episode SET document_json=%s WHERE id=%s',
                               (dumps(document), episode['id']))
            task = self._create_task(connection, profile, TaskCreate(
                request_id='chat:' + request_id, goal=text,
                steps=(Step(action='draft', text='回复当前用户消息'),),
            ), episode=episode['id'])
            return task
        return await self.run(write, write=True)

    async def chat_context(self, id: str, task_id: str) -> list[dict]:
        """按会话的提交顺序读取先前已完成回复，不将其他参与者或未来消息混入上下文。"""
        def read(connection):
            """历史最多六轮和2400字符，重启从持久检查点恢复。"""
            self._profile(connection, id)
            current = self._task(connection, id, task_id)
            if not current['request_id'].startswith('chat:'):
                return []
            rows = query(connection,
                "SELECT t.document_json FROM agent_bot_autonomy_audit a "
                "JOIN agent_bot_autonomy_task t ON t.id=a.subject_id "
                "WHERE a.profile_id=%s AND a.kind='task_created' AND a.id< "
                "(SELECT id FROM agent_bot_autonomy_audit WHERE profile_id=%s "
                "AND kind='task_created' AND subject_id=%s) "
                "AND t.request_id LIKE 'chat:%%' AND t.status='succeeded' "
                "ORDER BY a.id DESC LIMIT 6", (id, id, task_id))
            result, remaining = [], 2400
            for row in rows:
                doc = json.loads(row['document_json'])
                if not doc['outputs'] or not isinstance(doc['outputs'][0], str):
                    continue
                pair = [{'role': 'user', 'content': doc['goal'][:600]},
                        {'role': 'assistant', 'content': doc['outputs'][0][:600]}]
                size = sum(len(item['content']) for item in pair)
                if size > remaining:
                    break
                result = pair + result
                remaining -= size
            return result
        return await self.run(read)

    def _task(self, connection, profile_id: str, id: str) -> dict:
        """任何步骤或审批都需同时匹配固定身份与任务ID。"""
        row = query(
            connection,
            "SELECT * FROM agent_bot_autonomy_task WHERE id=%s AND profile_id=%s",
            (id, profile_id),
            one=True,
        )
        if row is None:
            raise AiError("AUTONOMY_NOT_FOUND", status_code=404)
        return {**row, "document": json.loads(row["document_json"])}

    def _task_view(self, row: dict) -> dict:
        """管理视图包含进度和审批版本，租约令牌仅留给实际执行器。"""
        return {
            "id": row["id"],
            "profile_id": row["profile_id"],
            "revision": row["revision"],
            "status": row["status"],
            "attempts": row["attempts"],
            "request_id": row["request_id"],
            "next_run_at": time_value(row["next_run_at"]).replace(tzinfo=timezone.utc).isoformat(),
            **row["document"],
        }

    def _create_task(self, connection, profile: dict, body: TaskCreate, *, episode=None) -> dict:
        """调用者已锁身份，事件发现与管理员创建使用同一幂等和容量约束。"""
        id = profile["id"]
        existing = query(
            connection,
            "SELECT id FROM agent_bot_autonomy_task WHERE profile_id=%s AND request_id=%s",
            (id, body.request_id),
            one=True,
        )
        if existing:
            return self._task_view(self._task(connection, id, existing["id"]))
        count = query(
            connection,
            "SELECT COUNT(*) AS count FROM agent_bot_autonomy_task "
            "WHERE profile_id=%s AND status NOT IN ('succeeded','failed','cancelled')",
            (id,),
            one=True,
        )["count"]
        if count >= 50:
            raise AiError("AUTONOMY_TASK_LIMIT", status_code=409)
        task_id, now = uuid.uuid4().hex, self.now()
        steps = [step.model_dump() for step in body.steps] or [Step(action="plan").model_dump()]
        if episode is not None and body.request_id.startswith("consolidate:"):
            steps = [Step(action="consolidate").model_dump()]
        document = {
            "goal": body.goal,
            "steps": steps,
            "cursor": 0,
            "outputs": [],
            "identity": profile["document"]["identity"],
            "identity_revision": profile["revision"],
            "episode_id": episode,
            "error_code": None,
            "approved_revision": None,
            "notified": False,
        }
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO agent_bot_autonomy_task "
                "(id,profile_id,request_id,revision,status,document_json,attempts,"
                "next_run_at,created_at,updated_at) VALUES (%s,%s,%s,1,%s,%s,0,%s,%s,%s)",
                (
                    task_id,
                    id,
                    body.request_id,
                    "pending",
                    dumps(document),
                    time_value(body.run_at) if body.run_at else now,
                    now,
                    now,
                ),
            )
        self._audit(connection, id, "task_created", {"goal": body.goal, "steps": steps}, task_id)
        return self._task_view(self._task(connection, id, task_id))

    async def create_task(self, id: str, body: TaskCreate) -> dict:
        """明确任务无需聊天维持执行；原身份快照固定，暂停和预算仍按当前策略。"""
        return await self.run(
            lambda db: self._create_task(db, self._profile(db, id, lock=True), body), write=True
        )

    async def tasks(self, id: str, *, after: str = "", limit: int = 50) -> list[dict]:
        """任务列表提供未完成事项、检查点、结果和恢复错误。"""

        def read(connection):
            """先检查命名空间，再按稳定主键分页。"""
            self._profile(connection, id)
            return [
                self._task_view({**row, "document": json.loads(row["document_json"])})
                for row in query(
                    connection,
                    "SELECT * FROM agent_bot_autonomy_task "
                    "WHERE profile_id=%s AND id>%s ORDER BY id LIMIT %s",
                    (id, after, limit),
                )
            ]

        return await self.run(read)

    async def decide_task(self, id: str, task_id: str, revision: int, approve: bool) -> dict:
        """批准绑定当前计划；取消会使旧执行令牌失效，后续结果不能重开任务。"""

        def write(connection):
            """审批不扩大工具白名单或预算；仍由领取事务再次校验。"""
            profile = self._profile(connection, id, lock=True)
            row = self._task(connection, id, task_id)
            if (
                row["revision"] != revision
                or row["status"] in TERMINAL
                or (approve and row["status"] != "approval")
            ):
                raise AiError("AUTONOMY_CONFLICT", status_code=409)
            row["document"]["approved_revision"] = revision if approve else None
            row["document"]["approved_policy"] = (
                approval_policy(Policy.model_validate(profile["document"]["policy"]))
                if approve
                else None
            )
            row["status"] = "pending" if approve else "cancelled"
            if not approve and profile["document"]["state"]["current_task"] == task_id:
                profile["document"]["state"].update(current_task=None, activity="sleeping")
                self._save_profile(connection, profile)
            self._save_task(connection, row, token=None, next_run=self.now())
            self._audit(
                connection,
                id,
                "task_approved" if approve else "task_cancelled",
                {"revision": revision},
                task_id,
            )
            return self._task_view(row)

        return await self.run(write, write=True)

    def _save_task(self, connection, row: dict, *, token=None, expires=None, next_run=None) -> None:
        """状态、检查点与令牌一次提交，终态与步骤推进没有中间可见窗口。"""
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE agent_bot_autonomy_task SET revision=%s,status=%s,document_json=%s,"
                "attempts=%s,lease_token=%s,lease_expires_at=%s,next_run_at=%s,updated_at=%s "
                "WHERE id=%s",
                (
                    row["revision"],
                    row["status"],
                    dumps(row["document"]),
                    row["attempts"],
                    token,
                    expires,
                    next_run or row["next_run_at"],
                    self.now(),
                    row["id"],
                ),
            )

    async def discover(self, id: str) -> None:
        """只从宿主确认的用户任务意图发现工作；到期情节遗忘原文并保留墓碑。"""

        def write(connection):
            """发现与情节消费标记同事务提交，重启不能重复排期。"""
            profile = self._profile(connection, id, lock=True)
            policy = Policy.model_validate(profile["document"]["policy"])
            if policy.paused or time_value(profile["next_wake_at"]) > self.now():
                return
            if "review" in policy.allowed_actions:
                day = (self.now() + timedelta(hours=8)).date().isoformat()
                self._create_task(
                    connection,
                    profile,
                    TaskCreate(
                        request_id="daily-review:" + day,
                        goal="每日状态和记忆复盘",
                        steps=(Step(action="review"),),
                    ),
                )
            after = profile["document"].get("episode_cursor", "")
            rows = query(
                connection,
                "SELECT * FROM agent_bot_autonomy_episode "
                "WHERE profile_id=%s AND id>%s ORDER BY id LIMIT 100",
                (id, after),
            )
            for row in rows:
                episode = json.loads(row["document_json"])
                if time_value(row["expires_at"]) <= self.now():
                    if not episode["forgotten"]:
                        episode.update(text="", summary=None, forgotten=True)
                        self._audit(connection, id, "episode_forgotten", {"episode_id": row["id"]})
                else:
                    if (
                        policy.discover_tasks
                        and episode["task_requested"]
                        and not episode.get("task_discovered")
                    ):
                        self._create_task(
                            connection,
                            profile,
                            TaskCreate(
                                request_id="discover:" + row["id"],
                                goal=episode["text"][:4000],
                            ),
                            episode=row["id"],
                        )
                        episode["task_discovered"] = True
                    if policy.auto_consolidate and not episode.get("consolidation_scheduled"):
                        self._create_task(
                            connection,
                            profile,
                            TaskCreate(
                                request_id="consolidate:" + row["id"],
                                goal="整理有来源的情节记忆",
                            ),
                            episode=row["id"],
                        )
                        episode["consolidation_scheduled"] = True
                    episode["processed"] = True
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE agent_bot_autonomy_episode SET document_json=%s WHERE id=%s",
                        (dumps(episode), row["id"]),
                    )
            profile["document"]["episode_cursor"] = rows[-1]["id"] if len(rows) == 100 else ""
            self._save_profile(connection, profile, wake=self.now() + timedelta(seconds=60))

        await self.run(write, write=True)

    def _usage(self, document: dict) -> dict:
        """预算按北京时间切日；消费预留不退款，崩溃后不会低估已发生的费用。"""
        day = (self.now() + timedelta(hours=8)).date().isoformat()
        usage = document["usage"]
        if usage.get("date") != day:
            document["usage"] = {
                "date": day,
                "model_calls": 0,
                "reserved_cost": 0,
                "reports": 0,
                "last_report_at": usage.get("last_report_at"),
                "last_report_digest": usage.get("last_report_digest"),
            }
        return document["usage"]

    async def claim(self, id: str) -> dict | None:
        """当前策略、步骤权限和租约在同一锁下检查；每次仅领取一个有限动作。"""

        def write(connection):
            """预算在动作开始前预留，审批、静默或上限不足转为有原因的等待。"""
            profile = self._profile(connection, id, lock=True)
            policy = Policy.model_validate(profile["document"]["policy"])
            if policy.paused:
                return None
            running = query(
                connection,
                "SELECT id FROM agent_bot_autonomy_task WHERE profile_id=%s "
                "AND status='running' AND lease_expires_at>%s LIMIT 1",
                (id, self.now()),
                one=True,
            )
            if running:
                return None
            rows = query(
                connection,
                "SELECT * FROM agent_bot_autonomy_task WHERE profile_id=%s "
                "AND status IN ('pending','retry','blocked','running') AND next_run_at<=%s "
                "ORDER BY next_run_at,id LIMIT 50",
                (id, self.now()),
            )
            for item in rows:
                row = {**item, "document": json.loads(item["document_json"])}
                if row["status"] == "running" and time_value(row["lease_expires_at"]) > self.now():
                    continue
                doc = row["document"]
                if row['request_id'].startswith('chat:'):
                    prior = query(connection,
                        "SELECT t.id FROM agent_bot_autonomy_task t "
                        "JOIN agent_bot_autonomy_audit a ON a.subject_id=t.id "
                        "WHERE a.profile_id=%s AND a.kind='task_created' "
                        "AND a.id<(SELECT id FROM agent_bot_autonomy_audit "
                        "WHERE profile_id=%s AND kind='task_created' AND subject_id=%s) "
                        "AND t.request_id LIKE 'chat:%%' "
                        "AND t.status NOT IN ('succeeded','failed','cancelled') LIMIT 1",
                        (id, id, row['id']), one=True)
                    if prior:
                        continue
                step = Step.model_validate(doc["steps"][doc["cursor"]])
                code = None
                if row["attempts"] >= policy.max_attempts:
                    row["status"], code = "failed", "RECOVERY_LIMIT_REACHED"
                elif step.action not in policy.allowed_actions:
                    row["status"], code = "approval", "ACTION_NOT_ALLOWED"
                elif (
                    step.action == "report"
                    and policy.notify_qq
                    and (
                        doc["approved_revision"] != row["revision"]
                        or doc.get("approved_policy") != approval_policy(policy)
                    )
                ):
                    row["status"], code = "approval", "REPORT_APPROVAL_REQUIRED"
                elif step.action in MODEL_ACTIONS and (
                    not policy.model or policy.input_cost_per_million is None
                ):
                    row["status"], code = "blocked", "MODEL_PRICE_OR_SELECTION_REQUIRED"
                elif step.action == "report" and quiet(policy, self.now()):
                    row["status"], code = "blocked", "QUIET_HOURS"
                usage = self._usage(profile["document"])
                # 输入按UTF8字节上界及固定协议余量估算token，输出硬限制512token。
                reserve = (
                    34000 * (policy.input_cost_per_million or 0)
                    + 512 * (policy.output_cost_per_million or 0)
                ) / 1_000_000
                if (
                    code is None
                    and step.action in MODEL_ACTIONS
                    and (
                        usage["model_calls"] >= policy.max_model_calls_per_day
                        or usage["reserved_cost"] + reserve > policy.daily_cost_limit
                    )
                ):
                    row["status"], code = "blocked", "DAILY_MODEL_BUDGET"
                if (
                    code is None
                    and step.action == "report"
                    and str(doc["cursor"]) not in doc.get("report_receipts", {})
                    and (
                        usage["reports"] >= policy.max_reports_per_day
                        or (
                            usage["last_report_at"]
                            and (self.now() - time_value(usage["last_report_at"])).total_seconds()
                            < policy.min_report_interval_seconds
                        )
                    )
                ):
                    row["status"], code = "blocked", "REPORT_RATE_LIMIT"
                if code:
                    changed = doc["error_code"] != code
                    doc["error_code"] = code
                    self._save_task(connection, row, next_run=self.now() + timedelta(seconds=60))
                    if changed:
                        self._audit(connection, id, "step_blocked", {"code": code}, row["id"])
                    if row["status"] == "failed":
                        state = profile["document"]["state"]
                        if state["current_task"] == row["id"]:
                            state.update(
                                current_task=None, activity="sleeping", emotion="concerned"
                            )
                        state["recent_errors"] = [code, *state["recent_errors"]][:10]
                        self._terminal_notice(connection, profile, row)
                    continue
                if step.action in MODEL_ACTIONS:
                    usage["model_calls"] += 1
                    usage["reserved_cost"] += reserve
                row["status"], row["attempts"] = "running", row["attempts"] + 1
                doc["error_code"] = None
                token = uuid.uuid4().hex
                self._save_task(
                    connection,
                    row,
                    token=token,
                    expires=self.now() + timedelta(seconds=LEASE_SECONDS),
                )
                state = profile["document"]["state"]
                state.update(
                    current_task=row["id"],
                    current_goal=doc["goal"][:2000],
                    activity=step.action,
                    emotion="focused",
                )
                self._save_profile(connection, profile)
                self._audit(
                    connection,
                    id,
                    "step_started",
                    {"step": doc["cursor"], "action": step.action, "attempt": row["attempts"]},
                    row["id"],
                )
                return {
                    **self._task_view(row),
                    "lease_token": token,
                    "step": step.model_dump(),
                    "policy": policy.model_dump(),
                    "owner": profile["document"]["owner"],
                }
            self._save_profile(connection, profile)
            return None

        return await self.run(write, write=True)

    def _terminal_notice(self, connection, profile: dict, task: dict) -> None:
        """已启用QQ汇报时把终态汇报作为独立待审动作持久化，失败报告不递归派生。"""
        doc = task["document"]
        if (
            not profile["document"]["policy"]["notify_qq"]
            or task["status"] not in {"succeeded", "failed"}
            or any(
                step["action"] == "report"
                for step in doc["steps"][: min(doc["cursor"] + 1, len(doc["steps"]))]
            )
        ):
            return
        outcome = "任务完成" if task["status"] == "succeeded" else "任务失败"
        text = outcome + "：" + doc["goal"][:1800]
        if doc["error_code"]:
            text += "\n错误：" + doc["error_code"]
        self._create_task(
            connection,
            profile,
            TaskCreate(
                request_id="terminal-report:" + task["id"],
                goal=outcome + "汇报",
                steps=(Step(action="report", text=text),),
            ),
        )

    async def finish(
        self,
        id: str,
        task_id: str,
        token: str,
        *,
        output: Any = None,
        error: str | None = None,
        plan: list[dict] | None = None,
    ) -> bool:
        """只接受当前未过期令牌；每步结果与进度一起提交，完成步骤不会重放。"""

        def write(connection):
            """失败按有界退避重试，令牌或任务被取消时迟到输出不产生任何变更。"""
            profile = self._profile(connection, id, lock=True)
            row = self._task(connection, id, task_id)
            if (
                row["status"] != "running"
                or row["lease_token"] != token
                or time_value(row["lease_expires_at"]) <= self.now()
            ):
                return False
            doc = row["document"]
            if error:
                doc["error_code"] = error
                policy = Policy.model_validate(profile["document"]["policy"])
                row["status"] = "failed" if row["attempts"] >= policy.max_attempts else "retry"
                next_run = self.now() + timedelta(seconds=min(300, 10 * 2 ** row["attempts"]))
                errors = profile["document"]["state"]["recent_errors"]
                profile["document"]["state"].update(
                    recent_errors=[error, *errors][:10], emotion="concerned"
                )
            elif plan is not None:
                validated = [step.model_dump() for step in Plan(steps=plan).steps]
                doc.update(steps=validated, cursor=0, approved_revision=None)
                row["revision"] += 1
                row["status"], row["attempts"] = "pending", 0
                next_run = self.now()
            else:
                doc["error_code"] = None
                doc["outputs"].append(output)
                doc["cursor"] += 1
                row["status"] = "succeeded" if doc["cursor"] == len(doc["steps"]) else "pending"
                row["attempts"] = 0
                next_run = self.now()
            self._save_task(connection, row, next_run=next_run)
            self._terminal_notice(connection, profile, row)
            state = profile["document"]["state"]
            if state["current_task"] == task_id:
                state.update(current_task=None, activity="sleeping")
            self._save_profile(connection, profile)
            self._audit(
                connection,
                id,
                "step_failed" if error else "step_completed",
                {"step": doc["cursor"], "status": row["status"], "error_code": error},
                task_id,
            )
            return True

        return await self.run(write, write=True)

    async def audit(self, id: str, *, before: str | None = None, limit: int = 50) -> dict:
        """时间和ID复合游标固定倒序，不允许按用户输入拼接任意列或表。"""

        def read(connection):
            """原始来源和身份历史只向绑定命名空间的管理权限提供。"""
            self._profile(connection, id)
            rows = query(
                connection,
                "SELECT * FROM agent_bot_autonomy_audit WHERE profile_id=%s "
                + ("AND id<%s " if before else "")
                + "ORDER BY id DESC LIMIT %s",
                (id, *((before,) if before else ()), limit + 1),
            )
            return {
                "items": [
                    {**row, "document_json": json.loads(row["document_json"])}
                    for row in rows[:limit]
                ],
                "next_before": rows[limit - 1]["id"] if len(rows) > limit else None,
            }

        return await self.run(read)

    async def conversations(self, id: str, *, before: str | None = None, limit: int = 20) -> dict:
        """以已有创建审计为时间游标重建对话，不复制正文或新增独立的历史权威。"""

        def read(connection):
            """校验身份后读取当前检查点；过期来源即刻隐藏，投递状态以任务回执为准。"""
            self._profile(connection, id)
            rows = query(
                connection,
                "SELECT id,kind,subject_id,document_json,created_at "
                "FROM agent_bot_autonomy_audit WHERE profile_id=%s "
                "AND kind IN ('episode_ingested','task_created') "
                + ("AND id<%s " if before else "")
                + "ORDER BY id DESC LIMIT %s",
                (id, *((before,) if before else ()), limit + 1),
            )
            items = []
            for row in rows[:limit]:
                task = None
                episode_id = json.loads(row['document_json']).get('episode_id')
                if row['kind'] == 'task_created':
                    task = self._task_view(self._task(connection, id, row['subject_id']))
                    episode_id = task['episode_id']
                    # 富化的是读取副本，不写回检查点，也不把入队误报为已发送。
                    for output in task['outputs']:
                        if not isinstance(output, dict) or not output.get('queued'):
                            continue
                        receipt = query(
                            connection,
                            "SELECT delivered_at FROM agent_bot_job WHERE id=%s",
                            (output['report_id'],), one=True,
                        )
                        output['delivery'] = (
                            'delivered' if receipt and receipt['delivered_at'] else 'queued'
                        )
                episode = None
                if episode_id:
                    source = query(
                        connection,
                        "SELECT document_json,expires_at FROM agent_bot_autonomy_episode "
                        "WHERE id=%s AND profile_id=%s", (episode_id, id), one=True,
                    )
                    if source and time_value(source['expires_at']) > self.now():
                        document = json.loads(source['document_json'])
                        if not document['forgotten']:
                            episode = {'id': episode_id, **document}
                items.append({
                    'id': row['id'], 'created_at': time_value(row['created_at']).replace(
                        tzinfo=timezone.utc).isoformat(),
                    'task': task, 'episode': episode,
                    'source_expired': bool(episode_id and episode is None),
                })
            return {
                'items': items,
                'next_before': rows[limit - 1]['id'] if len(rows) > limit else None,
            }

        return await self.run(read)

    async def record_evaluation(self, id: str, result: dict) -> None:
        """固定评测结果与样本版本写入审计，不因评分改写身份或业务事实。"""

        def write(connection):
            """评测同样遵守命名空间边界。"""
            self._profile(connection, id, lock=True)
            self._audit(connection, id, "evaluation", result)

        await self.run(write, write=True)

    async def observations(self, id: str) -> dict:
        """统计真实任务与审批行为，分母未知或无样本时返回空值而非虚构成功率。"""

        def read(connection):
            """聚合整个身份历史，避免只统计第一页而误报任务完成率。"""
            profile = self._profile(connection, id)
            statuses = {
                row["status"]: row["count"]
                for row in query(
                    connection,
                    "SELECT status,COUNT(*) AS count FROM agent_bot_autonomy_task "
                    "WHERE profile_id=%s GROUP BY status",
                    (id,),
                )
            }
            events = {
                row["kind"]: row["count"]
                for row in query(
                    connection,
                    "SELECT kind,COUNT(*) AS count FROM agent_bot_autonomy_audit "
                    "WHERE profile_id=%s GROUP BY kind",
                    (id,),
                )
            }
            finished = statuses.get("succeeded", 0) + statuses.get("failed", 0)
            return {
                "task_statuses": statuses,
                "events": events,
                "completion_rate": statuses.get("succeeded", 0) / finished if finished else None,
                "open_tasks": sum(value for key, value in statuses.items() if key not in TERMINAL),
                "capabilities": profile["document"]["policy"]["allowed_actions"],
                "recent_errors": profile["document"]["state"]["recent_errors"],
            }

        return await self.run(read)

    async def episode(self, id: str, episode_id: str) -> dict:
        """整理与审批只能读取同一身份尚未遗忘的原始情节。"""

        def read(connection):
            """过期即不可再作为新的来源证据，不等待后台墓碑整理。"""
            self._profile(connection, id)
            row = query(
                connection,
                "SELECT * FROM agent_bot_autonomy_episode WHERE id=%s AND profile_id=%s",
                (episode_id, id),
                one=True,
            )
            if row is None or time_value(row["expires_at"]) <= self.now():
                raise AiError("AUTONOMY_SOURCE_EXPIRED", status_code=409)
            document = json.loads(row["document_json"])
            if document["forgotten"]:
                raise AiError("AUTONOMY_SOURCE_EXPIRED", status_code=409)
            return {"id": episode_id, **document}

        return await self.run(read)

    async def episodes(self, id: str, *, after: str = "", limit: int = 50) -> list[dict]:
        """统一记忆视图的情节层只返回有效资料，摘要单独标明为模型摘要。"""

        def read(connection):
            """原情节正文不会因过期整理尚未运行而被再次暴露。"""
            self._profile(connection, id)
            return [
                {"id": row["id"], **json.loads(row["document_json"])}
                for row in query(
                    connection,
                    "SELECT id,document_json FROM agent_bot_autonomy_episode "
                    "WHERE profile_id=%s AND id>%s AND expires_at>%s ORDER BY id LIMIT %s",
                    (id, after, self.now(), limit),
                )
            ]

        return await self.run(read)

    async def save_consolidation(self, claim: dict, summary: str, facts: list[dict]) -> None:
        """摘要与事实提案原子提交，步骤重试使用确定性主键，不重复申请事实写入。"""

        def write(connection):
            """提交前核对步骤令牌及来源存活状态，取消后的模型输出不能写入提案。"""
            id = claim["profile_id"]
            self._profile(connection, id, lock=True)
            task = self._task(connection, id, claim["id"])
            if (
                task["status"] != "running"
                or task["lease_token"] != claim["lease_token"]
                or time_value(task["lease_expires_at"]) <= self.now()
            ):
                raise AiError("AUTONOMY_CONFLICT", status_code=409)
            row = query(
                connection,
                "SELECT * FROM agent_bot_autonomy_episode WHERE id=%s AND profile_id=%s",
                (claim["episode_id"], id),
                one=True,
            )
            if row is None or time_value(row["expires_at"]) <= self.now():
                raise AiError("AUTONOMY_SOURCE_EXPIRED", status_code=409)
            episode = json.loads(row["document_json"])
            if episode["summary"] is not None:
                return
            episode["summary"] = summary
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE agent_bot_autonomy_episode SET document_json=%s WHERE id=%s",
                    (dumps(episode), row["id"]),
                )
                for index, fact in enumerate(facts):
                    proposal_id = hashlib.sha256((claim["id"] + str(index)).encode()).hexdigest()[
                        :32
                    ]
                    cursor.execute(
                        "INSERT INTO agent_bot_autonomy_proposal "
                        "(id,profile_id,episode_id,status,revision,document_json,created_at) "
                        "VALUES (%s,%s,%s,%s,1,%s,%s)",
                        (
                            proposal_id,
                            id,
                            row["id"],
                            "pending",
                            dumps({"fact": fact, "result": None, "approved": False}),
                            self.now(),
                        ),
                    )
            self._audit(
                connection,
                id,
                "memory_consolidated",
                {"episode_id": row["id"], "proposal_count": len(facts)},
                claim["id"],
            )

        await self.run(write, write=True)

    async def proposals(
        self, id: str, *, after: str = "", limit: int = 50, active_only: bool = False
    ) -> list[dict]:
        """待确认的语义结论与事实层分开，普通召回不会把提案当成已确认事实。"""

        def read(connection):
            """审批列表保留结果，当前密钥外的数据不可枚举。"""
            profile = self._profile(connection, id, lock=active_only)
            cursor = profile["document"].get("proposal_cursor", "") if active_only else after
            rows = [
                {**row, "document": json.loads(row["document_json"])}
                for row in query(
                    connection,
                    "SELECT * FROM agent_bot_autonomy_proposal "
                    "WHERE profile_id=%s AND id>%s "
                    + ("AND status IN ('pending','applying') " if active_only else "")
                    + "ORDER BY id LIMIT %s",
                    (id, cursor, limit),
                )
            ]
            if active_only:
                profile["document"]["proposal_cursor"] = (
                    rows[-1]["id"] if len(rows) == limit else ""
                )
                self._save_profile(connection, profile)
            return rows

        return await self.run(read, write=active_only)

    async def decide_proposal(
        self, id: str, proposal_id: str, revision: int, approve: bool
    ) -> None:
        """管理员只审批已看过的提案版本，模型不能自行标记管理员已确认。"""

        def write(connection):
            """批准只允许后续来源校验与写入，不跳过原事实版本及作用域校验。"""
            self._profile(connection, id, lock=True)
            row = query(
                connection,
                "SELECT * FROM agent_bot_autonomy_proposal WHERE id=%s AND profile_id=%s",
                (proposal_id, id),
                one=True,
            )
            if row is None:
                raise AiError("AUTONOMY_NOT_FOUND", status_code=404)
            if row["revision"] != revision or row["status"] not in {"pending", "review"}:
                raise AiError("AUTONOMY_CONFLICT", status_code=409)
            document = json.loads(row["document_json"])
            document["approved"] = approve
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE agent_bot_autonomy_proposal SET document_json=%s,"
                    "status=%s,revision=revision+1 WHERE id=%s",
                    (dumps(document), "pending" if approve else "rejected", proposal_id),
                )
            self._audit(
                connection,
                id,
                "proposal_approved" if approve else "proposal_rejected",
                {"revision": revision},
                proposal_id,
            )

        await self.run(write, write=True)

    async def prepare_proposal(self, id: str, proposal_id: str, fact: dict) -> dict:
        """语义合并审核后先固定实际写入参数，崩溃重试使用相同记忆操作摘要。"""

        def write(connection):
            """只有已授权在途提案可以固定参数；并发准备复用先提交的同一份快照。"""
            self._profile(connection, id, lock=True)
            row = query(
                connection,
                "SELECT status,document_json FROM agent_bot_autonomy_proposal "
                "WHERE id=%s AND profile_id=%s",
                (proposal_id, id),
                one=True,
            )
            if row is None or row["status"] != "applying":
                raise AiError("AUTONOMY_CONFLICT", status_code=409)
            document = json.loads(row["document_json"])
            if not document.get("resolved_fact"):
                document["resolved_fact"] = fact
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE agent_bot_autonomy_proposal SET document_json=%s WHERE id=%s",
                        (dumps(document), proposal_id),
                    )
            return document["resolved_fact"]

        return await self.run(write, write=True)

    async def complete_proposal(self, id: str, proposal_id: str, result: dict) -> None:
        """原事实写入幂等回执后再记录提案结果，崩溃后可重复读取原回执恢复。"""

        def write(connection):
            """拒绝已被人工拒绝的提案提交，不伪造冲突修正已成功。"""
            profile = self._profile(connection, id, lock=True)
            row = query(
                connection,
                "SELECT * FROM agent_bot_autonomy_proposal WHERE id=%s AND profile_id=%s",
                (proposal_id, id),
                one=True,
            )
            if row is None or row["status"] not in {"pending", "applying"}:
                return
            document = json.loads(row["document_json"])
            document["result"] = result
            status = (
                "applied"
                if result["status"]
                in {"saved", "deduplicated", "confirmed", "revised", "unchanged"}
                else "review"
            )
            if (
                status == "applied"
                and result["status"] in {"saved", "deduplicated", "confirmed", "revised"}
                and document["fact"]["kind"] == "task"
                and profile["document"]["policy"]["discover_tasks"]
            ):
                episode = query(
                    connection,
                    "SELECT document_json FROM agent_bot_autonomy_episode WHERE id=%s",
                    (row["episode_id"],),
                    one=True,
                )
                if json.loads(episode["document_json"])["source"] == "self_report":
                    self._create_task(
                        connection,
                        profile,
                        TaskCreate(
                            request_id="memory-task:" + proposal_id,
                            goal=document["fact"]["content"],
                        ),
                    )
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE agent_bot_autonomy_proposal SET document_json=%s,"
                    "status=%s,revision=revision+1 WHERE id=%s",
                    (dumps(document), status, proposal_id),
                )
            self._audit(
                connection,
                id,
                "proposal_" + status,
                {"status": result["status"], "records": result.get("records", [])},
                proposal_id,
            )

        await self.run(write, write=True)

    async def authorize_proposal(self, id: str, proposal_id: str) -> dict | None:
        """提交事实前重新读取策略并固定获准的提案，拒绝期间不能再变更已开始的操作。"""

        def write(connection):
            """自动更正仅在显式开启且为本人来源时允许，其他更正留待管理员核对。"""
            profile = self._profile(connection, id, lock=True)
            policy = Policy.model_validate(profile["document"]["policy"])
            if policy.paused or "consolidate" not in policy.allowed_actions:
                return None
            row = query(
                connection,
                "SELECT * FROM agent_bot_autonomy_proposal WHERE id=%s AND profile_id=%s",
                (proposal_id, id),
                one=True,
            )
            if row is None or row["status"] not in {"pending", "applying"}:
                return None
            document = json.loads(row["document_json"])
            if not document["approved"] and (
                not policy.auto_consolidate
                or (document["fact"].get("correction_id") and not policy.auto_correct)
            ):
                return None
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE agent_bot_autonomy_proposal SET status='applying' WHERE id=%s",
                    (proposal_id,),
                )
            return {**row, "document": document, "owner": profile["document"]["owner"]}

        return await self.run(write, write=True)

    async def publish_report(self, claim: dict, text: str) -> dict:
        """先原子记录宿主频率和出站任务，再由现有持久队列完成实际QQ发送。"""

        def write(connection):
            """相同报告步骤固定任务ID；崩溃重试不会再次计数或产生重复队列结果。"""
            id, task_id = claim["profile_id"], claim["id"]
            profile = self._profile(connection, id, lock=True)
            policy = Policy.model_validate(profile["document"]["policy"])
            task = self._task(connection, id, task_id)
            if (
                task["lease_token"] != claim["lease_token"]
                or task["status"] != "running"
                or time_value(task["lease_expires_at"]) <= self.now()
            ):
                raise AiError("AUTONOMY_CONFLICT", status_code=409)
            report_id = hashlib.sha256((task_id + ":" + str(claim["cursor"])).encode()).hexdigest()[
                :32
            ]
            receipts = task["document"].setdefault("report_receipts", {})
            if str(claim["cursor"]) in receipts:
                return receipts[str(claim["cursor"])]
            previous = query(
                connection, "SELECT id FROM agent_bot_job WHERE id=%s", (report_id,), one=True
            )
            if previous:
                return {"report_id": report_id, "queued": True}
            if policy.paused or "report" not in policy.allowed_actions or quiet(policy, self.now()):
                raise AiError("AUTONOMY_PAUSED", status_code=409)
            usage = self._usage(profile["document"])
            digest = hashlib.sha256(text.encode()).hexdigest()
            if usage.get("last_report_digest") == digest:
                self._audit(connection, id, "report_suppressed", {"reason": "duplicate"}, task_id)
                return {"suppressed": True}
            if usage["reports"] >= policy.max_reports_per_day or (
                usage["last_report_at"]
                and (self.now() - time_value(usage["last_report_at"])).total_seconds()
                < policy.min_report_interval_seconds
            ):
                raise AiError("AUTONOMY_REPORT_LIMIT", status_code=409)
            queued = policy.notify_qq
            if queued:
                if task["document"]["approved_revision"] != task["revision"] or task[
                    "document"
                ].get("approved_policy") != approval_policy(policy):
                    raise AiError("AUTONOMY_APPROVAL_REQUIRED", status_code=409)
                from agent_bot.autonomy.qq_adapter import enqueue_report

                enqueue_report(self, connection, profile, task, report_id, text)
            usage.update(
                reports=usage["reports"] + 1,
                last_report_at=str(self.now()),
                last_report_digest=digest,
            )
            self._save_profile(connection, profile)
            self._audit(
                connection,
                id,
                "report",
                {"text": text, "queued": queued, "report_id": report_id},
                task_id,
            )
            receipt = {"text": text, "report_id": report_id, "queued": queued}
            receipts[str(claim["cursor"])] = receipt
            self._save_task(
                connection, task, token=task["lease_token"], expires=task["lease_expires_at"]
            )
            return receipt

        return await self.run(write, write=True)

    async def delivery_allowed(self, job_id: str) -> bool:
        """每段发送前读取最新宿主暂停、静默和原计划审批，已入队消息也不能绕过。"""

        def read(connection):
            """只匹配本命名空间的自主报告，旧密钥、取消或撤销权限均阻止后续发送。"""
            job = query(
                connection, "SELECT model FROM agent_bot_job WHERE id=%s", (job_id,), one=True
            )
            if not job or not job["model"].startswith("autonomy:"):
                return False
            _, id, task_id = job["model"].split(":")
            profile = self._profile(connection, id)
            task = self._task(connection, id, task_id)
            policy = Policy.model_validate(profile["document"]["policy"])
            return (
                not policy.paused
                and policy.notify_qq
                and "report" in policy.allowed_actions
                and not quiet(policy, self.now())
                and task["status"] != "cancelled"
                and task["document"]["approved_revision"] == task["revision"]
                and task["document"].get("approved_policy") == approval_policy(policy)
            )

        try:
            return await self.run(read)
        except AiError as error:
            if error.code == "AUTONOMY_NOT_FOUND":
                return False
            raise
