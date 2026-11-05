"""人物图保存、并发版本与失败回滚使用独立数据库验证。"""

import base64
import hashlib
from io import BytesIO

import pytest
from PIL import Image

from agent_bot.agent.catalog import AgentCatalog
from agent_bot.agent.contracts import AgentCreate, AgentUpdate
from agent_bot.agent.portraits import PortraitUpload
from agent_bot.ai.errors import AiError


def portrait(color="red"):
    """生成无外部资源依赖的小型 PNG。"""
    stream = BytesIO()
    Image.new("RGB", (3, 3), color).save(stream, format="PNG")
    raw = stream.getvalue()
    return raw, PortraitUpload(mime_type="image/png", data=base64.b64encode(raw).decode())


async def test_portrait_persists_replaces_and_cascades(agent_database, agent_params, seeded_model):
    """重建目录仍能读原图，普通修改保留图片，显式清空及删除不会留下孤立数据。"""
    catalog = AgentCatalog(agent_params, connect=agent_database.connect)
    raw, upload = portrait()
    agent = await catalog.create_agent(AgentCreate(
        code="portrait", name="人物", model_id=seeded_model[2].id, portrait=upload))
    assert agent.portrait_sha256 == hashlib.sha256(raw).hexdigest()
    catalog = AgentCatalog(agent_params, connect=agent_database.connect)
    assert (await catalog.portrait_content(agent.id))["image_data"] == raw
    listed = (await catalog.list_agents()).model_dump_json()
    assert upload.data not in listed and '"portrait":' not in listed
    renamed = await catalog.update_agent(agent.id, AgentUpdate(expected_revision=1, name="新版"))
    assert renamed.portrait_sha256 == agent.portrait_sha256
    new_raw, replacement = portrait("blue")
    changed = await catalog.update_agent(agent.id, AgentUpdate(
        expected_revision=2, portrait=replacement))
    assert changed.revision == 3 and changed.portrait_sha256 != agent.portrait_sha256
    assert (await catalog.portrait_content(agent.id))["image_data"] == new_raw
    cleared = await catalog.update_agent(agent.id, AgentUpdate(expected_revision=3, portrait=None))
    assert cleared.portrait_sha256 is None
    with pytest.raises(AiError) as missing:
        await catalog.portrait_content(agent.id)
    assert missing.value.status_code == 404
    await catalog.update_agent(agent.id, AgentUpdate(expected_revision=4, portrait=upload))
    await catalog.delete_agent(agent.id, 5)
    rows = agent_database.connection.execute("SELECT * FROM agent_bot_agent_portrait").fetchall()
    assert rows == []


async def test_bad_image_and_stale_revision_preserve_role_and_portrait(
    agent_database, agent_params, seeded_model,
):
    """图片解码失败回滚先前写入的资料，旧版本上传不能覆盖新图。"""
    catalog = AgentCatalog(agent_params, connect=agent_database.connect)
    raw, upload = portrait()
    agent = await catalog.create_agent(AgentCreate(
        code="atomic", name="原名", model_id=seeded_model[2].id, portrait=upload))
    bad = PortraitUpload(mime_type="image/png", data=base64.b64encode(b"broken").decode())
    with pytest.raises(AiError):
        await catalog.update_agent(agent.id, AgentUpdate(
            expected_revision=1, name="不应保存", portrait=bad))
    assert await catalog.get_agent(agent.id) == agent
    assert (await catalog.portrait_content(agent.id))["image_data"] == raw
    await catalog.update_agent(agent.id, AgentUpdate(expected_revision=1, name="已保存"))
    with pytest.raises(AiError) as stale:
        await catalog.update_agent(agent.id, AgentUpdate(expected_revision=1, portrait=None))
    assert stale.value.status_code == 409
    assert (await catalog.portrait_content(agent.id))["image_data"] == raw
    with pytest.raises(AiError):
        await catalog.create_agent(AgentCreate(
            code="invalid", name="坏图", model_id=seeded_model[2].id, portrait=bad))
    assert len((await catalog.list_agents()).items) == 1


@pytest.mark.parametrize("mime,data", [
    ("image/png", "not base64!"),
    ("image/jpeg", None),
])
def test_invalid_image_rejected(mime, data):
    """非法编码及伪装容器均不能作为人物图保存。"""
    _, upload = portrait()
    with pytest.raises(AiError):
        PortraitUpload(mime_type=mime, data=data or upload.data).decode()



def test_oversized_and_animated_portraits_rejected():
    """应用字节限制和静态图限制均由后端执行，不能绕过浏览器校验。"""
    from agent_bot.agent.portraits import MAX_PORTRAIT_BYTES

    with pytest.raises(AiError) as oversized:
        PortraitUpload(mime_type="image/png", data=base64.b64encode(
            b"x" * (MAX_PORTRAIT_BYTES + 1)).decode()).decode()
    assert oversized.value.status_code == 413
    stream = BytesIO()
    first = Image.new("RGB", (3, 3), "red")
    second = Image.new("RGB", (3, 3), "blue")
    first.save(stream, format="PNG", save_all=True, append_images=[second], duration=100)
    with pytest.raises(AiError) as animated:
        PortraitUpload(mime_type="image/png", data=base64.b64encode(
            stream.getvalue()).decode()).decode()
    assert animated.value.code == "MEDIA_ANIMATION_UNSUPPORTED"
