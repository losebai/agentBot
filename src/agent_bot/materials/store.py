"""持久素材原件与元数据的事务存储，按真实入口及会话隔离。"""

import asyncio
import base64
import binascii
import hashlib
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import BinaryIO
from uuid import uuid4

from agent_bot.ai.contracts import AiModel, Page
from agent_bot.ai.database import AiDatabase, query
from agent_bot.database_pool import finish_database_write
from agent_bot.materials.contracts import (
    MAX_FILE_BYTES,
    MaterialId,
    MaterialLibraryArguments,
    MaterialLibraryView,
    MaterialSearchArguments,
    MaterialSearchResult,
    MaterialView,
)
from agent_bot.media.config import STORAGE_QUOTA_BYTES
from agent_bot.media.contracts import MediaOwner
from agent_bot.media.errors import MediaError
from agent_bot.media.inputs import ImagePreparer
from agent_bot.media.store import finish_cleanup, utc_now

PUBLIC_COLUMNS = "id,name,kind,mime_type,size_bytes,sha256,description,created_at"
LIBRARY_COLUMNS = PUBLIC_COLUMNS + ",entry,session_id"


class _LibraryCursor(AiModel):
    """游标只携带排序边界及筛选摘要，不携带 SQL 或存储地址。"""

    created_at: datetime
    id: MaterialId
    filters: str


class MaterialStore(AiDatabase):
    """文件在数据库事务内原子发布，取消等待线程结束后再释放目录锁。"""

    def __init__(self, database, *, connect, files):
        """只装配资源；启动检查表结构，不为既有部署隐式升级。"""
        super().__init__(database, connect=connect)
        self.files = files
        self.ready = False
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """确认独立增量已执行，再取得持久目录锁。"""
        await self.run(lambda c: query(c, f"SELECT {PUBLIC_COLUMNS} FROM chat_material LIMIT 0"))
        await self.run(lambda c: query(c, "SELECT source_key,material_id "
                                      "FROM chat_material_source LIMIT 0"))
        await self.run(lambda c: query(c, "SELECT material_ids_json "
                                      "FROM chat_tagged_memory LIMIT 0"))
        await finish_database_write(self.files.start)
        self.ready = True

    def require_ready(self) -> None:
        """缺少迁移时明确返回错误，不降级为只保留易失远程链接。"""
        if not self.ready:
            raise MediaError("MATERIAL_NOT_INITIALIZED", status_code=503)

    async def aclose(self) -> None:
        """持有锁等待读者和写者退出后，才释放进程文件锁。"""
        async with self._lock:
            self.ready = False
            self.files.close()

    async def put(
        self, *, owner: MediaOwner, name: str, mime_type: str, data: bytes,
        description: str = "", source_key: str | None = None,
    ) -> MaterialView:
        """在当前会话内复用相同内容；失败撤销数据库和本次新建文件。"""
        self.require_ready()
        if (not name.strip() or len(name) > 255 or "\0" in name
                or len(description) > 1000 or "\0" in description
                or not re.fullmatch(r"[\w.+-]+/[\w.+-]+", mime_type)
                or not 0 < len(data) <= MAX_FILE_BYTES):
            raise MediaError("MATERIAL_INVALID_FILE", status_code=422)
        if source_key is not None and not re.fullmatch(r"[a-f0-9]{64}", source_key):
            raise ValueError("invalid material source")
        kind = "image" if mime_type.startswith("image/") else "file"
        if kind == "image":
            await finish_database_write(lambda: ImagePreparer().prepare(data, mime_type))
        asset = MaterialView(
            id=uuid4().hex, name=name.strip(), kind=kind, mime_type=mime_type,
            size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
            description=description, created_at=utc_now(),
        )

        def save(connection):
            """内容与来源唯一约束在同一事务中提交，来源不能被替换为别的内容。"""
            published = False
            try:
                old = query(connection, f"SELECT {PUBLIC_COLUMNS} FROM chat_material "
                            "WHERE entry=%s AND session_id=%s AND sha256=%s",
                            (owner.entry, owner.session_id, asset.sha256), one=True)
                result = MaterialView.model_validate(old) if old else asset
                if old is None:
                    used = sum(size for _, size, _ in self.files.entries())
                    if used + len(data) > STORAGE_QUOTA_BYTES:
                        raise MediaError("MEDIA_STORAGE_FULL", status_code=507)
                    values = asset.model_dump()
                    values.update(entry=owner.entry, session_id=owner.session_id,
                                  storage_key=asset.id)
                    query(connection, "INSERT INTO chat_material (" + ",".join(values)
                          + ") VALUES (" + ",".join("%s" for _ in values) + ")",
                          tuple(values.values()))
                    published = True
                    self.files.write(asset.id, data)
                if source_key is not None:
                    source = query(connection, "SELECT material_id FROM chat_material_source "
                                   "WHERE entry=%s AND session_id=%s AND source_key=%s",
                                   (owner.entry, owner.session_id, source_key), one=True)
                    if source is None:
                        query(connection, "INSERT INTO chat_material_source "
                              "(entry,session_id,source_key,material_id) VALUES (%s,%s,%s,%s)",
                              (owner.entry, owner.session_id, source_key, result.id))
                    elif source["material_id"] != result.id:
                        raise MediaError("MATERIAL_SOURCE_CONFLICT", status_code=409)
                return result
            except BaseException:
                if published:
                    self.files.delete(asset.id)
                    self.files.delete(asset.id, temporary=True)
                raise

        async def publish():
            """事务失败清理本次随机文件；已成功提交则保留可再次查询的原件。"""
            try:
                return await self.run(save, write=True)
            except BaseException:
                # 包括事务提交失败；只删除本次随机 ID，不触碰已去重的旧文件。
                await finish_database_write(lambda: self.files.delete(asset.id))
                await finish_database_write(lambda: self.files.delete(asset.id, temporary=True))
                raise

        async with self._lock:
            task = asyncio.create_task(publish())
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError as cancellation:
                async def join():
                    """外层取消不能打断事务，也不能删除已经提交记录所引用的文件。"""
                    return await task

                try:
                    await finish_cleanup(join())
                except Exception as failure:
                    raise cancellation from failure
                raise

    async def find_source(self, owner: MediaOwner, source_key: str) -> MaterialView | None:
        """引用优先读取已登记来源，无需再次下载 QQ 的临时 URL。"""
        self.require_ready()
        row = await self.run(lambda c: query(
            c, "SELECT " + ",".join("m." + name for name in PUBLIC_COLUMNS.split(","))
            + " FROM chat_material m JOIN chat_material_source s ON s.material_id=m.id "
            "WHERE s.entry=%s AND s.session_id=%s AND s.source_key=%s "
            "AND m.entry=%s AND m.session_id=%s",
            (owner.entry, owner.session_id, source_key, owner.entry, owner.session_id), one=True))
        return MaterialView.model_validate(row) if row else None

    async def describe(self, id: str, *, owner: MediaOwner) -> MaterialView:
        """不存在与越权采用同一错误，不允许枚举其他会话素材。"""
        self.require_ready()
        row = await self.run(lambda c: query(
            c, f"SELECT {PUBLIC_COLUMNS} FROM chat_material "
            "WHERE id=%s AND entry=%s AND session_id=%s",
            (id, owner.entry, owner.session_id), one=True))
        if row is None:
            raise MediaError("MATERIAL_NOT_FOUND", status_code=404)
        return MaterialView.model_validate(row)

    async def search(
        self, owner: MediaOwner, arguments: MaterialSearchArguments,
    ) -> MaterialSearchResult:
        """参数化过滤当前会话，按创建时间与 ID 稳定分页。"""
        self.require_ready()
        conditions = ["entry=%s", "session_id=%s"]
        args: list = [owner.entry, owner.session_id]
        if arguments.ids:
            conditions.append("id IN (" + ",".join("%s" for _ in arguments.ids) + ")")
            args.extend(arguments.ids)
        if arguments.kind:
            conditions.append("kind=%s")
            args.append(arguments.kind)
        if arguments.query:
            # 两种后端都使用显式转义符，用户输入的 %/_ 仅按原文匹配。
            pattern = "%" + arguments.query.replace("!", "!!").replace(
                "%", "!%"
            ).replace("_", "!_") + "%"
            conditions.append("(name LIKE %s ESCAPE '!' OR description LIKE %s ESCAPE '!')")
            args.extend((pattern, pattern))
        rows = await self.run(lambda c: query(
            c, f"SELECT {PUBLIC_COLUMNS} FROM chat_material WHERE " + " AND ".join(conditions)
            + " ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s",
            (*args, arguments.limit + 1, arguments.offset)))
        return MaterialSearchResult(
            # 工具正文有 64KiB 上限，保留完整名称和 ID，说明只展示前二百字符。
            records=tuple(MaterialView.model_validate({
                **row, "description": row["description"][:200],
            }) for row in rows[:arguments.limit]),
            next_offset=arguments.offset + arguments.limit if len(rows) > arguments.limit else None,
        )

    @asynccontextmanager
    async def open_content(self, id: str, *, owner: MediaOwner) -> AsyncIterator[BinaryIO]:
        """读者持有同一存储锁，关闭或文件发布不会抢先释放目录。"""
        async with self._lock:
            asset = await self.describe(id, owner=owner)
            async with self._open_file(asset) as file:
                yield file

    async def list_library(
        self, arguments: MaterialLibraryArguments, *, cursor: str | None = None,
    ) -> Page[MaterialLibraryView]:
        """管理员专用跨来源目录，以创建时间和 ID 游标分页且绑定原筛选。"""
        self.require_ready()
        fingerprint = hashlib.sha256(
            arguments.model_dump_json(exclude={"limit"}).encode()
        ).hexdigest()
        boundary = None
        if cursor is not None:
            try:
                boundary = _LibraryCursor.model_validate_json(
                    base64.b64decode(cursor, altchars=b"-_", validate=True)
                )
                if boundary.filters != fingerprint or boundary.created_at.tzinfo is not None:
                    raise ValueError("invalid material cursor")
            except (ValueError, TypeError, UnicodeError, binascii.Error) as error:
                raise MediaError("MATERIAL_INVALID_PAGE", status_code=422) from error

        conditions, args = [], []
        for column in ("kind", "entry", "session_id"):
            value = getattr(arguments, column)
            if value:
                conditions.append(f"{column}=%s")
                args.append(value)
        if arguments.query:
            pattern = "%" + arguments.query.replace("!", "!!").replace(
                "%", "!%"
            ).replace("_", "!_") + "%"
            conditions.append("(name LIKE %s ESCAPE '!' OR description LIKE %s ESCAPE '!')")
            args.extend((pattern, pattern))
        if boundary is not None:
            conditions.append("(created_at<%s OR (created_at=%s AND id<%s))")
            args.extend((boundary.created_at, boundary.created_at, boundary.id))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = await self.run(lambda c: query(
            c, f"SELECT {LIBRARY_COLUMNS} FROM chat_material" + where
            + " ORDER BY created_at DESC,id DESC LIMIT %s", (*args, arguments.limit + 1)))
        items = [MaterialLibraryView.model_validate(row) for row in rows[:arguments.limit]]
        next_cursor = None
        if len(rows) > arguments.limit:
            payload = _LibraryCursor(
                created_at=items[-1].created_at, id=items[-1].id, filters=fingerprint,
            )
            next_cursor = base64.urlsafe_b64encode(payload.model_dump_json().encode()).decode()
        return Page[MaterialLibraryView](items=items, next_cursor=next_cursor)

    async def describe_for_library(self, id: str) -> MaterialLibraryView:
        """仅管理认证路由调用；与会话授权查询分开，避免意外放宽工具权限。"""
        self.require_ready()
        row = await self.run(lambda c: query(
            c, f"SELECT {LIBRARY_COLUMNS} FROM chat_material WHERE id=%s", (id,), one=True))
        if row is None:
            raise MediaError("MATERIAL_NOT_FOUND", status_code=404)
        return MaterialLibraryView.model_validate(row)

    @asynccontextmanager
    async def open_library_content(self, id: str) -> AsyncIterator[BinaryIO]:
        """管理员跨来源读取仍持有文件租约并校验大小与摘要。"""
        async with self._lock:
            asset = await self.describe_for_library(id)
            async with self._open_file(asset) as file:
                yield file

    @asynccontextmanager
    async def _open_file(self, asset: MaterialView) -> AsyncIterator[BinaryIO]:
        """调用方持锁并完成权限检查；取消也等待句柄创建再可靠关闭。"""
        holder: list[BinaryIO] = []

        def open_file() -> None:
            """保留已经创建的句柄，避免线程返回前取消导致资源泄漏。"""
            holder.append(self.files.open(asset.id, size=asset.size_bytes, digest=asset.sha256))

        try:
            await finish_database_write(open_file)
            yield holder[0]
        finally:
            for file in holder:
                file.close()
