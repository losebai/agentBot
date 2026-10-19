"""直接生图工具在 QQ 与管理 Agent 中的授权、队列和附件回归。"""

import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from agent_bot.agent.contracts import (
    AgentRequest,
    ExecutionAuthority,
    ModelEvent,
    ModelTurn,
    ToolCall,
)
from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.media import AdminMediaTools
from agent_bot.config import RuntimeConfig
from agent_bot.integrations.onebot.queue_processor import ConversationQueueProcessor
from agent_bot.integrations.onebot.tooling import ChatToolSessionManager
from agent_bot.integrations.sqlite_queue_database import initialize_queue_database
from agent_bot.integrations.sqlite_tool_operations import SqliteToolOperationStore
from agent_bot.integrations.tool_operations import RedisToolOperationStore
from agent_bot.services.chat_tools import ChatToolContext, ChatToolError, build_chat_tool_registry
from agent_bot.services.conversation_queue import ConversationJob
from agent_bot.services.tool_operations import ImageTaskEffect
from agent_bot.services.workflow_execution import WorkflowImageCollector
from media.test_admin_tools import adapters, workflow_call
from media.test_agent_artifacts import build_agent
from media.test_generation_service import build


def runtime_config(**values):
    """测试配置使用独立凭据，禁止加载真实启动文件。"""
    return RuntimeConfig(
        onebot_v11_access_token="x" * 40, chat_session_secret="y" * 40, **values,
    )


def image_call(**arguments):
    """模型只传图片描述及开画文案，服务地址和模型由管理员配置。"""
    return ToolCall(
        call_id="image-1", name="image_generate",
        arguments={"prompt": "一只猫", "start_reply": "我来画一只猫。", **arguments},
    )


async def test_qq_direct_image_tool_queues_once_and_restores_receipt():
    """直接生图无需 Comfy，恢复已提交任务时不能再次产生生成副作用。"""
    collector = WorkflowImageCollector()
    submit = AsyncMock(return_value="image-task-1")
    collector.submit_image = submit
    operations = SimpleNamespace(begin=AsyncMock(return_value=None), complete=AsyncMock())
    context = ChatToolContext(
        session_id="qq-s", user_id="12345", allowed_tools=frozenset({"image_generate"}),
        workflow_collector=collector, operation_store=operations, job_id="parent",
    )
    registry = build_chat_tool_registry()
    call = image_call()
    result = await registry.call(call.name, call.arguments, context)
    assert json.loads(result.content) == {"status": "queued", "task_id": "image-task-1"}
    submit.assert_awaited_once_with("一只猫", "我来画一只猫。")
    assert collector.queued_task_id == "image-task-1"
    operations.begin.return_value = ImageTaskEffect(task_id="image-task-1")
    await registry.call(call.name, call.arguments, context)
    submit.assert_awaited_once()
    with pytest.raises(ChatToolError, match="当前请求未授权"):
        await registry.call(call.name, call.arguments, ChatToolContext(
            session_id="qq-s", user_id="12345", allowed_tools=frozenset(),
        ))


@pytest.mark.parametrize("enabled,mcp,queue", [
    (True, True, True), (False, True, True), (True, False, True), (True, True, False),
])
def test_qq_direct_image_availability_requires_enabled_queue(enabled, mcp, queue):
    """仅在生图开关、聊天工具和持久队列可用时提供直接生图能力。"""
    config = runtime_config(chat_image_generation_enabled=enabled, chat_mcp_enabled=mcp)
    manager = ChatToolSessionManager(
        config_provider=lambda: config, store_provider=lambda: object(),
        naraka_service_provider=lambda: None,
        group_member_refresher=lambda _bot, _group: None,
        image_tasks_provider=lambda: object() if queue else None,
    )
    assert ("image_generate" in manager.available_group_tools()) == (enabled and mcp and queue)


class DirectImageProvider:
    """聊天模型只请求直接生图工具，不访问真实供应商。"""

    def __init__(self):
        """记录轮次，生成后只返回完成文字。"""
        self.calls = 0

    def validate_options(self, options):
        """本地替身接受默认模型参数。"""

    async def stream_turn(self, call):
        """首轮调用生图，第二轮保留原聊天模型收尾。"""
        self.calls += 1
        if self.calls == 1:
            assert "image_generate" in {tool.name for tool in call.tools}
            yield ModelEvent(kind="turn_end", turn=ModelTurn(model="chat", calls=(image_call(),)))
        else:
            yield ModelEvent(kind="text_delta", delta="图片已生成")
            yield ModelEvent(kind="turn_end", turn=ModelTurn(model="chat", content="图片已生成"))


async def test_admin_agent_direct_image_uses_configured_model_and_binds_attachment(
    media_model, media_database, agent_params, tmp_path,
):
    """管理对话不依赖 Comfy，生成附件必须关联当前 Agent 执行及运行详情。"""
    generation, resources, provider, _ = await build(
        media_model, media_database, agent_params, tmp_path,
    )
    config = runtime_config(
        chat_image_generation_enabled=True, chat_image_model=media_model[2].alias,
    )
    adapter = AdminMediaTools(
        config, None, None, resources.store, resources=resources, image_service=generation,
    )
    service = build_agent(
        media_database, agent_params, resources.store, adapter, DirectImageProvider(),
    )
    try:
        result = await service.complete(
            AgentRequest(model=media_model[2].alias, session_id="s", prompt="画猫",
                         request_id="direct-image", tools=("image_generate",)),
            ExecutionAuthority(entry="admin", allowed_tools=("image_generate",)),
        )
        assert result.attachments[0].status == "ready"
        request = provider.generate.await_args.args[0]
        assert request.model == config.chat_image_model and request.prompt == "一只猫"
        detail = await service.records.get_run(result.execution_id)
        assert detail.attachments == result.attachments
    finally:
        await resources.aclose()


async def test_admin_direct_image_disabled_is_not_exposed():
    """关闭生图后不能仅凭 Agent 预设工具名绕过运行开关。"""
    adapter = AdminMediaTools(
        runtime_config(chat_image_generation_enabled=False), None, None, None,
        image_service=AsyncMock(),
    )
    assert "image_generate" not in adapter.allowed_tools


@pytest.mark.parametrize("direct_first", [True, False])
async def test_admin_image_tools_share_quota_and_cannot_override_model(
    media_model, media_database, agent_params, tmp_path, direct_first,
):
    """Comfy 和直接生图共享一次额度，模型指定身份、地址或模型均在副作用前拒绝。"""
    from media.test_store import SCOPE

    generation, resources, provider, _ = await build(
        media_model, media_database, agent_params, tmp_path / "media",
    )
    try:
        adapter, workflow, _, _ = adapters(resources.store, tmp_path)
        adapter.config = runtime_config(
            chat_image_generation_enabled=True, chat_image_model=media_model[2].alias,
            chat_local_workflow_enabled=True, chat_mcp_enabled=True,
        )
        adapter.image_service = generation
        session = adapter.bind(SCOPE, ())
        with pytest.raises(AgentError, match="AGENT_TOOL_ARGUMENTS_INVALID"):
            await session.call(image_call(model="untrusted", user="other"))
        provider.generate.assert_not_called()
        first, second = (image_call(), workflow_call()) if direct_first else (
            workflow_call(), image_call(),
        )
        await session.call(first)
        with pytest.raises(AgentError, match="AGENT_TOOL_LIMIT"):
            await session.call(second)
        assert provider.generate.await_count + workflow.generate.await_count == 1
    finally:
        await resources.aclose()


@pytest.mark.parametrize("backend", ["sqlite", "redis"])
async def test_direct_image_receipt_is_persistent_and_rejects_wrong_effect(backend, tmp_path):
    """两种队列均保存直接生图回执，类型错误不能污染后续恢复。"""
    from agent_bot.services.tool_operations import ReplyEffect

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    if backend == "sqlite":
        path = str(tmp_path / "queue.sqlite3")
        initialize_queue_database(path)

    def factory():
        """重建存储对象以验证回执不依赖原实例内存。"""
        if backend == "sqlite":
            return SqliteToolOperationStore(path, key_prefix="test:image", ttl_seconds=60)
        return RedisToolOperationStore(redis, key_prefix="test:image", ttl_seconds=60)
    try:
        store = factory()
        assert await store.begin("job", "image_generate", "a" * 64) is None
        with pytest.raises(ValueError, match="result type"):
            await store.complete("job", "image_generate", "a" * 64, ReplyEffect(text="不是回执"))
        effect = ImageTaskEffect(task_id="image-task")
        await store.complete("job", "image_generate", "a" * 64, effect)
        assert await factory().completed("job", "image_generate") == effect
    finally:
        await redis.aclose()


@pytest.mark.parametrize("delivery_fails", [False, True])
async def test_qq_agent_direct_tool_submits_prompt_after_notice(delivery_fails):
    """生产队列适配器将工具描述固化到子任务，通知失败不得提交图片生成。"""
    config = runtime_config(chat_image_generation_enabled=True)
    collector = WorkflowImageCollector()
    operations = SimpleNamespace(begin=AsyncMock(return_value=None), complete=AsyncMock())
    context = ChatToolContext(
        session_id="s", user_id="12345", allowed_tools=frozenset({"image_generate"}),
        workflow_collector=collector, operation_store=operations, job_id="parent",
    )

    @asynccontextmanager
    async def lease(*args):
        """只替换进程租约，不改变提交器、工具和任务校验。"""
        yield SimpleNamespace(tools=("image_generate",), token="offline", collector=collector)

    async def generate(*args, **kwargs):
        """模拟模型要求直接生图，执行生产工具后再返回文本。"""
        await build_chat_tool_registry().call("image_generate", image_call().arguments, context)
        return "已排队"

    gateway = SimpleNamespace(send_bot_exact_part=AsyncMock(
        side_effect=ConnectionError("offline notice failed") if delivery_fails else None,
    ))
    submit = AsyncMock(return_value="queue-entry")
    processor = ConversationQueueProcessor(
        config_provider=lambda: config,
        chat_service_provider=lambda: SimpleNamespace(generate_queued=generate),
        runtime_lease=lease, tool_session_provider=lease, bot_provider=lambda _: None,
        reply_composer=None, reply_gateway=gateway, operation_store_provider=lambda: operations,
        image_submitter=submit, workflow_service_provider=lambda: None,
    )
    parent = ConversationJob(
        job_id="parent", request_id="qq-parent", session_id="s", created_at=time.time(),
        bot_id="90001", scene="private", user_id="12345", prompt="请帮我画猫",
        chat_tools=("image_generate",),
    )
    if delivery_fails:
        with pytest.raises(ChatToolError):
            await processor.generate(parent)
        submit.assert_not_called()
    else:
        # 开画文案落盘后，父请求复用正文；无需再向聊天模型请求一条开画通知。
        from agent_bot.services.tool_operations import ReplyEffect
        operations.completed = AsyncMock(return_value=ReplyEffect(text="我来画一只猫。"))
        result = await processor.generate(parent)
        assert result.kind == "IMAGE_QUEUED"
        child = submit.await_args.args[0]
        assert child.prompt == "一只猫" and child.media_output == "IMAGE"
        assert child.background_image and child.workflow_request is None
        assert child.chat_tools == child.image_material_ids == ()
        gateway.send_bot_exact_part.assert_awaited_once()
