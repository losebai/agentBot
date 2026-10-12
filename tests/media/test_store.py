"""媒体文件、身份、租约、取消及崩溃对账的离线验证。"""

import asyncio
import hashlib
import os
import threading
from contextlib import asynccontextmanager

import pytest

from agent_bot.media.contracts import MediaOwner, MediaScope
from agent_bot.media.errors import MediaError
from media.test_inputs import picture

OWNER = MediaOwner(entry="admin", session_id="session")
SCOPE = MediaScope(owner=OWNER, execution_id="a" * 32, execution_kind="agent")


@asynccontextmanager
async def storage(database, params, directory, clock, **kwargs):
    """测试必须主动启动并关闭目录资源，构造函数不能触及磁盘。"""
    from agent_bot.media.files import MediaFiles
    from agent_bot.media.store import MediaStore

    files = MediaFiles(directory)
    files.start()
    store = MediaStore(params, connect=database.connect, files=files, clock=clock, **kwargs)
    try:
        yield store
    finally:
        files.close()


async def test_cross_session_and_qq_asset_are_hidden(
    media_database, agent_params, tmp_path, media_clock
):
    """管理员历史权限仅允许管理端附件；普通访问必须匹配会话。"""
    async with storage(media_database, agent_params, tmp_path, media_clock) as store:
        asset = await store.put(owner=OWNER, purpose="input", data=picture(), mime_type="image/png")
        assert asset.status == "ready" and asset.sha256 == hashlib.sha256(picture()).hexdigest()
        assert "storage_key" not in asset.model_dump()
        assert await store.describe(asset.id, admin_only=True) == asset
        for authority in [
            {},
            {"owner": OWNER, "admin_only": True},
            {"owner": MediaOwner(entry="admin", session_id="other")},
        ]:
            with pytest.raises(MediaError) as error:
                await store.describe(asset.id, **authority)
            assert error.value.status_code == 404
        qq = await store.put(
            owner=MediaOwner(entry="qq", session_id="session"),
            purpose="output",
            data=picture(),
            mime_type="image/png",
        )
        with pytest.raises(MediaError) as error:
            await store.describe(qq.id, admin_only=True)
        assert error.value.status_code == 404


async def test_expired_asset_waits_for_reader_lease(
    media_database, agent_params, tmp_path, media_clock
):
    """到期后新读者收到 410，已有读者结束后才删除文件。"""
    async with storage(media_database, agent_params, tmp_path, media_clock) as store:
        asset = await store.put(
            owner=OWNER, purpose="output", data=picture(), mime_type="image/png", scope=SCOPE
        )
        async with store.open_content(asset.id, owner=OWNER) as content:
            media_clock.advance(86401)
            await store.cleanup(media_clock())
            assert content.read() == picture()
            assert (tmp_path / asset.id).exists()
            with pytest.raises(MediaError) as expired:
                await store.describe(asset.id, owner=OWNER)
            assert expired.value.status_code == 410
        await store.cleanup(media_clock())
        assert not (tmp_path / asset.id).exists()
        assert (await store.list_execution(SCOPE))[0].status == "expired"


async def test_binding_is_atomic_and_scoped(media_database, agent_params, tmp_path, media_clock):
    """整批预校验失败不消费第一个 ID；一旦消费只能重入同一执行。"""
    async with storage(media_database, agent_params, tmp_path, media_clock) as store:
        asset = await store.put(owner=OWNER, purpose="input", data=picture(), mime_type="image/png")
        with pytest.raises(MediaError):
            async with store.bind_inputs((asset.id, "b" * 32), SCOPE):
                pytest.fail("非法批次不应打开租约")
        assert await store.list_execution(SCOPE) == ()
        async with store.bind_inputs((asset.id,), SCOPE) as images:
            assert images[0].data == picture()
        async with store.bind_inputs((asset.id,), SCOPE):
            pass
        other = SCOPE.model_copy(update={"execution_id": "c" * 32})
        with pytest.raises(MediaError) as conflict:
            async with store.bind_inputs((asset.id,), other):
                pass
        assert conflict.value.status_code == 409
        (tmp_path / asset.id).write_bytes(b"corrupt")
        with pytest.raises(MediaError, match="MEDIA_FILE_CORRUPT"):
            async with store.open_content(asset.id, owner=OWNER):
                pass


async def test_quota_includes_pending_writes(
    media_database, agent_params, tmp_path, media_clock, monkeypatch
):
    """两个写入竞争最后一份容量时，只有一个可发布。"""
    async with storage(
        media_database, agent_params, tmp_path, media_clock, quota=len(picture())
    ) as store:
        results = await asyncio.gather(
            *[
                store.put(owner=OWNER, purpose="input", data=picture(), mime_type="image/png")
                for _ in range(2)
            ],
            return_exceptions=True,
        )
        assert (
            len(
                [x for x in results if isinstance(x, MediaError) and x.code == "MEDIA_STORAGE_FULL"]
            )
            == 1
        )
        assert len([x for x in results if not isinstance(x, BaseException)]) == 1


async def test_cancel_between_replace_and_ready_recovers(
    media_database, agent_params, tmp_path, media_clock, monkeypatch
):
    """取消必须等待文件线程归还，未发布的结果不能出现 ready 附件。"""
    async with storage(media_database, agent_params, tmp_path, media_clock) as store:
        replaced, release = threading.Event(), threading.Event()
        original = store.files.write

        def delayed(key, data):
            """在原子替换之后暂停，模拟调用方取消。"""
            original(key, data)
            replaced.set()
            assert release.wait(5)

        monkeypatch.setattr(store.files, "write", delayed)
        task = asyncio.create_task(
            store.put(
                owner=OWNER, purpose="output", data=picture(), mime_type="image/png", scope=SCOPE
            )
        )
        assert await asyncio.to_thread(replaced.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await store.list_execution(SCOPE) == ()
        await store.reconcile(media_clock())
        assert not [path for path in tmp_path.iterdir() if len(path.name) == 32]


async def test_reconcile_does_not_delete_unowned_files(
    media_database, agent_params, tmp_path, media_clock
):
    """启动对账只清理超过一小时的服务端命名孤儿。"""
    (tmp_path / "keep.txt").write_text("用户文件", encoding="utf-8")
    orphan = tmp_path / ("d" * 32 + ".part")
    orphan.write_bytes(b"orphan")
    os.utime(orphan, (0, 0))
    fresh = tmp_path / ("e" * 32)
    fresh.write_bytes(b"fresh")
    async with storage(media_database, agent_params, tmp_path, media_clock) as store:
        await store.reconcile(media_clock())
    assert not orphan.exists()
    assert fresh.exists() and (tmp_path / "keep.txt").exists()


def test_root_lock_and_path_rejection(tmp_path):
    """同目录只允许单个所有者，客户端路径不可参与读写。"""
    from agent_bot.media.files import MediaFiles

    root = tmp_path / "files"
    one, two = MediaFiles(root), MediaFiles(root)
    assert not root.exists()
    one.start()
    try:
        with pytest.raises(MediaError):
            two.start()
        for key in ["../x", "C:\\outside", "a" * 32 + ".exe"]:
            with pytest.raises(MediaError):
                one.write(key, b"test")
    finally:
        one.close()
        two.close()
    two.start()
    two.close()


async def test_commit_failure_keeps_real_quota_until_restart(
    media_database, agent_params, tmp_path, media_clock, monkeypatch
):
    """文件已落盘而数据库故障时保留占用，重建存储后回收中断写入。"""
    async with storage(
        media_database, agent_params, tmp_path, media_clock, quota=len(picture())
    ) as store:

        async def unavailable(id, status):
            """模拟落盘后所有状态提交失败。"""
            raise MediaError("AI_DATABASE_UNAVAILABLE")

        monkeypatch.setattr(store, "_status", unavailable)
        with pytest.raises(MediaError, match="MEDIA_SAVE_FAILED"):
            await store.put(
                owner=OWNER, purpose="output", data=picture(), mime_type="image/png", scope=SCOPE
            )
        assert await store.list_execution(SCOPE) == ()
        with pytest.raises(MediaError, match="MEDIA_STORAGE_FULL"):
            await store.put(owner=OWNER, purpose="input", data=picture(), mime_type="image/png")
    async with storage(
        media_database, agent_params, tmp_path, media_clock, quota=len(picture())
    ) as restarted:
        await restarted.reconcile(media_clock())
        result = await restarted.put(
            owner=OWNER, purpose="input", data=picture(), mime_type="image/png"
        )
        assert result.status == "ready"


def test_reparse_directory_rejected_before_io(tmp_path, monkeypatch):
    """模拟 Windows 联接属性，任何重解析目录都不能创建媒体锁文件。"""
    from pathlib import Path
    from types import SimpleNamespace

    from agent_bot.media.files import MediaFiles

    original = Path.lstat

    def reparse(path):
        """仅给目标根目录注入联接属性，保留其他路径的真实属性。"""
        return (
            SimpleNamespace(st_mode=0, st_file_attributes=0x400)
            if path == tmp_path
            else original(path)
        )

    monkeypatch.setattr(Path, "lstat", reparse)
    with pytest.raises(MediaError, match="MEDIA_STORAGE_UNSAFE"):
        MediaFiles(tmp_path).start()
    assert not (tmp_path / ".agentbot.lock").exists()
