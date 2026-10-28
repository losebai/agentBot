"""通过真实 SQLite 验证工作流管理权限、同步保留规则和并发编辑边界。"""

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI

from agent_bot.admin.config import AdminConfig
from agent_bot.admin.routes import install_admin
from agent_bot.integrations.workflow_store import WorkflowRevisionConflictError, WorkflowStore
from agent_bot.services.workflows import WorkflowCatalogImporter, WorkflowMetadataPatch
from agent_bot.settings import DatabaseParams

HEADERS = {"Authorization": "Bearer offline-admin"}


@pytest.fixture
def workflows(tmp_path, admin_runtime):
    """建独立业务库；管理请求不得创建或迁移数据表。"""
    root = Path(__file__).parents[2]
    path = tmp_path / "workflows.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript((root / "sql/agent_bot_init.sqlite.sql").read_text("utf-8"))
    store = WorkflowStore(DatabaseParams(backend="sqlite", sqlite_path=str(path)))
    admin_runtime.workflow_store = store
    return store


@pytest.fixture
def workflow_json():
    """使用正式 API 模板，测试不连接 Comfy 或生成图片。"""
    return (Path(__file__).parents[2] / "src/resources/sdxl_lightning_4step_api.json").read_text(
        "utf-8"
    )


@pytest.fixture
async def workflow_api(admin_store, admin_runtime, workflows):
    """所有 HTTP 请求走实际认证和生产存储。"""
    app = FastAPI()
    install_admin(app, admin_store, admin_runtime, AdminConfig(token="offline-admin"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://offline.test"
    ) as client:
        yield client


async def import_one(api, content, filename="portrait.json"):
    """导入正常 JSON 并取回服务器生成的版本。"""
    result = await api.post(
        "/api/v1/workflows/import", headers=HEADERS,
        json={"filename": filename, "content": content},
    )
    assert result.status_code == 200, result.text
    return (await api.get("/api/v1/workflows", headers=HEADERS)).json()["items"][0]


@pytest.mark.parametrize('method,path,body', [
    ('GET', '/api/v1/workflows', None),
    ('POST', '/api/v1/workflows/sync', None),
    ('POST', '/api/v1/workflows/import', {'filename': 'disabled.json', 'content': '{}'}),
])
async def test_disabled_comfy_denies_formal_workflow_http(
    admin_store, admin_runtime, workflows, method, path, body,
):
    """正式宿主的禁用门槛返回 503，不因数据库仍存有工作流而开放管理。"""
    from agent_bot.admin.application_runtime import ApplicationRuntimeProvider

    plugins = SimpleNamespace(running_plugin=Mock(return_value=None))
    runtime = SimpleNamespace(config=admin_runtime.config, workflow_store=workflows,
                              lease=lambda **kwargs: admin_runtime.lease())
    provider = ApplicationRuntimeProvider(runtime, plugins)
    app = FastAPI()
    install_admin(app, admin_store, provider, AdminConfig(token="offline-admin"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url='http://offline.test') as client:
        result = await client.request(method, path, headers=HEADERS, json=body)
    assert result.status_code == 503
    assert result.json()['code'] == 'WORKFLOW_NOT_READY'
    plugins.running_plugin.assert_called_with('comfy')


async def test_h3_import_requires_enable_and_cannot_replace_default_image(workflow_api):
    """视频可同步和启用，但不能被默认生图入口选择。"""
    content = await asyncio.to_thread(
        Path('src/resources/minimax_h3_r2v_upscale2x_api.json').read_text, 'utf-8',
    )
    item = await import_one(workflow_api, content, filename='h3.json')
    assert item['workflow_type'] == 'image_to_video'
    assert item['executable'] and not item['enabled']
    result = await workflow_api.patch('/api/v1/workflows/h3', headers=HEADERS,
        json={'expected_revision': item['revision'], 'values': {'enabled': True}})
    assert result.status_code == 200, result.text
    catalog = (await workflow_api.get('/api/v1/workflows', headers=HEADERS)).json()
    response = await workflow_api.post('/api/v1/workflows/h3/default', headers=HEADERS,
        json={'expected_revision': catalog['settings_revision']})
    assert response.status_code == 422


async def test_workflow_import_and_detail_respect_authentication(workflow_api, workflow_json):
    """工作流正文仅对管理员开放，列表不包含大体积原始 JSON。"""
    assert (await workflow_api.get("/api/v1/workflows")).status_code == 401
    assert (await workflow_api.post(
        "/api/v1/workflows/import", json={"filename": "a.json", "content": workflow_json}
    )).status_code == 401
    item = await import_one(workflow_api, workflow_json)
    assert item["workflow_key"] == "portrait"
    assert item["executable"] and item["enabled"]
    assert len(item["revision"]) == 64 and "api_json" not in item
    detail = await workflow_api.get("/api/v1/workflows/portrait", headers=HEADERS)
    assert json.loads(detail.json()["api_json"]) == json.loads(workflow_json)
    assert detail.headers["cache-control"] == "no-store"


async def test_workflow_edit_disable_and_stale_revision(
    workflow_api, workflow_json, workflows,
):
    """禁用记录仍能编辑；旧窗口不能覆盖新设置，业务信息保存后实际可读。"""
    item = await import_one(workflow_api, workflow_json)
    values = {
        "display_name": "写实人像", "description": "适合证件照",
        "additional_positive_prompt": "高清", "additional_negative_prompt": "水印",
        "enabled": False,
    }
    response = await workflow_api.patch(
        "/api/v1/workflows/portrait", headers=HEADERS,
        json={"expected_revision": item["revision"], "values": values},
    )
    assert response.status_code == 200
    assert response.json()["revision"] != item["revision"]
    saved = workflows.list_all()[0]
    assert saved.display_name == "写实人像" and saved.description == "适合证件照"
    assert saved.additional_positive_prompt == "高清" and not saved.enabled
    conflict = await workflow_api.patch(
        "/api/v1/workflows/portrait", headers=HEADERS,
        json={"expected_revision": item["revision"], "values": {"description": "stale"}},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "WORKFLOW_CONFLICT"
    detail = await workflow_api.get("/api/v1/workflows/portrait", headers=HEADERS)
    assert detail.status_code == 200


async def test_workflow_reimport_preserves_metadata_and_invalidates_old_edit(
    workflow_api, workflow_json, workflows,
):
    """JSON 更新刷新技术版本，管理员维护的提示词、名称和停用状态保留。"""
    item = await import_one(workflow_api, workflow_json)
    edited = await workflow_api.patch(
        "/api/v1/workflows/portrait", headers=HEADERS,
        json={"expected_revision": item["revision"], "values": {
            "display_name": "自定义名称", "description": "用途",
            "additional_positive_prompt": "高清", "enabled": False,
        }},
    )
    updated_json = json.loads(workflow_json)
    updated_json["10"]["inputs"]["steps"] = 6
    await import_one(workflow_api, json.dumps(updated_json))
    saved = workflows.list_all()[0]
    assert saved.display_name == "自定义名称" and saved.description == "用途"
    assert saved.additional_positive_prompt == "高清" and not saved.enabled
    assert json.loads(saved.api_json)["10"]["inputs"]["steps"] == 6
    conflict = await workflow_api.patch(
        "/api/v1/workflows/portrait", headers=HEADERS,
        json={"expected_revision": edited.json()["revision"], "values": {"enabled": True}},
    )
    assert conflict.status_code == 409


async def test_default_workflow_uses_settings_revision(workflow_api, workflow_json, admin_store):
    """默认选择只允许启用的可执行模板，并遵循机器人配置并发版本。"""
    item = await import_one(workflow_api, workflow_json)
    result = await workflow_api.post(
        "/api/v1/workflows/portrait/default", headers=HEADERS,
        json={"expected_revision": 1},
    )
    assert result.status_code == 200
    assert result.json()["default_key"] == "portrait"
    assert result.json()["settings_revision"] == 2
    assert admin_store.load().config.chat_local_workflow_default_key == "portrait"
    stale = await workflow_api.post(
        "/api/v1/workflows/portrait/default", headers=HEADERS, json={"expected_revision": 1},
    )
    assert stale.status_code == 409
    await workflow_api.patch(
        "/api/v1/workflows/portrait", headers=HEADERS,
        json={"expected_revision": item["revision"], "values": {"enabled": False}},
    )
    disabled = await workflow_api.post(
        "/api/v1/workflows/portrait/default", headers=HEADERS, json={"expected_revision": 2},
    )
    assert disabled.status_code == 422
    assert admin_store.load().revision == 2


async def test_unexecutable_canvas_stays_disabled(workflow_api):
    """普通画布可保留用于查看，但不能误标为可执行或默认模板。"""
    item = await import_one(workflow_api, '{"nodes": []}', "notes.json")
    assert not item["executable"] and not item["enabled"]
    result = await workflow_api.patch(
        "/api/v1/workflows/notes", headers=HEADERS,
        json={"expected_revision": item["revision"], "values": {"enabled": True}},
    )
    assert result.status_code == 422


@pytest.mark.parametrize("filename,content", [
    ("../escape.json", "{}"), ("C:\\secret.json", "{}"), ("a.meta.json", "{}"),
    ("bad.txt", "{}"), ("bad.json", "not json"), ("array.json", "[]"),
    ("large.json", '{"text":"' + "中" * 350_000 + '"}'),
], ids=["traversal", "absolute", "metadata", "extension", "json", "array", "utf8-limit"])
async def test_import_rejects_invalid_file_without_writing(
    workflow_api, workflows, filename, content,
):
    """文件名不能指定服务器路径，实际 UTF-8 字节限制不能按字符数绕过。"""
    response = await workflow_api.post(
        "/api/v1/workflows/import", headers=HEADERS,
        json={"filename": filename, "content": content},
    )
    assert response.status_code in {413, 422}
    assert not workflows.list_all()


async def test_import_allows_json_above_normal_management_body_limit(workflow_api, workflow_json):
    """工作流正文有独立文件限额，超过普通配置的 64KiB 仍可正常导入。"""
    item = await import_one(workflow_api, " " * 70_000 + workflow_json)
    assert item["executable"]


async def test_sync_uses_live_directory_and_validates_entire_batch(
    workflow_api, admin_runtime, workflow_json, tmp_path, workflows,
):
    """网页不能指定任意目录；坏文件使整批中止，不产生半次同步。"""
    unavailable = await workflow_api.post("/api/v1/workflows/sync", headers=HEADERS)
    assert unavailable.status_code == 422
    directory = tmp_path / "catalog"
    directory.mkdir()
    (directory / "portrait.json").write_text(workflow_json, encoding="utf-8")
    (directory / "broken.json").write_text("broken", encoding="utf-8")
    admin_runtime.config.chat_local_workflow_directory = str(directory)
    invalid = await workflow_api.post("/api/v1/workflows/sync", headers=HEADERS)
    assert invalid.status_code == 422 and not workflows.list_all()
    (directory / "broken.json").unlink()
    result = await workflow_api.post("/api/v1/workflows/sync", headers=HEADERS)
    assert result.status_code == 200 and result.json() == {"imported": 1}
    assert workflows.list_all()[0].workflow_key == "portrait"
    catalog = await workflow_api.get("/api/v1/workflows", headers=HEADERS)
    assert catalog.json()["directory_configured"]


async def test_workflow_errors_are_safe_and_missing_rows_are_404(workflow_api, workflows):
    """数据库异常不回显路径、凭据和 SQL，缺失记录使用稳定的 404。"""
    missing = await workflow_api.get("/api/v1/workflows/missing", headers=HEADERS)
    assert missing.status_code == 404
    path = workflows._params.sqlite_path
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE chat_workflow")
    response = await workflow_api.get("/api/v1/workflows", headers=HEADERS)
    assert response.status_code == 503
    assert path not in response.text
    with sqlite3.connect(path) as connection:
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name='chat_workflow'"
        ).fetchall()


async def test_two_editors_cannot_both_save_same_workflow_version(
    workflow_api, workflow_json, workflows,
):
    """不同连接同时保存同一版本时仅一方提交，另一方收到明确冲突。"""
    item = await import_one(workflow_api, workflow_json)

    def save(description):
        """生产事务负责锁定，各编辑者使用独立数据库连接。"""
        try:
            workflows.update_metadata(
                "portrait", expected_revision=item["revision"],
                values=WorkflowMetadataPatch(description=description),
            )
            return "saved"
        except WorkflowRevisionConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, ("first", "second")))
    assert sorted(results) == ["conflict", "saved"]
    assert workflows.list_all()[0].description in {"first", "second"}


@pytest.mark.parametrize("values", [
    {"description": None}, {"enabled": "yes"}, {"api_json": "{}"}, {"display_name": " "}, {},
], ids=["null", "boolean", "technical-field", "empty-name", "empty-patch"])
async def test_metadata_rejects_null_and_technical_changes(
    workflow_api, workflow_json, workflows, values,
):
    """管理编辑不能清空不可空列或以业务补丁覆盖执行模板。"""
    item = await import_one(workflow_api, workflow_json)
    result = await workflow_api.patch(
        "/api/v1/workflows/portrait", headers=HEADERS,
        json={"expected_revision": item["revision"], "values": values},
    )
    assert result.status_code == 422
    assert workflows.list_all()[0].display_name == "portrait"


async def test_import_wire_limit_and_unauthorized_large_body(workflow_api, workflows):
    """鉴权先于大正文读取，授权正文仍按实际分块限制占用。"""
    body = b"x" * (6 * 1_048_576 + 8193)
    assert (await workflow_api.post("/api/v1/workflows/import", content=body)).status_code == 401
    result = await workflow_api.post("/api/v1/workflows/import", headers=HEADERS, content=body)
    assert result.status_code == 413
    assert not workflows.list_all()


async def test_catalog_cursor_exposes_workflows_beyond_first_hundred(
    workflow_api, workflows, workflow_json,
):
    """多次导入可累计超过单批上限，所有成功保存的记录都必须能继续管理。"""
    records = []
    payload = json.loads(workflow_json)
    for index in range(101):
        payload["2"]["inputs"]["text"] = f"portrait-{index}"
        records.append(WorkflowCatalogImporter().parse_json(
            f"portrait-{index:03}.json", json.dumps(payload),
        ))
    workflows.upsert(records[:100])
    workflows.upsert(records[100:])
    first = (await workflow_api.get("/api/v1/workflows", headers=HEADERS)).json()
    assert len(first["items"]) == 100 and first["next_cursor"] == "portrait-099"
    second = (await workflow_api.get(
        "/api/v1/workflows", headers=HEADERS, params={"after": first["next_cursor"]},
    )).json()
    assert [row["workflow_key"] for row in second["items"]] == ["portrait-100"]
    assert second["next_cursor"] is None
