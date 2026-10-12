"""媒体输入必须在能力和入口权限预检后准备，租约覆盖整个 Agent 执行。"""

from unittest.mock import AsyncMock

import pytest

from agent.test_service import service_for
from agent_bot.agent.contracts import AgentRequest, ExecutionAuthority
from agent_bot.agent.errors import AgentError
from agent_bot.media.inputs import ImagePreparer
from media.test_inputs import picture
from media.test_store import OWNER, storage


async def test_capability_guard_precedes_download(seeded_model, agent_database, agent_params):
    """没有声明识图能力时，不请求远端 CDN。"""
    service = service_for(agent_database, agent_params)
    service.image_downloader = AsyncMock()
    request = AgentRequest(
        model="local-model",
        prompt="看图",
        session_id="s",
        request_id="r",
        image_urls=("https://qpic.cn/a",),
        max_steps=1,
    )
    with pytest.raises(AgentError, match="MEDIA_VISION_UNSUPPORTED"):
        await service.complete(request, ExecutionAuthority(entry="qq", allowed_tools=("lookup",)))
    service.image_downloader.fetch.assert_not_called()
    assert service.active == 0


async def test_images_use_private_budget_and_only_current_turn(
    media_model, media_database, agent_params
):
    """模型图像不会挤占文本上下文预算，下一条无图请求不继承图片。"""
    service = service_for(media_database, agent_params)
    image = ImagePreparer().prepare(picture(), None)
    # 较大副本用于验证预算分离，不需要真实分配百万像素。
    image = image.model_copy(
        update={"model_image": image.model_image.model_copy(update={"data": b"x" * 150000})}
    )
    service.image_downloader = AsyncMock()
    service.image_downloader.fetch.return_value = image
    request = AgentRequest(
        model="local-model",
        prompt="",
        session_id="s",
        request_id="r",
        image_urls=("https://qpic.cn/a",),
        max_steps=1,
    )
    result = await service.complete(
        request, ExecutionAuthority(entry="qq", allowed_tools=("lookup",))
    )
    assert result.content == "最终回答"
    calls = service.providers["openai-chat"].calls
    assert len(calls) == 2
    assert all(sum(len(m.images) for m in call.messages) == 1 for call in calls)
    plain = request.model_copy(update={"image_urls": (), "prompt": "hello"})
    await service.complete(plain, ExecutionAuthority(entry="qq", allowed_tools=("lookup",)))
    assert not any(m.images for m in calls[-1].messages)


async def test_admin_input_binding_and_source_authority(
    media_model, media_database, agent_params, tmp_path, media_clock
):
    """管理端只能引用本会话上传；执行结束后所有文件租约归还。"""
    async with storage(media_database, agent_params, tmp_path, media_clock) as store:
        asset = await store.put(owner=OWNER, purpose="input", data=picture(), mime_type="image/png")
        service = service_for(media_database, agent_params)
        service.media_store = store
        request = AgentRequest(
            model="local-model",
            prompt="",
            session_id=OWNER.session_id,
            request_id="r",
            attachment_ids=(asset.id,),
            max_steps=1,
        )
        result = await service.complete(
            request, ExecutionAuthority(entry="admin", allowed_tools=("lookup",))
        )
        assert result.content == "最终回答" and not store._leases
        with pytest.raises(AgentError, match="MEDIA_INPUT_UNAUTHORIZED"):
            await service.complete(
                request, ExecutionAuthority(entry="qq", allowed_tools=("lookup",))
            )
