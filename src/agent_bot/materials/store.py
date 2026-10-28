"""持久素材原件与元数据的事务存储，按真实入口及会话隔离。"""

import asyncio
import base64
import binascii
import hashlib
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from io import BytesIO
from typing import Any, BinaryIO
from uuid import uuid4

from PIL import Image, ImageOps, UnidentifiedImageError

from agent_bot.ai.contracts import AiModel, Page
from agent_bot.ai.database import AiDatabase, query
from agent_bot.database_pool import finish_database_write
from agent_bot.materials.contracts import (
    MAX_FILE_BYTES,
    MaterialCategory,
    MaterialId,
    MaterialLibraryArguments,
    MaterialLibraryView,
    MaterialSearchArguments,
    MaterialSearchResult,
    MaterialView,
    normalize_material_tags,
)
from agent_bot.media.contracts import MediaOwner
from agent_bot.media.errors import MediaError
from agent_bot.media.inputs import ImagePreparer
from agent_bot.media.store import finish_cleanup, utc_now

PUBLIC_COLUMNS = "id,name,kind,mime_type,size_bytes,sha256,description,created_at"
LIBRARY_COLUMNS = PUBLIC_COLUMNS + ",entry,session_id"
CATEGORY_TAGS: dict[MaterialCategory, str] = {
    "received": "触发接收", "sent": "LLM发送", "generated": "生成图片", "uploaded": "手动上传",
}


def _metadata_filters(
    arguments: MaterialLibraryArguments | MaterialSearchArguments,
) -> tuple[list[str], list[str]]:
    """关联表通过 EXISTS 筛选，避免多类别和标签把素材行及计数重复展开。"""
    conditions, args = [], []
    for attribute, table, column in (
        ("category", "chat_material_category", "category"), ("tag", "chat_material_tag", "tag"),
    ):
        value = getattr(arguments, attribute)
        if value:
            conditions.append(
                f"EXISTS (SELECT 1 FROM {table} WHERE material_id=chat_material.id AND {column}=%s)"
            )
            args.append(value)
    return conditions, args


def _with_metadata(
    connection: Any, rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """在同一事务中批量装配元数据，每页最多两次关联读取且保持原行顺序。"""
    if not rows:
        return []
    ids = tuple(row["id"] for row in rows)
    metadata: dict[str, dict[str, list[str]]] = {
        id: {"categories": [], "tags": []} for id in ids
    }
    placeholders = ",".join("%s" for _ in ids)
    for table, column, attribute in (
        ("chat_material_category", "category", "categories"), ("chat_material_tag", "tag", "tags"),
    ):
        for row in query(connection, f"SELECT material_id,{column} FROM {table} "
                         f"WHERE material_id IN ({placeholders}) ORDER BY {column}", ids):
            metadata[row["material_id"]][attribute].append(row[column])
    return [{**row, **metadata[row["id"]]} for row in rows]


class _LibraryCursor(AiModel):
    """游标只携带排序边界及筛选摘要，不携带 SQL 或存储地址。"""

    created_at: datetime
    id: MaterialId
    filters: str


def _library_filters(arguments: MaterialLibraryArguments) -> tuple[list[str], list[str]]:
    """列表和独立计数共用筛选及转义，通配符只按用户输入的原文匹配。"""
    conditions, args = _metadata_filters(arguments)
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
    return conditions, args


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
        await self.run(lambda c: query(c, "SELECT material_id,category "
                                      "FROM chat_material_category LIMIT 0"))
        await self.run(lambda c: query(c, "SELECT material_id,tag FROM chat_material_tag LIMIT 0"))
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
        category: MaterialCategory | None = None, tags: tuple[str, ...] = (),
    ) -> MaterialView:
        """持久素材不设总配额；按会话去重，失败撤销数据库和本次新建文件。"""
        self.require_ready()
        try:
            tags = normalize_material_tags(tags)
            if category is not None and category not in CATEGORY_TAGS:
                raise ValueError("invalid material category")
        except ValueError as error:
            raise MediaError("MATERIAL_INVALID_TAGS", status_code=422) from error
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
                    values = asset.model_dump(exclude={"categories", "tags"})
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
                metadata = _with_metadata(connection, [{"id": result.id}])[0]
                new_tags = tags
                if category is not None and category not in metadata["categories"]:
                    query(connection, "INSERT INTO chat_material_category (material_id,category) "
                          "VALUES (%s,%s)", (result.id, category))
                    # 来源标签只在首次建档加入，后续流转不能覆盖或挤占管理员标签。
                    if old is None:
                        new_tags += normalize_material_tags((CATEGORY_TAGS[category],))
                try:
                    merged_tags = normalize_material_tags(tuple(metadata["tags"]) + new_tags)
                except ValueError as error:
                    raise MediaError("MATERIAL_INVALID_TAGS", status_code=422) from error
                for tag in merged_tags:
                    if tag not in metadata["tags"]:
                        query(connection, "INSERT INTO chat_material_tag (material_id,tag) "
                              "VALUES (%s,%s)", (result.id, tag))
                return MaterialView.model_validate(_with_metadata(
                    connection, [result.model_dump(exclude={"categories", "tags"})],
                )[0])
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
        rows = await self.run(lambda c: _with_metadata(c, query(
            c, "SELECT " + ",".join("m." + name for name in PUBLIC_COLUMNS.split(","))
            + " FROM chat_material m JOIN chat_material_source s ON s.material_id=m.id "
            "WHERE s.entry=%s AND s.session_id=%s AND s.source_key=%s "
            "AND m.entry=%s AND m.session_id=%s",
            (owner.entry, owner.session_id, source_key, owner.entry, owner.session_id))))
        return MaterialView.model_validate(rows[0]) if rows else None

    async def describe(self, id: str, *, owner: MediaOwner) -> MaterialView:
        """不存在与越权采用同一错误，不允许枚举其他会话素材。"""
        self.require_ready()
        rows = await self.run(lambda c: _with_metadata(c, query(
            c, f"SELECT {PUBLIC_COLUMNS} FROM chat_material "
            "WHERE id=%s AND entry=%s AND session_id=%s",
            (id, owner.entry, owner.session_id))))
        if not rows:
            raise MediaError("MATERIAL_NOT_FOUND", status_code=404)
        return MaterialView.model_validate(rows[0])

    async def add_category(
        self, id: str, category: MaterialCategory, *, owner: MediaOwner,
    ) -> MaterialView:
        """已验证实际接收或发送后登记来源；只追加分类，不读取原件或修改标签。"""
        self.require_ready()
        if category not in CATEGORY_TAGS:
            raise MediaError("MATERIAL_INVALID_CATEGORY", status_code=422)

        def append(connection: Any) -> MaterialView:
            """同一事务先验证真实归属，再幂等追加来源并返回完整公开元数据。"""
            row = query(connection, f"SELECT {PUBLIC_COLUMNS} FROM chat_material "
                        "WHERE id=%s AND entry=%s AND session_id=%s",
                        (id, owner.entry, owner.session_id), one=True)
            if row is None:
                raise MediaError("MATERIAL_NOT_FOUND", status_code=404)
            existing = query(connection, "SELECT category FROM chat_material_category "
                             "WHERE material_id=%s AND category=%s", (id, category), one=True)
            if existing is None:
                query(connection, "INSERT INTO chat_material_category (material_id,category) "
                      "VALUES (%s,%s)", (id, category))
            return MaterialView.model_validate(_with_metadata(connection, [row])[0])

        async with self._lock:
            return await self.run(append, write=True)

    async def search(
        self, owner: MediaOwner, arguments: MaterialSearchArguments,
    ) -> MaterialSearchResult:
        """参数化过滤当前会话，按创建时间与 ID 稳定分页。"""
        self.require_ready()
        conditions, args = _metadata_filters(arguments)
        conditions.extend(("entry=%s", "session_id=%s"))
        args.extend((owner.entry, owner.session_id))
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
        rows = await self.run(lambda c: _with_metadata(c, query(
            c, f"SELECT {PUBLIC_COLUMNS} FROM chat_material WHERE " + " AND ".join(conditions)
            + " ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s",
            (*args, arguments.limit + 1, arguments.offset))))
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
        page: int | None = None,
    ) -> Page[MaterialLibraryView]:
        """管理员跨来源目录支持页码；旧游标仍绑定原筛选，不能混用两种分页。"""
        self.require_ready()
        if page is not None and (page < 1 or cursor is not None):
            raise MediaError("MATERIAL_INVALID_PAGE", status_code=422)
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

        conditions, args = _library_filters(arguments)
        if boundary is not None:
            conditions.append("(created_at<%s OR (created_at=%s AND id<%s))")
            args.extend((boundary.created_at, boundary.created_at, boundary.id))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = await self.run(lambda c: _with_metadata(c, query(
            c, f"SELECT {LIBRARY_COLUMNS} FROM chat_material" + where
            + " ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s",
            (*args, arguments.limit + 1, (page - 1) * arguments.limit if page is not None else 0))))
        items = [MaterialLibraryView.model_validate(row) for row in rows[:arguments.limit]]
        next_cursor = None
        if len(rows) > arguments.limit:
            payload = _LibraryCursor(
                created_at=items[-1].created_at, id=items[-1].id, filters=fingerprint,
            )
            next_cursor = base64.urlsafe_b64encode(payload.model_dump_json().encode()).decode()
        return Page[MaterialLibraryView](items=items, next_cursor=next_cursor)

    async def count_library(self, arguments: MaterialLibraryArguments) -> int:
        """独立计算当前筛选总量，不向每次分页查询附加全表计数。"""
        self.require_ready()
        conditions, args = _library_filters(arguments)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        row = await self.run(lambda c: query(
            c, "SELECT COUNT(*) AS total FROM chat_material" + where, tuple(args), one=True))
        return row["total"]

    async def describe_for_library(self, id: str) -> MaterialLibraryView:
        """仅管理认证路由调用；与会话授权查询分开，避免意外放宽工具权限。"""
        self.require_ready()
        rows = await self.run(lambda c: _with_metadata(c, query(
            c, f"SELECT {LIBRARY_COLUMNS} FROM chat_material WHERE id=%s", (id,))))
        if not rows:
            raise MediaError("MATERIAL_NOT_FOUND", status_code=404)
        return MaterialLibraryView.model_validate(rows[0])

    async def update_tags(self, id: str, tags: tuple[str, ...]) -> MaterialLibraryView:
        """仅认证管理路由可替换完整标签组，事务失败保留旧组且不改变分类。"""
        self.require_ready()
        try:
            normalized = normalize_material_tags(tags)
        except ValueError as error:
            raise MediaError("MATERIAL_INVALID_TAGS", status_code=422) from error

        def replace(connection):
            """先确认素材存在，再在同一写事务内替换标签并读取最终公开状态。"""
            row = query(connection, f"SELECT {LIBRARY_COLUMNS} FROM chat_material WHERE id=%s",
                        (id,), one=True)
            if row is None:
                raise MediaError("MATERIAL_NOT_FOUND", status_code=404)
            query(connection, "DELETE FROM chat_material_tag WHERE material_id=%s", (id,))
            for tag in normalized:
                query(connection, "INSERT INTO chat_material_tag (material_id,tag) VALUES (%s,%s)",
                      (id, tag))
            return MaterialLibraryView.model_validate(_with_metadata(connection, [row])[0])

        async with self._lock:
            return await self.run(replace, write=True)

    @asynccontextmanager
    async def open_library_content(self, id: str) -> AsyncIterator[BinaryIO]:
        """管理员跨来源读取仍持有文件租约并校验大小与摘要。"""
        async with self._lock:
            asset = await self.describe_for_library(id)
            async with self._open_file(asset) as file:
                yield file

    async def library_thumbnail(self, id: str) -> bytes:
        """认证路由专用缩略图，持锁及完整性租约直到取消后的编码线程退出。"""
        async with self._lock:
            asset = await self.describe_for_library(id)
            if asset.kind != "image":
                raise MediaError("MATERIAL_INVALID_FILE", status_code=422)
            async with self._open_file(asset) as file:
                def encode() -> bytes:
                    """在工作线程校正方向并生成无元数据 PNG，不修改或落盘原件。"""
                    try:
                        with (
                            Image.open(file) as source,
                            ImageOps.exif_transpose(source) as oriented,
                        ):
                            oriented.thumbnail((160, 160), Image.Resampling.LANCZOS)
                            # 调色板与 tRNS 透明信息不一定包含 A 通道，编码前转为真实透明度。
                            has_alpha = (
                                "A" in oriented.getbands() or "transparency" in oriented.info
                            )
                            with oriented.convert("RGBA" if has_alpha else "RGB") as normalized:
                                normalized.info.clear()
                                output = BytesIO()
                                normalized.save(output, format="PNG")
                                return output.getvalue()
                    except (OSError, ValueError, UnidentifiedImageError,
                            Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
                        raise MediaError("MEDIA_FILE_CORRUPT") from error

                return await finish_database_write(encode)

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
