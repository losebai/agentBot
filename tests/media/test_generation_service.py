"""共享媒体用例的并发、记录、取消和不可重试副作用边界。"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from agent_bot.ai.credentials import CredentialResolver
from agent_bot.materials.contracts import MaterialSearchArguments
from agent_bot.media.config import MediaConfig
from agent_bot.media.contracts import MediaOwner
from agent_bot.media.errors import MediaError
from media.test_generation_protocols import media_request
from media.test_inputs import picture
from media.test_validation import response


async def build(media_model, media_database, agent_params, tmp_path):
    """使用真实存储、目录和运行记录，仅替换供应商网络边界。"""
    from agent_bot.media.resources import MediaResources
    from agent_bot.media.service import MediaGenerationService

    resources = MediaResources(
        MediaConfig(directory=tmp_path), agent_params, connect=media_database.connect
    )
    await resources.start()
    provider = AsyncMock()
    provider.generate.return_value = response(picture())
    service = MediaGenerationService(
        media_model[0], CredentialResolver(), provider, resources
    )
    request = media_request().model_copy(update={"model": media_model[2].alias})
    return service, resources, provider, request


async def test_generation_publishes_asset_and_record(
    media_model, media_database, agent_params, tmp_path
):
    """公开响应不包含 QQ 私有 Base64，运行记录能关联生成附件。"""
    service, resources, provider, request = await build(
        media_model, media_database, agent_params, tmp_path
    )
    try:
        result = await service.generate(request, MediaOwner(entry="admin", session_id="s"))
        assert result.record_saved and result.attachments[0].status == "ready"
        assert result.response.items and "response" not in result.model_dump()
        assert (await resources.records.get_run(result.execution_id)).status == "completed"
        records = (await resources.materials.search(
            MediaOwner(entry="admin", session_id="s"), MaterialSearchArguments(),
        )).records
        assert len(records) == 1 and records[0].kind == "image"
        assert records[0].categories == ("generated",)
        async with resources.materials.open_content(
            records[0].id, owner=MediaOwner(entry="admin", session_id="s"),
        ) as image:
            assert image.read() == picture()
        provider.generate.assert_awaited_once()
    finally:
        await resources.aclose()


async def test_record_failure_keeps_file_but_save_failure_returns_error(
    media_model, media_database, agent_params, tmp_path
):
    """记录可尽力保存，文件元数据失败则明确报告，均不重调模型。"""
    service, resources, provider, request = await build(
        media_model, media_database, agent_params, tmp_path
    )
    resources.records.finish = AsyncMock(side_effect=MediaError("AI_DATABASE_UNAVAILABLE"))
    try:
        result = await service.generate(request, MediaOwner(entry="admin", session_id="s"))
        assert not result.record_saved and result.attachments[0].status == "ready"
        resources.store.put = AsyncMock(side_effect=OSError("offline disk unavailable"))
        with pytest.raises(MediaError, match="MEDIA_SAVE_FAILED"):
            await service.generate(request, MediaOwner(entry="admin", session_id="s"))
        assert provider.generate.await_count == 2
    finally:
        await resources.aclose()


async def test_material_save_failure_returns_error_without_regenerating(
    media_model, media_database, agent_params, tmp_path,
):
    """生成原件保存失败必须显式失败，不能把临时附件成功当作完整成功或重调模型。"""
    service, resources, provider, request = await build(
        media_model, media_database, agent_params, tmp_path,
    )
    resources.materials.put = AsyncMock(side_effect=OSError("material disk unavailable"))
    try:
        with pytest.raises(MediaError, match="MEDIA_SAVE_FAILED"):
            await service.generate(request, MediaOwner(entry="admin", session_id="s"))
        runs = await resources.records.list_runs()
        assert len(runs.items) == 1 and runs.items[0].status == "failed"
        provider.generate.assert_awaited_once()
    finally:
        await resources.aclose()


async def test_third_generation_busy_and_cancel_closes_record(
    media_model, media_database, agent_params, tmp_path
):
    """名额覆盖上游等待，取消后运行记录收束且容量全部归还。"""
    service, resources, provider, request = await build(
        media_model, media_database, agent_params, tmp_path
    )
    ready = asyncio.Event()
    count = 0

    async def wait(*args):
        """模拟两个已经开始且尚未完成的计费调用。"""
        nonlocal count
        count += 1
        if count == 2:
            ready.set()
        await asyncio.Event().wait()

    provider.generate.side_effect = wait
    owner = MediaOwner(entry="admin", session_id="s")
    tasks = [asyncio.create_task(service.generate(request, owner)) for _ in range(2)]
    try:
        await asyncio.wait_for(ready.wait(), 3)
        with pytest.raises(MediaError) as busy:
            await service.generate(request, owner)
        assert busy.value.status_code == 429
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert resources.generations.active == 0
        assert all(run.status == "cancelled" for run in (await resources.records.list_runs()).items)
        await resources.aclose()
