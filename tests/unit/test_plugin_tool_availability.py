"""验证插件工具只随当前生命周期开放，不以数据库中的待重启选择授予能力。"""

from types import SimpleNamespace

import pytest

from agent_bot.agent.contracts import ExecutionAuthority, ToolCall
from agent_bot.agent.errors import AgentError
from agent_bot.config import RuntimeConfig
from agent_bot.integrations.onebot.tooling import ChatToolSessionManager
from agent_bot.plugins.bilibili.tools import BilibiliTools


def test_qq_plugin_tools_follow_runtime_provider_instead_of_saved_selection():
    """配置已选 B站仍不能提前授权；运行中与停止后使用同一实时能力提供器。"""
    config = RuntimeConfig(app_enabled_plugins=['qqbot', 'bilibili'], chat_mcp_enabled=True,
                           chat_job_enabled=False, chat_tagged_memory_enabled=False,
                           chat_image_generation_enabled=False, chat_local_workflow_enabled=False,
                           chat_tts_enabled=False, superusers={'10001'},
                           onebot_v11_access_token='offline-onebot-' + 'o' * 32,
                           chat_session_secret='offline-session-' + 's' * 32)
    available = ()
    manager = ChatToolSessionManager(
        config_provider=lambda: config, store_provider=lambda: object(),
        naraka_service_provider=lambda: None,
        group_member_refresher=lambda _bot, _group: None,
        plugin_tools_provider=lambda: available,
    )
    event = SimpleNamespace(user_id=10001)
    assert manager.available_tools(event) == ()
    available = ('bilibili_login_status', 'bilibili_get_comments', 'bilibili_reply_comment')
    assert manager.available_tools(event) == available
    config = config.model_copy(update={'app_enabled_plugins': []})
    assert manager.available_tools(event) == available
    available = ()
    assert manager.available_tools(event) == ()


async def test_bilibili_stopping_revokes_previously_bound_tool_session():
    """运行前不注册定义，停止后既不注册新定义，也不能沿用旧会话的权限。"""
    service = None
    tools = BilibiliTools(lambda: service, lambda: set())
    request = SimpleNamespace(session_id='offline', attachment_ids=())
    authority = ExecutionAuthority(entry='admin', allowed_tools=('bilibili_login_status',))
    assert tools.bind(request, authority, None, None, None).definitions == ()
    service = object()
    session = tools.bind(request, authority, None, None, None)
    assert {item.name for item in session.definitions} == {
        'bilibili_login_status', 'bilibili_get_comments',
        'bilibili_reply_comment', 'bilibili_publish_video',
    }
    service = None
    assert tools.bind(request, authority, None, None, None).definitions == ()
    with pytest.raises(AgentError, match='AGENT_TOOL_UNAUTHORIZED'):
        await session.call(ToolCall(call_id='c', name='bilibili_login_status', arguments={}))
