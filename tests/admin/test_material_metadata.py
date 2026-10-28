"""素材分类与标签的管理接口验证，使用独立测试库和目录。"""

import pytest

from agent_bot.media.contracts import MediaOwner

from .test_ai_routes import HEADERS
from .test_media_routes import media_api as media_api


async def test_library_metadata_filter_and_update_are_authenticated(media_api):
    """分类和规范化标签筛选与计数一致，标签替换允许清空且需管理认证。"""
    api, _, runtime, _ = media_api
    store = runtime.media_resources.materials
    owner = MediaOwner(entry="qq", session_id="metadata-session")
    asset = await store.put(owner=owner, name="a.txt", data=b"a", mime_type="text/plain",
                            category="received", tags=("专题",))
    await store.put(owner=owner, name="b.txt", data=b"b", mime_type="text/plain",
                    category="generated", tags=("其他",))
    url = "/api/v1/material-library"
    filters = {"category": "received", "tag": " 专题 ", "page": 1, "limit": 20}
    listed = await api.get(url, params=filters, headers=HEADERS)
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == [asset.id]
    assert (await api.get(f"{url}/count", params=filters, headers=HEADERS)).json() == {"total": 1}
    tags_url = f"{url}/{asset.id}/tags"
    assert (await api.patch(tags_url, json={"tags": ["new"]})).status_code == 401
    updated = await api.patch(tags_url, json={"tags": [" \uff21\uff22\uff23 ", "abc", "旅行"]},
                              headers=HEADERS)
    assert updated.status_code == 200, updated.text
    assert updated.json()["tags"] == ["abc", "旅行"]
    assert updated.json()["categories"] == ["received"]
    assert (await api.get(f"{url}/count", params=filters, headers=HEADERS)).json() == {"total": 0}
    cleared = await api.patch(tags_url, json={"tags": []}, headers=HEADERS)
    assert cleared.status_code == 200 and cleared.json()["tags"] == []
    assert (await api.get(f"{url}/{asset.id}", headers=HEADERS)).json()["tags"] == []


@pytest.mark.parametrize("body", [{"tags": ["x" * 33]}, {"tags": ["bad\nlabel"]},
                                  {"tags": [str(index) for index in range(9)]}])
async def test_invalid_material_tags_do_not_overwrite_existing(media_api, body):
    """标签校验失败不能清除旧标签或改变素材原件。"""
    api, _, runtime, _ = media_api
    asset = await runtime.media_resources.materials.put(
        owner=MediaOwner(entry="qq", session_id="s"), name="a.txt", mime_type="text/plain",
        data=b"a", tags=("original",),
    )
    url = f"/api/v1/material-library/{asset.id}"
    assert (await api.patch(f"{url}/tags", json=body, headers=HEADERS)).status_code == 422
    assert (await api.get(url, headers=HEADERS)).json()["tags"] == ["original"]


async def test_material_metadata_rejects_unknown_category_and_missing_record(media_api):
    """非法分类明确拒绝，编辑不存在素材不产生悬空标签。"""
    api, _, _, _ = media_api
    url = "/api/v1/material-library"
    for path in (url, f"{url}/count"):
        response = await api.get(path, params={"category": "unknown"}, headers=HEADERS)
        assert response.status_code == 422
    response = await api.patch(f"{url}/{'a' * 32}/tags", json={"tags": ["new"]}, headers=HEADERS)
    assert response.status_code == 404
