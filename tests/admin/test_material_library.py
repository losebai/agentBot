"""管理员素材库跨来源只读访问，不改变会话工具的权限。"""

import pytest

from agent_bot.media.contracts import MediaOwner

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
