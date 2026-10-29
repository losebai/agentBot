"""真实临时 SQLite 覆盖表情库修订、文件生命周期和独立目录。"""

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from agent_bot.database import database_connector
from agent_bot.media.errors import MediaError
from agent_bot.media.files import MediaFiles
from agent_bot.plugins.stickers.contracts import StickerPatch
from agent_bot.plugins.stickers.store import StickerStore
from agent_bot.settings import DatabaseParams
from stickers.test_images import picture

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
async def store(tmp_path):
    """只使用测试创建的库和图库目录，不读取安装资源或真实配置。"""
    path = tmp_path / 'test.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.executescript((ROOT / 'sql/stickers-init.sqlite.sql').read_text('utf-8'))
    params = DatabaseParams(backend='sqlite', sqlite_path=str(path))
    result = StickerStore(params, connect=database_connector(params),
                          files=MediaFiles(tmp_path / 'stickers'))
    await result.start()
    try:
        yield result
    finally:
        await result.aclose()


async def test_animation_tags_sparse_update_and_analysis_revision(store):
    """识图不能覆盖自定义标签或并发编辑，失败保留旧说明。"""
    data = picture('GIF', animated=True)
    item = await store.create('../../笑', data, 'image/gif', tags=(' 开心 ', '开心', '招呼'))
    assert item.tags == ('开心', '招呼') and item.revision == 1
    assert 'storage_key' not in item.model_dump()
    assert (await store.read(item.id))[1] == data
    assert (await store.read(item.id, preview=True))[1].startswith(b'\x89PNG')
    updated = await store.update(item.id, StickerPatch(expected_revision=1, description='管理员'))
    assert updated.tags == item.tags and updated.name == item.name and updated.revision == 2
    with pytest.raises(MediaError, match='STICKER_CONFLICT'):
        await store.set_analysis(item.id, expected_revision=1, description='旧识图',
                                 emotion='开心', usage_hint='打招呼')
    ready = await store.set_analysis(item.id, expected_revision=2, description='新识图',
                                     emotion='开心', usage_hint='打招呼')
    assert ready.analysis_status == 'ready' and ready.tags == item.tags
    failed = await store.set_analysis(item.id, expected_revision=ready.revision,
                                      error='STICKER_MODEL_UNAVAILABLE')
    assert failed.analysis_status == 'failed' and failed.description == '新识图'
    assert failed.analysis_error == 'STICKER_MODEL_UNAVAILABLE'
    assert {p.name for p in store.files.root.iterdir()} == {
        '.agentbot.lock', *[p.name for p in store.files.root.iterdir() if len(p.name) == 32],
    }
    assert len(list(store.files.root.iterdir())) == 3


async def test_filter_page_disabled_and_delete(store):
    """管理端仍可预览禁用图片，删除后任何检索与读取均不可访问。"""
    first = await store.create('招呼', picture(), 'image/png', tags=('开心',))
    second = await store.create('告别', picture('JPEG'), 'image/jpeg', tags=('伤心',))
    disabled = await store.update(first.id, StickerPatch(expected_revision=1, enabled=False))
    assert (await store.read(first.id))[1] == picture()
    assert (await store.list(enabled=True))['total'] == 1
    assert (await store.list(query='招呼', tag='开心'))['items'] == [disabled]
    page = await store.list(limit=1)
    assert page['total'] == 2 and page['next_offset'] == 1
    assert (await store.list(offset=1, limit=1))['next_offset'] is None
    await store.delete(first.id, expected_revision=disabled.revision)
    for method in (store.get, store.read):
        with pytest.raises(MediaError, match='STICKER_NOT_FOUND'):
            await method(first.id)
    assert (await store.list())['items'] == [second]
    assert len(list(store.files.root.iterdir())) == 3


async def test_corrupt_file_and_revision_conflict_do_not_publish_changes(store):
    """文件完整性失败报错，旧修订删除不能影响现有图库。"""
    item = await store.create('图片', picture(), 'image/png')
    with pytest.raises(MediaError, match='STICKER_CONFLICT'):
        await store.delete(item.id, expected_revision=2)
    assert await store.get(item.id) == item
    originals = [p for p in store.files.root.iterdir() if p.name != '.agentbot.lock']
    for path in originals:
        path.write_bytes(b'broken')
    with pytest.raises(MediaError, match='STICKER_FILE_CORRUPT'):
        await store.read(item.id)


async def test_duplicate_cleanup_and_reupload_after_delete(store):
    """重复上传清除本次临时文件，删除旧条目后允许再次使用同一原件。"""
    first = await store.create('原件', picture(), 'image/png')
    with pytest.raises(MediaError, match='STICKER_DUPLICATE'):
        await store.create('重复', picture(), 'image/png')
    assert len(list(store.files.root.iterdir())) == 3
    assert (await store.list())['total'] == 1
    await store.delete(first.id, expected_revision=first.revision)
    replacement = await store.create('再次上传', picture(), 'image/png')
    assert replacement.id != first.id and replacement.sha256 == first.sha256


async def test_partial_write_failure_cleans_files_and_does_not_publish(store, monkeypatch):
    """预览写入失败不能留下原件或可检索元数据。"""
    original = store.files.write
    count = 0

    def write(key, data):
        """模拟原件成功而预览落盘失败。"""
        nonlocal count
        count += 1
        if count == 2:
            raise OSError('offline disk error')
        original(key, data)

    monkeypatch.setattr(store.files, 'write', write)
    with pytest.raises(MediaError, match='STICKER_SAVE_FAILED'):
        await store.create('失败', picture(), 'image/png')
    assert (await store.list())['total'] == 0
    assert [p.name for p in store.files.root.iterdir()] == ['.agentbot.lock']


async def test_cancelled_read_drains_thread_before_close(store, monkeypatch):
    """取消不能提前释放锁或关闭工作线程仍在使用的目录。"""
    item = await store.create('读取', picture(), 'image/png')
    entered, release = threading.Event(), threading.Event()
    original = store.files.open

    def open_file(*args, **kwargs):
        """受控阻塞证明关闭确实等待活动读取。"""
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(store.files, 'open', open_file)
    reading = asyncio.create_task(store.read(item.id))
    await asyncio.wait_for(asyncio.to_thread(entered.wait), 3)
    reading.cancel()
    closing = asyncio.create_task(store.aclose())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await reading
    await closing
    assert store.files._lock_file is None
    with pytest.raises(MediaError, match='STICKER_STORE_UNAVAILABLE'):
        await store.get(item.id)


@pytest.mark.parametrize('tag', ['含\x00零', '含\n换行', '含\t制表', '含\u200b隐藏'])
def test_tag_control_characters_are_rejected(tag):
    """标签不允许控制、空字节或隐藏格式字符。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        StickerPatch(expected_revision=1, tags=(tag,))


async def test_start_checks_empty_table_columns_and_releases_lock(tmp_path):
    """空表也必须验证字段完整性，启动失败不能独占目录或伪装可用。"""
    from agent_bot.ai.errors import AiError

    path = tmp_path / 'incomplete.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE agent_bot_sticker(id TEXT)')
    params = DatabaseParams(backend='sqlite', sqlite_path=str(path))
    files = MediaFiles(tmp_path / 'stickers')
    result = StickerStore(params, connect=database_connector(params), files=files)
    with pytest.raises(AiError, match='AI_NOT_INITIALIZED'):
        await result.start()
    assert files._lock_file is None


async def test_delete_failure_is_hidden_and_startup_resumes_cleanup(store, monkeypatch):
    """墓碑先提交，磁盘清理失败不会让已删除图片继续被检索或发送。"""
    item = await store.create('删除中断', picture(), 'image/png')
    original = store.files.delete

    def failed_delete(*args, **kwargs):
        """模拟提交删除后文件系统不可写。"""
        raise OSError('offline disk error')

    monkeypatch.setattr(store.files, 'delete', failed_delete)
    with pytest.raises(MediaError, match='STICKER_DELETE_FAILED'):
        await store.delete(item.id, expected_revision=item.revision)
    with pytest.raises(MediaError, match='STICKER_NOT_FOUND'):
        await store.get(item.id)
    assert (await store.list())['total'] == 0
    await store.aclose()
    monkeypatch.setattr(store.files, 'delete', original)
    await store.start()
    assert [p.name for p in store.files.root.iterdir()] == ['.agentbot.lock']
