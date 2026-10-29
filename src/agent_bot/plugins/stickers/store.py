"""表情包元数据、受控文件与修订的一致性边界。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from agent_bot.ai.database import AiDatabase, query
from agent_bot.ai.errors import AiError
from agent_bot.database_pool import finish_database_write
from agent_bot.media.errors import MediaError
from agent_bot.media.files import KEY, MediaFiles
from agent_bot.plugins.stickers.contracts import StickerPatch, StickerView, normalize_tags
from agent_bot.plugins.stickers.images import prepare_sticker_image
from agent_bot.settings import DatabaseParams


async def _finish(operation: Awaitable[Any]) -> Any:
    """完整文件事务结束前不交出锁；取消仍最终反馈给等待者。"""
    task = asyncio.create_task(operation)
    cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancellation = error
        except Exception:
            if cancellation is None:
                raise
    if cancellation is not None:
        if not task.cancelled():
            task.exception()
        raise cancellation
    return task.result()


def _view(row: dict[str, Any]) -> StickerView:
    """从私有记录投影公开字段，文件键与墓碑不会进入 API。"""
    values = {key: row[key] for key in StickerView.model_fields if key != 'tags'}
    values['tags'] = json.loads(row['tags_json'])
    return StickerView.model_validate(values)


class StickerStore(AiDatabase):
    """单进程串行文件变化，数据库行锁保护跨进程的乐观修订。"""

    def __init__(self, database: DatabaseParams, *, connect: Callable[..., Any], files: MediaFiles):
        """注入已有数据库所有者和独立 stickers 目录，构造不访问资源。"""
        super().__init__(database, connect=connect)
        self.files = files
        self._lock = asyncio.Lock()
        self._started = False

    async def start(self) -> None:
        """验证迁移已完成后独占图库；清理墓碑和中断写入遗留的私有文件。"""
        async with self._lock:
            if self._started:
                return
            try:
                await finish_database_write(self.files.start)
                columns = [field for field in StickerView.model_fields if field != 'tags']
                columns.extend(('tags_json', 'storage_key', 'preview_key', 'preview_size_bytes',
                                'preview_sha256', 'active_sha256', 'deleted'))
                rows = await self.run(lambda c: query(
                    c, 'SELECT ' + ','.join(columns) + ' FROM agent_bot_sticker'))
                known = {row[key] for row in rows if not row['deleted']
                         for key in ('storage_key', 'preview_key')}
                for name, _, _ in await finish_database_write(self.files.entries):
                    if name.endswith('.part') or name not in known:
                        await finish_database_write(
                            lambda name=name: self.files.delete(name[:32],
                                                               temporary=name.endswith('.part')))
                self._started = True
            except BaseException:
                self.files.close()
                raise

    async def aclose(self) -> None:
        """等待持锁操作和工作线程结束，再释放目录锁。"""
        async with self._lock:
            self._started = False
            self.files.close()

    def _require_started(self) -> None:
        """关闭后拒绝新操作，避免释放锁后仍有文件访问。"""
        if not self._started:
            raise MediaError('STICKER_STORE_UNAVAILABLE', status_code=503)

    async def _row(self, id: str) -> dict[str, Any]:
        """无效 ID 与已删除行都采用不可探测的不存在响应。"""
        self._require_started()
        if not isinstance(id, str) or not KEY.fullmatch(id):
            raise MediaError('STICKER_NOT_FOUND', status_code=404)
        row = await self.run(lambda c: query(c, 'SELECT * FROM agent_bot_sticker '
                                            'WHERE id=%s AND deleted=0', (id,), one=True))
        if row is None:
            raise MediaError('STICKER_NOT_FOUND', status_code=404)
        return row

    async def create(self, name: str, data: bytes, mime_type: str | None,
                     tags: tuple[str, ...] | list[str] = ()) -> StickerView:
        """先保存两份文件再发布元数据；失败删除本次私有文件，不留下活跃悬空引用。"""
        async with self._lock:
            self._require_started()
            return await _finish(self._create(name, data, mime_type, tags))

    async def _create(self, name: str, data: bytes, mime_type: str | None,
                      tags: tuple[str, ...] | list[str]) -> StickerView:
        """持有目录锁时执行有界解码和原子发布。"""
        try:
            tags = normalize_tags(tags)
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 255:
                raise ValueError('名称无效')
        except ValueError as error:
            raise MediaError('STICKER_INVALID_METADATA', status_code=422) from error
        prepared = await finish_database_write(lambda: prepare_sticker_image(data, mime_type))
        id, preview_key = uuid4().hex, uuid4().hex
        now = datetime.now(timezone.utc)
        values = dict(id=id, name=name.strip(), tags_json=json.dumps(tags, ensure_ascii=False),
                      description='', emotion='', usage_hint='', enabled=True, revision=1,
                      mime_type=prepared.mime_type, size_bytes=len(data),
                      sha256=hashlib.sha256(data).hexdigest(),
                      active_sha256=hashlib.sha256(data).hexdigest(), width=prepared.width,
                      height=prepared.height, frame_count=prepared.frame_count,
                      animated=prepared.animated, analysis_status='pending', analysis_error=None,
                      storage_key=id, preview_key=preview_key,
                      preview_size_bytes=len(prepared.preview),
                      preview_sha256=hashlib.sha256(prepared.preview).hexdigest(),
                      deleted=False, created_at=now, updated_at=now)
        try:
            await finish_database_write(lambda: self.files.write(id, data))
            await finish_database_write(lambda: self.files.write(preview_key, prepared.preview))
            await self.run(lambda c: query(
                c, 'INSERT INTO agent_bot_sticker (' + ','.join(values) + ') VALUES ('
                + ','.join('%s' for _ in values) + ')', tuple(values.values())), write=True)
        except BaseException as error:
            for key in (id, preview_key):
                for temporary in (False, True):
                    with suppress(Exception):
                        await finish_database_write(
                            lambda key=key, temporary=temporary:
                            self.files.delete(key, temporary=temporary))
            if isinstance(error, AiError) and error.code == 'AI_CONFIG_CONFLICT':
                raise MediaError('STICKER_DUPLICATE', status_code=409) from error
            if isinstance(error, asyncio.CancelledError):
                raise
            raise MediaError('STICKER_SAVE_FAILED') from error
        return _view(values)

    async def get(self, id: str) -> StickerView:
        """返回启用或停用的管理元数据；投递方须另行检查 enabled。"""
        async with self._lock:
            return _view(await self._row(id))

    async def list(self, query: str = '', tag: str = '', offset: int = 0, limit: int = 20,
                   enabled: bool | None = None) -> dict[str, Any]:
        """完整元数据筛选后分页，Unicode 标签精确匹配且不静默截断候选。"""
        if (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0
                or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100
                or not isinstance(query, str) or len(query) > 1000
                or not isinstance(tag, str) or len(tag) > 32
                or (enabled is not None and not isinstance(enabled, bool))):
            raise MediaError('STICKER_INVALID_PAGE', status_code=422)
        async with self._lock:
            self._require_started()
            # 参数 query 与公共接口保持一致，SQL 辅助函数在局部别名避免遮蔽。
            from agent_bot.ai.database import query as sql_query

            if not query.strip() and not tag.strip():
                def page(connection: Any) -> dict[str, Any]:
                    """候选库按同一读事务分页，避免每个匹配批次重复载入整库。"""
                    where = 'deleted=0' + (' AND enabled=%s' if enabled is not None else '')
                    args = (enabled,) if enabled is not None else ()
                    total = sql_query(connection, 'SELECT COUNT(*) AS total '
                                      'FROM agent_bot_sticker WHERE ' + where,
                                      args, one=True)['total']
                    rows = sql_query(connection, 'SELECT * FROM agent_bot_sticker WHERE '
                                     + where + ' ORDER BY created_at DESC,id LIMIT %s OFFSET %s',
                                     (*args, limit, offset))
                    return {'items': [_view(row) for row in rows], 'total': total,
                            'next_offset': offset + limit if offset + limit < total else None}
                return await self.run(page)
            rows = await self.run(lambda c: sql_query(
                c, 'SELECT * FROM agent_bot_sticker WHERE deleted=0 ORDER BY created_at DESC,id'))
            items = [_view(row) for row in rows]
            needle = query.strip().casefold()
            items = [item for item in items
                     if (enabled is None or item.enabled == enabled)
                     and (not tag or tag.strip() in item.tags)
                     and (not needle or needle in ' '.join(
                         (item.name, item.description, item.emotion, item.usage_hint,
                          *item.tags)).casefold())]
            total = len(items)
            return {'items': items[offset:offset + limit], 'total': total,
                    'next_offset': offset + limit if offset + limit < total else None}

    async def candidates(self, *, after: str | None = None,
                         limit: int = 40) -> list[StickerView]:
        """检索按不可变ID前进，前页停用或删除不会移动后续候选的读取边界。"""
        if (after is not None and (not isinstance(after, str) or not KEY.fullmatch(after))) or (
            isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100
        ):
            raise MediaError('STICKER_INVALID_PAGE', status_code=422)
        async with self._lock:
            self._require_started()
            condition = 'deleted=0 AND enabled=1' + (' AND id>%s' if after is not None else '')
            args = (after, limit) if after is not None else (limit,)
            rows = await self.run(lambda c: query(
                c, 'SELECT * FROM agent_bot_sticker WHERE ' + condition
                + ' ORDER BY id LIMIT %s', args))
            return [_view(row) for row in rows]

    async def update(self, id: str, patch: StickerPatch) -> StickerView:
        """乐观修订只提交编辑字段；识图结果与标签编辑共用同一冲突边界。"""
        values = patch.model_dump(exclude_unset=True, exclude={'expected_revision'})
        if 'tags' in values:
            values['tags_json'] = json.dumps(values.pop('tags'), ensure_ascii=False)
        if 'name' in values:
            values['name'] = values['name'].strip()
        async with self._lock:
            await self._row(id)
            return await self._change(id, patch.expected_revision, values)

    async def _change(self, id: str, revision: int, values: dict[str, Any]) -> StickerView:
        """读取校验与写入同事务持有行锁，避免多个实例同时接受同一修订。"""
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise MediaError('STICKER_INVALID_METADATA', status_code=422)

        def write(connection: Any) -> StickerView:
            """MySQL 使用行锁，SQLite 的写事务由适配器串行化。"""
            suffix = ' FOR UPDATE' if self.params.backend == 'mysql' else ''
            row = query(connection, 'SELECT * FROM agent_bot_sticker '
                        'WHERE id=%s AND deleted=0' + suffix, (id,), one=True)
            if row is None:
                raise MediaError('STICKER_NOT_FOUND', status_code=404)
            if row['revision'] != revision:
                raise MediaError('STICKER_CONFLICT', status_code=409)
            updated = dict(values, revision=revision + 1, updated_at=datetime.now(timezone.utc))
            query(connection, 'UPDATE agent_bot_sticker SET '
                  + ','.join(f'{name}=%s' for name in updated) + ' WHERE id=%s',
                  (*updated.values(), id))
            return _view(dict(row, **updated))
        return await self.run(write, write=True)

    async def set_analysis(self, id: str, *, expected_revision: int, description: str = '',
                           emotion: str = '', usage_hint: str = '',
                           error: str | None = None) -> StickerView:
        """失败只更新识图状态；成功只写模型说明，永不修改管理员标签。"""
        if (not isinstance(expected_revision, int) or isinstance(expected_revision, bool)
                or expected_revision < 1):
            raise MediaError('STICKER_INVALID_METADATA', status_code=422)
        if error is not None:
            if not isinstance(error, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,99}', error):
                raise MediaError('STICKER_INVALID_METADATA', status_code=422)
            values = {'analysis_status': 'failed', 'analysis_error': error}
        else:
            try:
                patch = StickerPatch(expected_revision=expected_revision, description=description,
                                     emotion=emotion, usage_hint=usage_hint)
            except ValidationError as failure:
                raise MediaError('STICKER_INVALID_METADATA', status_code=422) from failure
            values = patch.model_dump(exclude={'expected_revision'}, exclude_unset=True)
            values.update(analysis_status='ready', analysis_error=None)
        async with self._lock:
            await self._row(id)
            return await self._change(id, expected_revision, values)

    async def read(self, id: str, preview: bool = False) -> tuple[StickerView, bytes]:
        """禁用内容仍可管理预览；在锁内读取并关闭已校验句柄，不跨线程泄漏租约。"""
        async with self._lock:
            row = await self._row(id)
            key = row['preview_key'] if preview else row['storage_key']
            size = row['preview_size_bytes'] if preview else row['size_bytes']
            digest = row['preview_sha256'] if preview else row['sha256']

            def read_file() -> bytes:
                """完整性检查和读取在同一有界工作线程内完成。"""
                with self.files.open(key, size=size, digest=digest) as file:
                    return file.read()
            try:
                data = await finish_database_write(read_file)
            except (MediaError, OSError) as error:
                raise MediaError('STICKER_FILE_CORRUPT') from error
            return _view(row), data

    async def delete(self, id: str, expected_revision: int) -> None:
        """先提交不可见墓碑，再删除文件；中断清理在下次独占启动恢复。"""
        async with self._lock:
            row = await self._row(id)
            await _finish(self._delete(row, expected_revision))

    async def _delete(self, row: dict[str, Any], expected_revision: int) -> None:
        """取消不能在提交墓碑与清理文件之间提前释放目录锁。"""
        await self._change(row['id'], expected_revision,
                           {'deleted': True, 'enabled': False, 'active_sha256': None})
        try:
            for key in (row['storage_key'], row['preview_key']):
                await finish_database_write(lambda key=key: self.files.delete(key))
        except (MediaError, OSError) as error:
            raise MediaError('STICKER_DELETE_FAILED') from error
