"""网页读取失败应留在工具层，不能重跑已完成的 Agent 操作。"""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
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
from agent_bot.agent.tools.public_http import PublicPageReader
from agent_bot.agent.tools.read_url import ReadUrlTool
from agent_bot.agent.tools.runtime import BoundTools
from agent_bot.ai.credentials import CredentialResolver
from agent_bot.services.request_deadline import request_deadline


def read_call() -> ToolCall:
    """构造包含敏感查询参数的 URL，以验证错误日志不会复制完整链接。"""
    return ToolCall(call_id='page-1', name='read_url', arguments={
        'url': 'https://example.com/article?token=private-query-token',
    })


@pytest.mark.parametrize('failure', [403, 404, 429, 503, 'connect'])
async def test_page_unavailability_is_an_explicit_tool_error(failure, caplog):
    """HTTP 拒绝与连接失败返回错误标记，日志只保留主机和原因而不泄漏链接。"""
    def respond(request):
        """替代外部网络，只模拟明确的 HTTP 或连接结果。"""
        if failure == 'connect':
            raise httpx.ConnectError('private-upstream-detail', request=request)
        return httpx.Response(failure, text='private-upstream-response')

    tool = ReadUrlTool(PublicPageReader(transport=httpx.MockTransport(respond)))
    result = await tool.call(read_call())
    assert result.is_error and result.call_id == 'page-1' and result.name == 'read_url'
    payload = json.loads(result.content)
    assert payload['error']['code'] == 'AGENT_PAGE_UNAVAILABLE'
    assert 'content' not in payload
    assert 'example.com' in caplog.text
    reason = str(failure) if isinstance(failure, int) else 'ConnectError'
    assert reason in caplog.text
    assert 'private-' not in caplog.text + result.content


async def test_private_url_rejection_still_aborts():
    """访问内网的权限拒绝不能被转换为可继续执行的工具错误。"""
    tool = ReadUrlTool(PublicPageReader(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={'content-type': 'text/plain'}, text='x'),
    )))
    with pytest.raises(AgentError, match='AGENT_URL_REJECTED'):
        await tool.call(read_call().model_copy(update={'arguments': {'url': 'http://127.0.0.1'}}))


async def test_expired_request_budget_still_aborts():
    """总预算耗尽属于请求终态，不能作为普通网页失败继续请求模型。"""
    tool = ReadUrlTool(PublicPageReader(transport=httpx.MockTransport(
        lambda request: httpx.Response(503),
    )))
    with request_deadline(-1), pytest.raises(TimeoutError):
        await tool.call(read_call())


async def test_page_cancellation_still_propagates():
    """取消立即离开工具，不生成失败正文或吞掉取消信号。"""
    async def cancel(request):
        """模拟请求在网络传输期间被取消。"""
        raise asyncio.CancelledError

    tool = ReadUrlTool(PublicPageReader(transport=httpx.MockTransport(cancel)))
    with pytest.raises(asyncio.CancelledError):
        await tool.call(read_call())


async def test_failed_page_read_reaches_model_and_records_failed_step(
    agent_database, agent_params, seeded_model,
):
    """真实图执行和落库继续收尾，网页失败只记录为失败步骤而非未知结果。"""
    requests = []

    def respond(request):
        """计数实际网页读取，确保服务没有重跑请求。"""
        requests.append(str(request.url))
        return httpx.Response(403)

    tool = ReadUrlTool(PublicPageReader(transport=httpx.MockTransport(respond)))

    class ToolRuntime:
        """保留真实工具校验和执行，仅替代外部配置装配。"""

        @asynccontextmanager
        async def open(self, snapshot, request, authority, **kwargs):
            """只绑定当前测试授权的网页工具。"""
            yield BoundTools((tool.definition,), {'read_url': tool.call})

    class Provider:
        """受控模型收到明确失败后生成说明，不访问付费模型。"""

        def validate_options(self, options):
            """本测试没有协议参数差异。"""

        async def stream_turn(self, call):
            """首轮请求网页，收尾轮验证工具失败确实传入模型上下文。"""
            if not call.final_only:
                yield ModelEvent(kind='turn_end', turn=ModelTurn(
                    model='m', calls=(read_call(),),
                ))
                return
            result = call.messages[-1].tool_result
            assert result.is_error
            assert json.loads(result.content)['error']['code'] == 'AGENT_PAGE_UNAVAILABLE'
            answer = '网页无法读取，未获取正文。'
            yield ModelEvent(kind='text_delta', delta=answer)
            yield ModelEvent(kind='turn_end', turn=ModelTurn(model='m', content=answer))

    records = AgentRunStore(agent_params, connect=agent_database.connect)
    service = AgentService(
        AgentCatalog(agent_params, connect=agent_database.connect), records,
        CredentialResolver(), {'openai-chat': Provider()}, ToolRuntime(),
        maximum_tool_calls=1,
    )
    result = await service.complete(AgentRequest(
        model=seeded_model[2].alias, session_id='qq-test', request_id='qq-read-failure',
        prompt='读取网页', max_steps=1,
    ), ExecutionAuthority(entry='qq', allowed_tools=('read_url',)))
    assert result.content == '网页无法读取，未获取正文。' and result.record_saved
    assert len(requests) == 1
    saved = await records.get_run(result.execution_id)
    assert saved.status == 'completed'
    steps = (await records.list_steps(result.execution_id)).items
    page_step = next(step for step in steps if step.kind == 'tool_result')
    assert page_step.status == 'failed' and page_step.summary == 'AGENT_TOOL_FAILED'
