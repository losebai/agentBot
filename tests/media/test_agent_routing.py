"""通过真实目录、执行图和受控模型验证路由与错误接管，不连接外部服务。"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from agent.test_engine import Provider
from agent.test_service import service_for
from agent_bot.agent.contracts import (
    AgentCreate,
    AgentRequest,
    AgentUpdate,
    ExecutionAuthority,
    ModelEvent,
    ModelTurn,
    ToolCall,
)
from agent_bot.ai.contracts import ModelCreate, ModelUpdate, ProviderCreate
from agent_bot.ai.errors import AiError
from agent_bot.media.inputs import ImagePreparer
from media.test_inputs import picture


async def routed_service(media_database, agent_params, media_model, *, fallback=True):
    """聊天与识图模型分离，备用模型独立，工具仍使用真实图调度。"""
    ai, provider, vision = media_model
    chat = await ai.create_model(
        ModelCreate(
            alias="chat",
            upstream_model="chat-upstream",
            provider_id=provider.id,
            enabled=True,
        )
    )
    backup = await ai.create_model(
        ModelCreate(
            alias="backup",
            upstream_model="backup-upstream",
            provider_id=provider.id,
            enabled=True,
            media=vision.media,
        )
    )
    service = service_for(media_database, agent_params)
    agent = await service.catalog.create_agent(
        AgentCreate(
            name="路由助手",
            code="routed",
            enabled=True,
            model_id=chat.id,
            vision_model_id=vision.id,
            fallback_model_id=backup.id if fallback else None,
            tools=("lookup",),
            max_steps=1,
        )
    )
    service.image_downloader = AsyncMock()
    service.image_downloader.fetch.return_value = ImagePreparer().prepare(picture(), None)
    return service, agent, chat, vision, backup


def request(*, images=False):
    """同一 Agent 根据本次输入选路，不从历史文本猜测图片。"""
    return AgentRequest(
        agent_code="routed",
        prompt="请回答",
        session_id="s",
        request_id="r",
        image_urls=("https://qpic.cn/a",) if images else (),
    )


async def test_current_material_image_uses_vision_and_preserves_ownership(
    media_database, agent_params, media_model, tmp_path,
):
    """本次收到/引用的素材 ID 读取本地原件，不再请求 QQ 的临时链接。"""
    from agent_bot.materials.store import MaterialStore
    from agent_bot.media.contracts import MediaOwner
    from agent_bot.media.files import MediaFiles

    service, _, _, vision, _ = await routed_service(media_database, agent_params, media_model)
    materials = MaterialStore(agent_params, connect=media_database.connect,
                              files=MediaFiles(tmp_path / "materials"))
    await materials.start()
    try:
        asset = await materials.put(owner=MediaOwner(entry="qq", session_id="s"), name="cat.png",
                                    mime_type="image/png", data=picture())
        service.material_store = materials
        call = request().model_copy(update={"image_material_ids": (asset.id,)})
        await service.complete(call, ExecutionAuthority(entry="qq", allowed_tools=("lookup",)))
        calls = service.providers["openai-chat"].calls
        assert calls[0].resolved.model.id == vision.id
        assert calls[0].messages[-1].images
        service.image_downloader.fetch.assert_not_awaited()
    finally:
        await materials.aclose()


async def test_routes_and_clear_preserve_chat_binding(media_database, agent_params, media_model):
    """遗漏按图片选路或清空语义会使带图请求继续落入错误模型。"""
    service, agent, chat, vision, backup = await routed_service(
        media_database,
        agent_params,
        media_model,
    )
    plain = await service.catalog.snapshot(request())
    image = await service.catalog.snapshot(request(images=True))
    assert plain.resolved.model.id == chat.id
    assert image.resolved.model.id == vision.id
    assert image.fallback.model.id == backup.id
    changed = await service.catalog.update_agent(
        agent.id,
        AgentUpdate(
            expected_revision=1,
            vision_model_id=None,
            fallback_model_id=None,
        ),
    )
    assert changed.model_id == chat.id and changed.vision_model_id is None
    assert changed.fallback_model_id is None and changed.revision == 2
    with pytest.raises(AiError, match="AGENT_VISION_MODEL_REQUIRED"):
        await service.catalog.snapshot(request(images=True))


@pytest.mark.parametrize("vision_disabled", [False, True])
async def test_mcp_screenshot_keeps_chat_model_without_repeating_tool(
    media_database, agent_params, media_model, vision_disabled,
):
    """没有本次直接或引用图片时，截图工具不能触发识图模型或提交图像字节。"""
    from agent.test_engine import Tools
    from agent_bot.agent.contracts import ToolResult

    class ScreenshotTools(Tools):
        """仅模拟工具截图，不访问桌面或付费模型。"""

        async def call(self, call):
            """沿用真实图片校验，结果图像只留在本次模型上下文。"""
            self.invoked.append(call.call_id)
            return ToolResult(
                call_id=call.call_id, name=call.name, content="截图完成",
                images=(ImagePreparer().prepare(picture(), None).model_image,),
            )

    service, _, chat, vision, _backup = await routed_service(
        media_database, agent_params, media_model,
    )
    if vision_disabled:
        await media_model[0].update_model(
            vision.id, ModelUpdate(expected_revision=vision.revision, enabled=False),
        )
    tools = ScreenshotTools()
    service.tool_runtime = tools
    snapshot = await service.catalog.snapshot(request())
    assert snapshot.vision.model.id == vision.id
    result = await service.complete(
        request(), ExecutionAuthority(entry="admin", allowed_tools=("lookup",)),
    )
    calls = service.providers["openai-chat"].calls
    assert [call.resolved.model.id for call in calls] == [
        chat.id, chat.id,
    ]
    assert result.content == "最终回答" and len(tools.invoked) == 1
    assert not calls[1].messages[-1].tool_result.images
    assert calls[1].messages[-1].tool_result.content == "截图完成"
    steps = await service.records.list_steps(result.execution_id)
    assert "images" not in str([step.model_dump() for step in steps.items])


async def test_vision_route_requires_declared_capability(
    media_database,
    agent_params,
    media_model,
):
    """管理保存时就拒绝无识图能力的专用图片模型，事务不产生部分修改。"""
    service, agent, chat, _, _ = await routed_service(media_database, agent_params, media_model)
    with pytest.raises(AiError, match="MEDIA_VISION_UNSUPPORTED"):
        await service.catalog.update_agent(
            agent.id,
            AgentUpdate(
                expected_revision=1,
                vision_model_id=chat.id,
            ),
        )
    assert (await service.catalog.get_agent(agent.id)).revision == 1


async def test_backup_reference_blocks_model_deletion(media_database, agent_params, media_model):
    """备用引用也受约束，避免目录删除使已保存的转发配置失效。"""
    _, _, _, _, backup = await routed_service(media_database, agent_params, media_model)
    ai = media_model[0]
    changed = await ai.update_model(backup.id, ModelUpdate(expected_revision=1, enabled=False))
    with pytest.raises(AiError, match="AI_CONFIG_IN_USE"):
        await ai.delete_model(backup.id, changed.revision)


@pytest.mark.parametrize("shared", ["chat", "vision", "all"])
async def test_model_roles_can_share_models_without_self_fallback(
    media_database, agent_params, media_model, shared,
):
    """模型角色可复用；只按本次实际选中模型跳过自我兜底，创建与更新规则一致。"""
    service, agent, chat, vision, _ = await routed_service(
        media_database,
        agent_params,
        media_model,
    )
    primary = vision if shared == "all" else chat
    fallback = chat if shared == "chat" else vision
    values = dict(model_id=primary.id, vision_model_id=vision.id, fallback_model_id=fallback.id)
    changed = await service.catalog.update_agent(
        agent.id, AgentUpdate(expected_revision=1, **values),
    )
    assert changed.revision == 2 and changed.fallback_model_id == fallback.id
    created = await service.catalog.create_agent(AgentCreate(
        code="shared", name="共享模型助手", enabled=True, **values,
    ))
    for code in (agent.code, created.code):
        for images in (False, True):
            call = request(images=images).model_copy(update={"agent_code": code})
            snapshot = await service.catalog.snapshot(call)
            selected = vision if images else primary
            assert snapshot.resolved.model.id == selected.id
            if selected.id == fallback.id:
                assert snapshot.fallback is None
            else:
                assert snapshot.fallback.model.id == fallback.id


class FailingProvider(Provider):
    """只替换外部模型端口，故障发生在工具已经完成后的收尾轮。"""

    def __init__(self, *, error="AI_UPSTREAM_UNAVAILABLE", backup_error=False):
        """可控错误让测试检查一次接管和准确的最终错误。"""
        super().__init__()
        self.error, self.backup_error = error, backup_error

    async def stream_turn(self, call):
        """主模型先完成工具请求，收尾失败后备用继承工具结果。"""
        alias = call.resolved.model.alias
        if call.final_only and (alias != "backup" or self.backup_error):
            self.calls.append(call)
            yield ModelEvent(kind="text_delta", delta="失败草稿")
            raise AiError(self.error)
        async for event in super().stream_turn(call):
            yield event


async def test_qq_timeout_with_body_sends_existing_text_before_fallback(
    media_database, agent_params, media_model,
):
    """已有正文超时即交给 QQ 投递，不切换备用模型丢弃本轮生成。"""
    from agent_bot.ai.routing import RoutingAiClient
    from agent_bot.models import LlmRequestOptions

    class PartialTimeoutProvider(Provider):
        """主模型输出后超时，备用调用会明显暴露重复生成。"""

        async def stream_turn(self, call):
            """记录实际绑定，输出同一片段后返回稳定超时码。"""
            self.calls.append(call)
            yield ModelEvent(kind="text_delta", delta="已生成的汇编代码")
            raise AiError("AI_TIMEOUT", status_code=504)

    service, _, _, _, _ = await routed_service(media_database, agent_params, media_model)
    provider = PartialTimeoutProvider()
    service.providers["openai-chat"] = provider
    client = RoutingAiClient(
        AsyncMock(), agent_service=service, agent_code="routed", timeout_seconds=30,
        default_options=LlmRequestOptions(), mcp_services=(),
    )
    assert await client.complete(
        model="chat", session_id="s", prompt="写代码", messages=(), request_id="r",
        use_web_search=False,
    ) == "已生成的汇编代码"
    assert len(provider.calls) == 1
    assert (await service.records.list_runs()).items[0].status == "failed"


async def test_forward_keeps_completed_tools_and_resets_failed_text(
    media_database,
    agent_params,
    media_model,
):
    """重跑工作流会重复工具；只替换模型轮次，结果和记录不包含失败草稿。"""
    service, _, _, _, _ = await routed_service(media_database, agent_params, media_model)
    provider = FailingProvider()
    service.providers["openai-chat"] = provider
    result = await service.complete(
        request(), ExecutionAuthority(entry="qq", allowed_tools=("lookup",))
    )
    assert result.content == "最终回答" and service.tool_runtime.invoked == ["c1"]
    assert [call.resolved.model.alias for call in provider.calls] == ["chat", "chat", "backup"]
    assert provider.calls[-1].messages[-1].tool_result.content == "tool facts"
    assert all(not message.continuation for message in provider.calls[-1].messages)
    run = await service.records.get_run(result.execution_id)
    assert run.status == "completed" and run.content == "最终回答"
    steps = (await service.records.list_steps(result.execution_id)).items
    assert any(
        step.kind == "warning" and "AI_UPSTREAM_UNAVAILABLE" in step.summary for step in steps
    )
    assert service.active == 0 and service.tool_runtime.active == 0


@pytest.mark.parametrize("error", ["AGENT_TOOL_UNAUTHORIZED", "AGENT_TIMEOUT"])
async def test_authority_and_total_timeout_do_not_forward(
    media_database,
    agent_params,
    media_model,
    error,
):
    """仅接管模型故障；权限和总期限不能通过换模型规避。"""
    service, _, _, _, _ = await routed_service(media_database, agent_params, media_model)
    provider = FailingProvider(error=error)
    service.providers["openai-chat"] = provider
    with pytest.raises(AiError, match=error):
        await service.complete(request(), ExecutionAuthority(entry="qq", allowed_tools=("lookup",)))
    assert [call.resolved.model.alias for call in provider.calls] == ["chat", "chat"]


async def test_backup_failure_is_terminal(media_database, agent_params, media_model):
    """备用再次失败必须结束，不能在两个模型间无限往返。"""
    service, _, _, _, _ = await routed_service(media_database, agent_params, media_model)
    provider = FailingProvider(backup_error=True)
    service.providers["openai-chat"] = provider
    with pytest.raises(AiError, match="AI_UPSTREAM_UNAVAILABLE"):
        await service.complete(request(), ExecutionAuthority(entry="qq", allowed_tools=("lookup",)))
    assert [call.resolved.model.alias for call in provider.calls] == ["chat", "chat", "backup"]
    assert service.tool_runtime.invoked == ["c1"]


async def test_disabled_backup_does_not_block_primary(media_database, agent_params, media_model):
    """预检备用不应让正常主模型受停用备用影响。"""
    service, _, _, _, backup = await routed_service(media_database, agent_params, media_model)
    await media_model[0].update_model(backup.id, ModelUpdate(expected_revision=1, enabled=False))
    result = await service.complete(
        request(), ExecutionAuthority(entry="qq", allowed_tools=("lookup",))
    )
    assert result.content == "最终回答"


async def test_cancel_does_not_forward(media_database, agent_params, media_model):
    """取消模型等待后退出所有资源，不进入备用模型。"""
    service, _, _, _, _ = await routed_service(media_database, agent_params, media_model)
    started = asyncio.Event()

    class WaitingProvider(Provider):
        """阻塞真实图的模型端口，让调用方触发取消。"""

        async def stream_turn(self, call):
            """记下使用的模型后等待取消。"""
            self.calls.append(call)
            started.set()
            await asyncio.Event().wait()
            yield

    provider = WaitingProvider()
    service.providers["openai-chat"] = provider
    task = asyncio.create_task(service.complete(request(), ExecutionAuthority(entry="qq")))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [call.resolved.model.alias for call in provider.calls] == ["chat"]
    assert service.active == 0 and service.tool_runtime.active == 0


async def test_qq_images_reach_only_vision_model(media_database, agent_params, media_model):
    """仅测快照会漏掉调用仍使用旧聊天绑定的错误，直接执行 QQ 图片请求。"""
    service, _, _, _, _ = await routed_service(media_database, agent_params, media_model)
    result = await service.complete(
        request(images=True),
        ExecutionAuthority(entry="qq", allowed_tools=("lookup",)),
    )
    assert result.content == "最终回答"
    calls = service.providers["openai-chat"].calls
    assert [call.resolved.model.alias for call in calls] == ["local-model", "local-model"]
    assert all(sum(len(message.images) for message in call.messages) == 1 for call in calls)


async def test_missing_vision_route_is_rejected_before_download(
    media_database,
    agent_params,
    media_model,
):
    """没有专用图片路由时明确拒绝，不能借备用模型绕过缺失配置。"""
    service, agent, _, _, _ = await routed_service(media_database, agent_params, media_model)
    await service.catalog.update_agent(
        agent.id, AgentUpdate(expected_revision=1, vision_model_id=None)
    )
    with pytest.raises(AiError, match="AGENT_VISION_MODEL_REQUIRED"):
        await service.complete(
            request(images=True), ExecutionAuthority(entry="qq", allowed_tools=("lookup",)),
        )
    assert service.providers["openai-chat"].calls == []
    service.image_downloader.fetch.assert_not_awaited()


async def test_vision_capable_chat_model_does_not_replace_missing_image_route(
    media_database, agent_params, media_model,
):
    """聊天模型即使支持视觉，也不能隐式代替未配置的多模态路由。"""
    service, agent, _, vision, _ = await routed_service(media_database, agent_params, media_model)
    await service.catalog.update_agent(
        agent.id, AgentUpdate(expected_revision=1, model_id=vision.id, vision_model_id=None),
    )
    with pytest.raises(AiError, match="AGENT_VISION_MODEL_REQUIRED"):
        await service.catalog.snapshot(request(images=True))


async def test_no_backup_retains_original_error(media_database, agent_params, media_model):
    """空备用不得隐式选其他模型，即使模型目录中有可用模型。"""
    service, _, _, _, _ = await routed_service(
        media_database, agent_params, media_model, fallback=False
    )
    provider = FailingProvider()
    service.providers["openai-chat"] = provider
    with pytest.raises(AiError, match="AI_UPSTREAM_UNAVAILABLE"):
        await service.complete(request(), ExecutionAuthority(entry="qq", allowed_tools=("lookup",)))
    assert [call.resolved.model.alias for call in provider.calls] == ["chat", "chat"]


async def test_forward_strips_private_continuation(media_database, agent_params, media_model):
    """不同供应商的推理延续不得泄露给备用，但公共工具调用仍需完整保留。"""
    service, _, _, _, _ = await routed_service(media_database, agent_params, media_model)

    class PrivateProvider(FailingProvider):
        """模拟 Responses 私有延续，只允许其返回原模型。"""

        async def stream_turn(self, call):
            """第一轮携私有状态，后续复用正常模型故障路径。"""
            if not call.final_only:
                self.calls.append(call)
                yield ModelEvent(
                    kind="turn_end",
                    turn=ModelTurn(
                        content="",
                        model="chat-upstream",
                        calls=(ToolCall(call_id="c1", name="lookup", arguments={}),),
                        continuation=({"secret_thinking": "private"},),
                    ),
                )
            else:
                async for event in super().stream_turn(call):
                    yield event

    provider = PrivateProvider()
    service.providers["openai-chat"] = provider
    await service.complete(request(), ExecutionAuthority(entry="qq", allowed_tools=("lookup",)))
    assert any(message.continuation for message in provider.calls[1].messages)
    assert all(not message.continuation for message in provider.calls[2].messages)
    assert provider.calls[2].messages[-2].tool_calls[0].call_id == "c1"


async def test_new_request_starts_with_primary_again(media_database, agent_params, media_model):
    """接管额度和当前模型只属于单次请求，不能污染下次聊天。"""
    service, _, _, _, _ = await routed_service(media_database, agent_params, media_model)
    provider = FailingProvider()
    service.providers["openai-chat"] = provider
    for _ in range(2):
        await service.complete(request(), ExecutionAuthority(entry="qq", allowed_tools=("lookup",)))
    assert [call.resolved.model.alias for call in provider.calls] == [
        "chat",
        "chat",
        "backup",
        "chat",
        "chat",
        "backup",
    ]


async def test_backup_preserves_its_own_later_continuation(
    media_database,
    agent_params,
    media_model,
):
    """接管仅去除原模型延续；备用生成的延续必须保留给其后续工具轮次。"""
    service, _, _, _, _ = await routed_service(media_database, agent_params, media_model)

    class BackupThinking(Provider):
        """首个主模型调用失败，备用先思考调用工具再收尾。"""

        async def stream_turn(self, call):
            """备用的私有标记不会被其他模型消费。"""
            if call.resolved.model.alias == "chat":
                self.calls.append(call)
                raise AiError("AI_UPSTREAM_UNAVAILABLE")
            if not call.final_only:
                self.calls.append(call)
                yield ModelEvent(
                    kind="turn_end",
                    turn=ModelTurn(
                        model="backup-upstream",
                        content="",
                        calls=(ToolCall(call_id="backup-call", name="lookup", arguments={}),),
                        continuation=({"backup_thinking": "owned"},),
                    ),
                )
            else:
                async for event in super().stream_turn(call):
                    yield event

    provider = BackupThinking()
    service.providers["openai-chat"] = provider
    await service.complete(request(), ExecutionAuthority(entry="qq", allowed_tools=("lookup",)))
    assert provider.calls[-1].messages[-2].continuation == ({"backup_thinking": "owned"},)


async def test_forward_uses_backup_provider_protocol(media_database, agent_params, media_model):
    """转发到其他供应商时必须同步切换协议端口，不只替换模型名称。"""
    service, _, _, _, backup = await routed_service(media_database, agent_params, media_model)
    ai = media_model[0]
    provider = await ai.create_provider(
        ProviderCreate(
            name="备用供应商",
            protocol="openai-responses",
            base_url="https://backup.invalid/v1",
            enabled=True,
        )
    )
    await ai.update_model(backup.id, ModelUpdate(expected_revision=1, provider_id=provider.id))
    primary, secondary = FailingProvider(), Provider()
    service.providers.update({"openai-chat": primary, "openai-responses": secondary})
    result = await service.complete(
        request(), ExecutionAuthority(entry="qq", allowed_tools=("lookup",))
    )
    assert result.content == "最终回答"
    assert [call.resolved.model.alias for call in primary.calls] == ["chat", "chat"]
    assert [call.resolved.model.alias for call in secondary.calls] == ["backup"]
    assert secondary.calls[0].resolved.provider.base_url == "https://backup.invalid/v1"
    assert service.tool_runtime.invoked == ["c1"]
