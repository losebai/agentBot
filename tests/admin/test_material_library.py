"""管理员素材库跨来源只读访问，不改变会话工具的权限。"""

import asyncio
import threading
from io import BytesIO

import pytest
from PIL import Image

from agent_bot.media.contracts import MediaOwner
from media.test_inputs import picture

from .test_ai_routes import HEADERS
from .test_media_routes import media_api as media_api


async def test_library_lists_all_sources_and_downloads_with_admin_auth(media_api):
    """素材库读取 QQ 与管理原件，未认证和原会话接口仍不能越权。"""
    api, _, runtime, provider = media_api
    store = runtime.media_resources.materials
    qq = await store.put(owner=MediaOwner(entry="qq", session_id="qq-session"),
                         name="计划.txt", mime_type="text/plain", data=b"qq-plan")
    admin = await store.put(owner=MediaOwner(entry="admin", session_id="admin_s"),
                            name="管理.txt", mime_type="text/plain", data=b"admin-plan")
    url = "/api/v1/material-library"
    for path in (url, f"{url}/{qq.id}", f"{url}/{qq.id}/content"):
        assert (await api.get(path)).status_code == 401
    response = await api.get(url, headers=HEADERS)
    assert response.status_code == 200, response.text
    records = {item["id"]: item for item in response.json()["items"]}
    assert set(records) == {qq.id, admin.id}
    assert records[qq.id]["entry"] == "qq"
    assert records[qq.id]["session_id"] == "qq-session"
    assert "storage_key" not in response.text and "source_key" not in response.text
    detail = await api.get(f"{url}/{qq.id}", headers=HEADERS)
    assert detail.json()["description"] == ""
    content = await api.get(f"{url}/{qq.id}/content", headers=HEADERS)
    assert content.content == b"qq-plan"
    assert content.headers["content-disposition"].startswith("attachment")
    assert content.headers["cache-control"] == "no-store"
    assert content.headers["x-content-type-options"] == "nosniff"
    scoped = await api.get("/api/v1/materials?session_id=s", headers=HEADERS)
    assert [item["id"] for item in scoped.json()["records"]] == [admin.id]
    assert (await api.get(f"/api/v1/materials/{qq.id}/content?session_id=s",
                          headers=HEADERS)).status_code == 404
    provider.generate.assert_not_called()


async def test_library_cursor_and_literal_filters_do_not_skip_or_repeat(media_api):
    """游标绑定原筛选，新素材插入不能让后续页重复已有条目。"""
    api, _, runtime, _ = media_api
    store = runtime.media_resources.materials
    for index in range(3):
        await store.put(owner=MediaOwner(entry="qq", session_id="qq-session"),
                        name=f"计划_100%_{index}.txt", mime_type="text/plain",
                        data=f"plan-{index}".encode(), description="完整说明" * 80)
    await store.put(owner=MediaOwner(entry="admin", session_id="admin_s"),
                    name="计划_100%_other.txt", mime_type="text/plain", data=b"other")
    url = "/api/v1/material-library"
    params = {"query": "_100%", "entry": "qq", "session_id": "qq-session",
              "kind": "file", "limit": 1}
    first = await api.get(url, params=params, headers=HEADERS)
    assert first.status_code == 200, first.text
    page = first.json()
    assert len(page["items"]) == 1 and page["next_cursor"]
    seen = {page["items"][0]["id"]}
    await store.put(owner=MediaOwner(entry="qq", session_id="qq-session"),
                    name="计划_100%_new.txt", mime_type="text/plain", data=b"new")
    for _ in range(2):
        result = await api.get(url, params={**params, "cursor": page["next_cursor"]},
                               headers=HEADERS)
        assert result.status_code == 200, result.text
        page = result.json()
        assert len(page["items"]) == 1
        assert page["items"][0]["id"] not in seen
        seen.add(page["items"][0]["id"])
    assert page["next_cursor"] is None
    assert len(seen) == 3
    wrong_filter = await api.get(url, params={**params, "query": "other",
                                             "cursor": first.json()["next_cursor"]},
                                 headers=HEADERS)
    assert wrong_filter.status_code == 422
    empty = await api.get(url, params={"query": "_999%"}, headers=HEADERS)
    assert empty.json()["items"] == []
    detail = await api.get(f"{url}/{next(iter(seen))}", headers=HEADERS)
    assert len(detail.json()["description"]) == 320


@pytest.mark.parametrize("params", [{"kind": "audio"}, {"entry": "user"},
                                         {"limit": 101}, {"cursor": "bad"}])
async def test_library_rejects_invalid_filter_or_cursor(media_api, params):
    """拒绝不支持的类型、来源及无效游标，不能悄悄重置查询条件。"""
    api, _, _, _ = media_api
    response = await api.get("/api/v1/material-library", params=params, headers=HEADERS)
    assert response.status_code == 422


async def test_library_content_validates_corruption_before_success(media_api):
    """跨来源读取仍需完整性校验，损坏原件不发送成功响应头。"""
    api, _, runtime, _ = media_api
    store = runtime.media_resources.materials
    asset = await store.put(owner=MediaOwner(entry="qq", session_id="qq-session"),
                            name="计划.txt", mime_type="text/plain", data=b"plan")
    store.files.path(asset.id).write_bytes(b"bad")
    response = await api.get(f"/api/v1/material-library/{asset.id}/content", headers=HEADERS)
    assert response.status_code == 502
    assert response.json()["code"] == "MEDIA_FILE_CORRUPT"
    assert (await api.get(f"/api/v1/material-library/{'a' * 32}",
                          headers=HEADERS)).status_code == 404


async def test_library_numbered_pages_and_count_share_literal_filters(media_api):
    """页码和独立计数使用相同字面筛选，条数变化不会污染旧游标。"""
    api, _, runtime, _ = media_api
    store = runtime.media_resources.materials
    owner = MediaOwner(entry="qq", session_id="qq-session")
    for index in range(5):
        await store.put(owner=owner, name=f"计划_100%_{index}.txt", mime_type="text/plain",
                        data=f"plan-{index}".encode())
    for name, source in [("计划X100Y.txt", owner),
                         ("计划_100%.txt", MediaOwner(entry="admin", session_id="admin_s")),
                         ("计划_100%.txt", MediaOwner(entry="qq", session_id="other"))]:
        await store.put(owner=source, name=name, mime_type="text/plain", data=name.encode())
    await store.put(owner=owner, name="计划_100%.png", mime_type="image/png", data=picture())
    url = "/api/v1/material-library"
    filters = {"query": "_100%", "entry": "qq", "session_id": "qq-session", "kind": "file"}
    assert (await api.get(f"{url}/count")).status_code == 401
    count = await api.get(f"{url}/count", params=filters, headers=HEADERS)
    assert count.status_code == 200, count.text
    assert count.json() == {"total": 5}
    assert (await api.get(f"{url}/count", params={"query": "missing"},
                          headers=HEADERS)).json() == {"total": 0}
    all_items = (await api.get(url, params=filters, headers=HEADERS)).json()["items"]
    seen = []
    for page, size in [(1, 2), (2, 2), (3, 1), (4, 0)]:
        response = await api.get(url, params={**filters, "page": page, "limit": 2},
                                 headers=HEADERS)
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert len(items) == size
        seen.extend(item["id"] for item in items)
    assert seen == [item["id"] for item in all_items]
    resized = await api.get(url, params={**filters, "page": 2, "limit": 3}, headers=HEADERS)
    assert [item["id"] for item in resized.json()["items"]] == seen[3:]
    first = await api.get(url, params={**filters, "limit": 2}, headers=HEADERS)
    second = await api.get(url, params={**filters, "limit": 3,
                                       "cursor": first.json()["next_cursor"]}, headers=HEADERS)
    assert [item["id"] for item in second.json()["items"]] == seen[2:]


@pytest.mark.parametrize("params", [{"page": 0}, {"page": -1},
                                    {"page": 1, "cursor": "bad"}])
async def test_library_invalid_page_has_clear_error(media_api, params):
    """无效页码与混用游标明确失败，不静默回到第一页。"""
    api, _, _, _ = media_api
    response = await api.get("/api/v1/material-library", params=params, headers=HEADERS)
    assert response.status_code == 422
    assert response.json()["code"] == "MATERIAL_INVALID_PAGE"


async def test_library_thumbnail_is_small_oriented_and_preserves_original(media_api):
    """缩略图校正 EXIF 方向并保留原件，图片字节只向已认证管理员提供。"""
    api, _, runtime, provider = media_api
    store = runtime.media_resources.materials
    exif = Image.Exif()
    exif[274] = 6
    original = picture("JPEG", size=(800, 400), exif=exif)
    asset = await store.put(owner=MediaOwner(entry="qq", session_id="qq-session"),
                            name="照片.jpg", mime_type="image/jpeg", data=original)
    url = f"/api/v1/material-library/{asset.id}"
    assert (await api.get(f"{url}/thumbnail")).status_code == 401
    thumbnail = await api.get(f"{url}/thumbnail", headers=HEADERS)
    assert thumbnail.status_code == 200, thumbnail.text
    assert thumbnail.headers["content-type"] == "image/png"
    assert thumbnail.headers["cache-control"] == "no-store"
    assert thumbnail.headers["x-content-type-options"] == "nosniff"
    with Image.open(BytesIO(thumbnail.content)) as image:
        assert image.size == (80, 160)
        assert image.getexif() == {}
    assert (await api.get(f"{url}/content", headers=HEADERS)).content == original
    assert store.files.path(asset.id).read_bytes() == original
    provider.generate.assert_not_called()


async def test_library_thumbnail_preserves_palette_transparency(media_api):
    """调色板 PNG 的透明索引必须转为真实透明通道，不能显示成不透明色块。"""
    api, _, runtime, _ = media_api
    original = BytesIO()
    with Image.new("P", (20, 10)) as source:
        source.putpalette([255, 0, 0, 0, 255, 0] + [0] * 762)
        source.putpixel((1, 1), 1)
        source.save(original, format="PNG", transparency=0)
    asset = await runtime.media_resources.materials.put(
        owner=MediaOwner(entry="qq", session_id="qq-session"),
        name="透明.png", mime_type="image/png", data=original.getvalue(),
    )
    response = await api.get(f"/api/v1/material-library/{asset.id}/thumbnail", headers=HEADERS)
    assert response.status_code == 200, response.text
    with Image.open(BytesIO(response.content)) as thumbnail:
        rgba = thumbnail.convert("RGBA")
        try:
            assert rgba.getpixel((0, 0))[3] == 0
            assert rgba.getpixel((1, 1)) == (0, 255, 0, 255)
        finally:
            rgba.close()


@pytest.mark.parametrize("failure,code,status", [("file", "MATERIAL_INVALID_FILE", 422),
                                                 ("missing", "MEDIA_FILE_CORRUPT", 502),
                                                 ("corrupt", "MEDIA_FILE_CORRUPT", 502)])
async def test_library_thumbnail_rejects_invalid_or_damaged_original(
    media_api, failure, code, status,
):
    """类型和完整性失败在成功响应前返回稳定错误，不披露磁盘地址。"""
    api, _, runtime, _ = media_api
    store = runtime.media_resources.materials
    asset = await store.put(owner=MediaOwner(entry="qq", session_id="qq-session"),
                            name="素材",
                            mime_type="text/plain" if failure == "file" else "image/png",
                            data=b"plan" if failure == "file" else picture())
    path = store.files.path(asset.id)
    if failure == "missing":
        path.unlink()
    elif failure == "corrupt":
        path.write_bytes(b"bad")
    response = await api.get(f"/api/v1/material-library/{asset.id}/thumbnail", headers=HEADERS)
    assert response.status_code == status, response.text
    assert response.json()["code"] == code
    assert str(path) not in response.text


async def test_cancelled_thumbnail_waits_for_encoder_before_releasing_lease(media_api, monkeypatch):
    """取消不能抢先关闭编码线程使用的原件，也不能提前归还目录锁。"""
    _, _, runtime, _ = media_api
    store = runtime.media_resources.materials
    asset = await store.put(owner=MediaOwner(entry="qq", session_id="qq-session"),
                            name="图片", mime_type="image/png", data=picture(size=(800, 400)))
    entered, release = threading.Event(), threading.Event()
    save = Image.Image.save
    files = []
    open_file = store.files.open

    def track_open(*args, **kwargs):
        """记录真实原件句柄，验证取消期间仍保持租约。"""
        file = open_file(*args, **kwargs)
        files.append(file)
        return file

    def slow_save(*args, **kwargs):
        """用线程事件控制编码结束，不依赖调度时间猜测。"""
        entered.set()
        assert release.wait(5)
        return save(*args, **kwargs)

    monkeypatch.setattr(store.files, "open", track_open)
    monkeypatch.setattr(Image.Image, "save", slow_save)
    task = asyncio.create_task(store.library_thumbnail(asset.id))
    assert await asyncio.to_thread(entered.wait, 5)
    try:
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert store._lock.locked()
        assert files and not files[0].closed
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert files[0].closed
    assert not store._lock.locked()
