"""整批工具预检、附件事件及失败后保留已生成成果。"""

from contextlib import aclosing, asynccontextmanager
from types import SimpleNamespace

import pytest

from agent_bot.agent.catalog import AgentCatalog
from agent_bot.agent.contracts import (
    AgentRequest,
    ExecutionAuthority,
    ModelEvent,
    ModelTurn,
    ToolCall,
)
from agent_bot.agent.errors import AgentError
from agent_bot.agent.records import AgentRunStore
from agent_bot.agent.service import AgentService
from agent_bot.agent.tools.runtime import ToolRuntime
from agent_bot.ai.credentials import CredentialResolver
from media.test_admin_tools import adapters, workflow_call
from media.test_store import OWNER, storage


class NoRemote:
    """显式无远端服务，防止测试意外访问网络。"""

    @asynccontextmanager
    async def open(self, servers, **kwargs):
        """提供空发现结果，媒体工具由真实运行时绑定。"""
        assert not servers
        yield SimpleNamespace(tools=(), warnings=())


class MediaAgentProvider:
    """只替换模型响应，保留真实 Agent 循环和工具副作用。"""

    def __init__(self, *, invalid=False, fail_final=False):
        """控制第二个参数非法或最后模型轮次失败。"""
        self.invalid, self.fail_final, self.calls = invalid, fail_final, 0

    def validate_options(self, options):
        """测试不依赖特定模型选项。"""

    async def stream_turn(self, call):
        """首轮请求真实绘图，后续返回文本或稳定错误。"""
        self.calls += 1
        if self.calls == 1:
            calls = (workflow_call(),)
            if self.invalid:
                calls += (
                    ToolCall(
                        call_id="bad",
                        name="local_workflow_generate_image",
                        arguments={**workflow_call().arguments, "workflow_key": "../bad"},
                    ),
                )
            yield ModelEvent(kind="turn_end", turn=ModelTurn(model="actual", calls=calls))
        elif self.fail_final:
            raise AgentError("AI_UPSTREAM_UNAVAILABLE")
        else:
            yield ModelEvent(kind="text_delta", delta="图片已生成")
            yield ModelEvent(kind="turn_end", turn=ModelTurn(model="actual", content="图片已生成"))


def build_agent(database, params, store, tools, provider):
    """生产运行时负责绑定媒体工具，记录读取通过同一媒体存储关联附件。"""
    return AgentService(
        AgentCatalog(params, connect=database.connect),
        AgentRunStore(params, connect=database.connect, media_store=store),
        CredentialResolver(environ={}),
        {"openai-chat": provider},
        ToolRuntime(NoRemote(), admin_media_tools=tools),
        media_store=store,
    )


def request():
    """请求仅授权绘图工具，不能借工作流调用获取 QQ 权限。"""
    return AgentRequest(
        model="local-model",
        prompt="画猫",
        session_id=OWNER.session_id,
        request_id="r",
        tools=("local_workflow_generate_image",),
    )


AUTHORITY = ExecutionAuthority(entry="admin", allowed_tools=("local_workflow_generate_image",))


async def test_later_invalid_local_call_prevents_entire_batch(
    media_model, media_database, agent_params, tmp_path, media_clock
):
    """剥离 pattern 后仍必须对整批执行原参数模型验证，首项也不能生效。"""
    async with storage(media_database, agent_params, tmp_path / "media", media_clock) as store:
        tools, workflow, _, _ = adapters(store, tmp_path)
        service = build_agent(
            media_database, agent_params, store, tools, MediaAgentProvider(invalid=True)
        )
        with pytest.raises(AgentError, match="AGENT_TOOL_ARGUMENTS_INVALID"):
            await service.complete(request(), AUTHORITY)
        workflow.generate.assert_not_called()


async def test_artifact_sequence_and_done_match(
    media_model, media_database, agent_params, tmp_path, media_clock
):
    """工具完成事件先于附件，最终元数据和运行详情关联相同附件。"""
    async with storage(media_database, agent_params, tmp_path / "media", media_clock) as store:
        tools, _, _, _ = adapters(store, tmp_path)
        service = build_agent(media_database, agent_params, store, tools, MediaAgentProvider())
        async with aclosing(service.stream(request(), AUTHORITY)) as stream:
            events = [event async for event in stream]
        kinds = [event.kind for event in events]
        artifact = next(event.attachment for event in events if event.kind == "artifact")
        assert kinds.index("tool_result") < kinds.index("artifact") < kinds.index("done")
        assert [event.sequence for event in events] == sorted({event.sequence for event in events})
        assert events[-1].metadata.attachments == (artifact,)
        detail = await service.records.get_run(events[0].execution_id)
        assert detail.attachments == (artifact,) and detail.status == "completed"
        assert all(
            "base64" not in event.model_dump_json() and "storage_key" not in event.model_dump_json()
            for event in events
        )


@pytest.mark.parametrize("cancel", [False, True])
async def test_ready_artifact_survives_failure_or_close(
    media_model, media_database, agent_params, tmp_path, media_clock, cancel
):
    """生成已完成但模型失败或附件事件前断开时，运行详情仍能获取成果。"""
    async with storage(media_database, agent_params, tmp_path / "media", media_clock) as store:
        tools, _, _, _ = adapters(store, tmp_path)
        service = build_agent(
            media_database, agent_params, store, tools, MediaAgentProvider(fail_final=True)
        )
        events = []
        async with aclosing(service.stream(request(), AUTHORITY)) as stream:
            async for event in stream:
                events.append(event)
                if cancel and event.kind == "tool_result":
                    break
        detail = await service.records.get_run(events[0].execution_id)
        assert detail.status == ("cancelled" if cancel else "failed")
        assert detail.attachments[0].status == "ready"
        assert "done" not in [event.kind for event in events]
