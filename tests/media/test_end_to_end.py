"""Python Agent、QQ 工具授权、持久队列与媒体 API 的离线跨层验收。"""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_bot.agent.catalog import AgentCatalog
from agent_bot.agent.contracts import ModelEvent, ModelTurn
from agent_bot.agent.records import AgentRunStore
from agent_bot.agent.service import AgentService
from agent_bot.agent.tools.chat import ChatToolBridge
from agent_bot.agent.tools.runtime import ToolRuntime
from agent_bot.ai.credentials import CredentialResolver
from agent_bot.ai.routing import RoutingAiClient
from agent_bot.config import RuntimeConfig
from agent_bot.integrations.mcp import ChatMcpServer
from agent_bot.integrations.onebot.queue_processor import ConversationQueueProcessor
from agent_bot.integrations.onebot.replies import OneBotReplyGateway
from agent_bot.integrations.tool_operations import RedisToolOperationStore
from agent_bot.models import LlmRequestOptions
from agent_bot.services.chat_tools import ChatToolContext, build_chat_tool_registry
from agent_bot.services.tool_sessions import ToolSessionStore
from agent_bot.services.workflow_execution import (
    WorkflowExecutionService,
    WorkflowImageCollector,
)
from agent_bot.services.workflows import WorkflowDefinition
from media.test_admin_tools import workflow_call
from media.test_agent_artifacts import NoRemote
from unit.test_image_tasks import image_job, image_queue, owner  # noqa: F401


def text_definition():
    """提供真实工作流编排可以校验和加载的最小文生图模板。"""
    return WorkflowDefinition(
        workflow_key="portrait",
        display_name="猫咪绘图",
        source_file="portrait.json",
        source_format="api",
        ui_json=None,
        content_sha256="a" * 64,
        positive_node_id="2",
        negative_node_id="3",
        output_node_id="7",
        seed_node_id="10",
        api_json=json.dumps(
            {
                "1": {"class_type": "CheckpointLoaderSimple", "inputs": {}},
                "2": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
                "3": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
                "4": {"class_type": "EmptyLatentImage", "inputs": {}},
                "7": {"class_type": "PreviewImage", "inputs": {"images": ["10", 0]}},
                "10": {
                    "class_type": "KSampler",
                    "inputs": {
                        "model": ["1", 0],
                        "positive": ["2", 0],
                        "negative": ["3", 0],
                        "latent_image": ["4", 0],
                        "seed": 1,
                    },
                },
            }
        ),
    )


class JavaMustNotBeCalled:
    """任何旧 Java 调用都立即失败，不能悄悄使用旧服务弥补错误。"""

    async def complete(self, **kwargs):
        """禁止回退普通调用。"""
        raise AssertionError("不应调用 Java complete")

    async def generate_media(self, **kwargs):
        """禁止回退媒体生成。"""
        raise AssertionError("不应调用 Java media")


class QueueProvider:
    """模型边界只要求一次绘图，然后等待用户停止。"""

    def __init__(self):
        """用事件让测试精确停在工具已入队后的模型轮次。"""
        self.calls = 0
        self.after_tool = asyncio.Event()

    def validate_options(self, options):
        """离线模型接受默认选项。"""

    async def stream_turn(self, call):
        """真实 Agent 循环必须完成授权、工具执行和第二轮调用。"""
        self.calls += 1
        if self.calls == 1:
            yield ModelEvent(
                kind="turn_end", turn=ModelTurn(model="actual", calls=(workflow_call(),))
            )
        else:
            self.after_tool.set()
            await asyncio.Event().wait()


@pytest.mark.parametrize("delivery_fails", [True, False])
async def test_qq_delivery_and_stopping_agent_preserve_queue_semantics(
    media_model,
    media_database,
    agent_params,
    image_queue,  # noqa: F811
    delivery_fails,
):
    """开画发送失败绝不入队；成功入队后停止父 Agent 不撤销后台图片。"""
    env = image_queue
    bot = AsyncMock()

    async def send_group_msg(**kwargs):
        """只替换 QQ 网络边界，仍使用生产回复网关。"""
        return await bot.call_api("send_group_msg", **kwargs)

    bot.send_group_msg.side_effect = send_group_msg
    if delivery_fails:
        bot.call_api.side_effect = ConnectionError("离线发送失败")
    config = RuntimeConfig(
        onebot_v11_access_token="x" * 40,
        dispatch_chat_session_secret="y" * 40,
        dispatch_chat_local_workflow_enabled=True,
        dispatch_chat_image_generation_enabled=True,
        dispatch_chat_mcp_enabled=True,
    )
    operations = RedisToolOperationStore(
        env.redis, key_prefix="media:e2e:operations", ttl_seconds=60
    )
    comfy = SimpleNamespace(generate=AsyncMock())
    workflow = WorkflowExecutionService(
        store=SimpleNamespace(get_executable=lambda _: text_definition()), client=comfy
    )

    @asynccontextmanager
    async def lease():
        """处理器只需要已进入运行时这一边界，无额外网络资源。"""
        yield

    processor = ConversationQueueProcessor(
        config_provider=lambda: config,
        chat_service_provider=lambda: None,
        runtime_lease=lease,
        tool_session_provider=lambda _: lease(),
        bot_provider=lambda _: bot,
        reply_composer=SimpleNamespace(),
        reply_gateway=OneBotReplyGateway(lambda: config),
        operation_store_provider=lambda: operations,
        image_submitter=env.queue.enqueue,
        workflow_service_provider=lambda: workflow,
    )
    parent = image_job("parent").model_copy(
        update={"background_image": False, "media_output": None}
    )
    collector = WorkflowImageCollector()
    collector.submit = lambda request, text: processor._submit_image(
        parent, request, start_reply=text
    )
    context = ChatToolContext(
        session_id=parent.session_id,
        user_id=parent.user_id,
        job_id=parent.job_id,
        allowed_tools=frozenset({"local_workflow_generate_image"}),
        workflow_service=workflow,
        workflow_collector=collector,
        operation_store=operations,
    )
    sessions = ToolSessionStore(ttl_seconds=30, max_sessions=2)
    bridge = ChatToolBridge(
        ChatMcpServer(
            store=sessions, registry=build_chat_tool_registry(), allowed_hosts=["localhost:*"]
        )
    )
    provider = QueueProvider()
    service = AgentService(
        AgentCatalog(agent_params, connect=media_database.connect),
        AgentRunStore(agent_params, connect=media_database.connect),
        CredentialResolver(environ={}),
        {"openai-chat": provider},
        ToolRuntime(NoRemote(), chat_bridge=bridge),
    )
    routing = RoutingAiClient(
        JavaMustNotBeCalled(),
        None,
        plain_backend="java",
        agent_backend="python",
        media_backend="python",
        agent_service=service,
        timeout_seconds=30,
        default_options=LlmRequestOptions(),
        mcp_services=(),
        laya_enabled=False,
    )
    async with sessions.open(context) as token:
        task = asyncio.create_task(
            routing.complete(
                model="local-model",
                session_id=parent.session_id,
                prompt="画猫",
                messages=(),
                request_id="qq-media",
                use_agent=True,
                use_web_search=False,
                chat_tools=("local_workflow_generate_image",),
                tool_session_token=token,
            )
        )
        try:
            await asyncio.wait_for(provider.after_tool.wait(), 3)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    tasks = (await env.manager.list_tasks(owner())).tasks
    assert bot.call_api.await_count == 1
    comfy.generate.assert_not_called()
    if delivery_fails:
        assert tasks == [] and collector.queued_task_id is None
    else:
        assert len(tasks) == 1 and tasks[0].status == "queued"
        assert not tasks[0].cancellation_requested
        assert collector.queued_task_id == tasks[0].task_id
    runs = await service.records.list_runs()
    assert runs.items[0].status == "cancelled"
