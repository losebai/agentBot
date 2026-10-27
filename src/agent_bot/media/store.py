"""媒体元数据状态机、文件租约、配额和保守恢复。"""

import asyncio
import hashlib
from collections import Counter
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from agent_bot.ai.database import AiDatabase, query
from agent_bot.database_pool import finish_database_write
from agent_bot.media.config import ORPHAN_RETENTION_SECONDS, RETENTION_SECONDS, STORAGE_QUOTA_BYTES
from agent_bot.media.contracts import MediaAssetRecord, MediaAssetView, MediaOwner, MediaScope
from agent_bot.media.errors import MediaError
from agent_bot.media.files import KEY, MediaFiles
from agent_bot.media.inputs import ImagePreparer


def utc_now():
    """数据库统一使用无时区的协调世界时。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def decode_asset(row):
    """将明确的数据库列还原为内部归属对象。"""
    values = dict(row)
    owner = MediaOwner(entry=values.pop("entry"), session_id=values.pop("session_id"))
    return MediaAssetRecord.model_validate({**values, "owner": owner})


async def finish_cleanup(operation):
    """取消路径等待清理结束；重复取消也不能提前释放锁或文件句柄。"""
    task = asyncio.create_task(operation)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


class MediaStore(AiDatabase):
    """所有文件变化按进程串行，读取期间通过租约阻止清理。"""

    def __init__(
        self,
        database,
        *,
        connect,
        files: MediaFiles,
        clock=utc_now,
        quota=STORAGE_QUOTA_BYTES,
        retention=RETENTION_SECONDS,
    ):
        """注入目录和时钟，运行时刷新共享本对象。"""
        super().__init__(database, connect=connect)
        self.files, self.clock, self.quota, self.retention = files, clock, quota, retention
        self._lock = asyncio.Lock()
        self._leases = Counter()
        self._idle = asyncio.Event()
        self._idle.set()

    def _lease(self, ids, delta):
        """统一租约计数，关闭时等待所有已打开文件和执行输入归还。"""
        for id in ids:
            self._leases[id] += delta
            if self._leases[id] == 0:
                del self._leases[id]
        if self._leases:
            self._idle.clear()
        else:
            self._idle.set()

    async def aclose(self):
        """等读者和图像执行释放租约，再归还进程目录锁。"""
        await self._idle.wait()
        async with self._lock:
            self.files.close()

    async def _row(self, id):
        """无效标识与不存在的条目采用相同错误。"""
        if not isinstance(id, str) or not KEY.fullmatch(id):
            raise MediaError("MEDIA_NOT_FOUND", status_code=404)
        row = await self.run(
            lambda c: query(c, "SELECT * FROM agent_bot_media_asset WHERE id=%s", (id,), one=True)
        )
        if row is None:
            raise MediaError("MEDIA_NOT_FOUND", status_code=404)
        return decode_asset(row)

    def _authorize(self, record, owner, admin_only, *, require_ready=True):
        """权限先于过期检查，禁止利用 410 探测其他入口和会话的附件。"""
        if (
            (owner is None) == (not admin_only)
            or (admin_only and record.owner.entry != "admin")
            or (owner is not None and record.owner != owner)
        ):
            raise MediaError("MEDIA_NOT_FOUND", status_code=404)
        if require_ready:
            if record.expires_at <= self.clock() or record.status == "expired":
                raise MediaError("MEDIA_EXPIRED", status_code=410)
            if record.status != "ready":
                raise MediaError("MEDIA_NOT_FOUND", status_code=404)

    async def _status(self, id, status):
        """单独提交生命周期状态，失败时保留文件以便启动对账。"""
        await self.run(
            lambda c: query(
                c, "UPDATE agent_bot_media_asset SET status=%s WHERE id=%s", (status, id)
            ),
            write=True,
        )

    async def put(
        self,
        *,
        owner: MediaOwner,
        purpose: str,
        data: bytes,
        mime_type: str,
        scope: MediaScope | None = None,
    ) -> MediaAssetView:
        """先记录 writing，再原子发布文件，校验完成后才能返回 ready 元数据。"""
        if scope is not None and scope.owner != owner:
            raise MediaError("MEDIA_NOT_FOUND", status_code=404)
        if mime_type == "video/mp4":
            from agent_bot.media.video import validate_mp4

            # 投稿上传与工作流产物均可为 MP4，身份由统一 owner 边界验证。
            try:
                await finish_database_write(lambda: validate_mp4(data))
            except ValueError as error:
                if purpose == 'input':
                    raise MediaError('BILIBILI_INVALID_MEDIA', status_code=422) from error
                raise MediaError("MEDIA_INVALID_RESPONSE") from error
        id = uuid4().hex
        now = self.clock()
        record = MediaAssetRecord(
            id=id,
            storage_key=id,
            owner=owner,
            purpose=purpose,
            mime_type=mime_type,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            status="writing",
            created_at=now,
            expires_at=now + timedelta(seconds=self.retention),
            execution_id=scope.execution_id if scope else None,
            execution_kind=scope.execution_kind if scope else None,
        )
        values = record.model_dump(exclude={"owner"})
        values.update(owner.model_dump())
        async with self._lock:
            used = sum(size for _, size, _ in await finish_database_write(self.files.entries))
            if used + len(data) > self.quota:
                raise MediaError("MEDIA_STORAGE_FULL", status_code=507)
            try:
                await self.run(
                    lambda c: query(
                        c,
                        "INSERT INTO agent_bot_media_asset ("
                        + ",".join(values)
                        + ") VALUES ("
                        + ",".join("%s" for _ in values)
                        + ")",
                        tuple(values.values()),
                    ),
                    write=True,
                )
                await finish_database_write(lambda: self.files.write(id, data))
                await finish_database_write(
                    lambda: self.files.open(
                        id, size=record.size_bytes, digest=record.sha256
                    ).close()
                )
                await self._status(id, "ready")
                return record.model_copy(update={"status": "ready"}).public()
            except BaseException as error:
                # 确认失败状态之后才能删除文件；数据库不可用时保留真实磁盘占用。
                with suppress(Exception):
                    await finish_cleanup(self._fail_write(id))
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise MediaError("MEDIA_SAVE_FAILED") from error

    async def _fail_write(self, id):
        """失败或取消后撤销可见性，清理已经落盘的两种文件名。"""
        await self._status(id, "failed")
        await finish_database_write(lambda: self.files.delete(id))
        await finish_database_write(lambda: self.files.delete(id, temporary=True))

    async def describe(self, id, *, owner=None, admin_only=False):
        """返回调用方有权访问的公开元数据。"""
        async with self._lock:
            record = await self._row(id)
            self._authorize(record, owner, admin_only)
            return record.public()

    @asynccontextmanager
    async def open_content(self, id, *, owner=None, admin_only=False):
        """打开与完整性校验在一个受保护的工作线程中完成。"""
        holder = []

        def open_file():
            """保存句柄到容器，即使等待者被取消也能准确关闭。"""
            holder.append(self.files.open(id, size=record.size_bytes, digest=record.sha256))

        async with self._lock:
            record = await self._row(id)
            self._authorize(record, owner, admin_only)
            try:
                await finish_database_write(open_file)
            except BaseException:
                for file in holder:
                    file.close()
                raise
            self._lease((id,), 1)
        try:
            yield holder[0]
        finally:
            holder[0].close()
            self._lease((id,), -1)

    @asynccontextmanager
    async def bind_inputs(self, ids: tuple[str, ...], scope: MediaScope):
        """整批授权、校验及绑定成功后才建立执行租约。"""
        if len(ids) > 8 or len(set(ids)) != len(ids):
            raise MediaError("MEDIA_TOO_MANY_IMAGES", status_code=422)
        images, records = [], []
        async with self._lock:
            for id in ids:
                record = await self._row(id)
                self._authorize(record, scope.owner, False)
                if record.purpose != "input":
                    raise MediaError("MEDIA_NOT_FOUND", status_code=404)
                if record.execution_id is not None and (
                    record.execution_id != scope.execution_id
                    or record.execution_kind != scope.execution_kind
                ):
                    raise MediaError("MEDIA_INPUT_ALREADY_USED", status_code=409)
                records.append(record)
            if sum(record.size_bytes for record in records) > 20 * 1024 * 1024:
                raise MediaError("MEDIA_TOO_LARGE", status_code=413)
            for record in records:
                images.append(
                    await finish_database_write(lambda record=record: self._prepare_file(record))
                )
            if ids:

                def bind(connection):
                    """所有输入一次提交，非法后项不会消费前面的上传。"""
                    for id in ids:
                        query(
                            connection,
                            "UPDATE agent_bot_media_asset SET execution_id=%s,execution_kind=%s "
                            "WHERE id=%s",
                            (scope.execution_id, scope.execution_kind, id),
                        )

                await self.run(bind, write=True)
            self._lease(ids, 1)
        try:
            yield tuple(images)
        finally:
            self._lease(ids, -1)

    def _prepare_file(self, record):
        """用校验过的同一文件句柄读取，避免准备期间被替换为任意输入。"""
        with self.files.open(record.id, size=record.size_bytes, digest=record.sha256) as file:
            return ImagePreparer().prepare(file.read(), record.mime_type)

    async def list_execution(self, scope: MediaScope):
        """已发布及过期附件进入历史，失败和写入中的条目不公开。"""
        async with self._lock:
            rows = await self.run(
                lambda c: query(
                    c,
                    "SELECT * FROM agent_bot_media_asset WHERE "
                    "entry=%s AND session_id=%s AND execution_kind=%s AND execution_id=%s "
                    "AND status IN ('ready','expired') ORDER BY created_at,id",
                    (
                        scope.owner.entry,
                        scope.owner.session_id,
                        scope.execution_kind,
                        scope.execution_id,
                    ),
                )
            )
            return tuple(decode_asset(row).public() for row in rows)

    async def cleanup(self, now: datetime):
        """到期先标记状态，活动读取结束后才能删除物理文件。"""
        async with self._lock:
            rows = await self.run(
                lambda c: query(
                    c,
                    "SELECT * FROM agent_bot_media_asset "
                    "WHERE expires_at<=%s OR status IN ('expired','failed')",
                    (now,),
                )
            )
            for row in rows:
                record = decode_asset(row)
                if record.status != "failed":
                    await self._status(record.id, "expired")
                if not self._leases[record.id]:
                    await finish_database_write(lambda record=record: self.files.delete(record.id))
                    await finish_database_write(
                        lambda record=record: self.files.delete(record.id, temporary=True)
                    )

    async def reconcile(self, now: datetime):
        """独占启动对账；中断写入不自动宣称成功，未归属文件保留一小时。"""
        async with self._lock:
            rows = await self.run(lambda c: query(c, "SELECT * FROM agent_bot_media_asset"))
            known = {row["id"] for row in rows}
            for row in rows:
                record = decode_asset(row)
                if self._leases[record.id]:
                    continue
                if record.status == "writing":
                    await self._fail_write(record.id)
                elif record.status == "ready":
                    try:
                        await finish_database_write(
                            lambda record=record: self.files.open(
                                record.id, size=record.size_bytes, digest=record.sha256
                            ).close()
                        )
                    except (MediaError, OSError):
                        await self._status(record.id, "failed")
            cutoff = now.replace(tzinfo=timezone.utc).timestamp() - ORPHAN_RETENTION_SECONDS
            for name, _, stamp in await finish_database_write(self.files.entries):
                key = name[:32]
                if key not in known and stamp <= cutoff:
                    await finish_database_write(
                        lambda key=key, name=name: self.files.delete(
                            key, temporary=name.endswith(".part")
                        )
                    )
        await self.cleanup(now)
