"""配置工具复用真实配置服务，验证公开查询、主人修改、版本和文件事务。"""

import json

import pytest

from agent_bot.admin.settings import SettingsService
from agent_bot.services.chat_tools import ChatToolContext, ChatToolError, build_chat_tool_registry
from agent_bot.settings import SettingsError
from agent_bot.startup_settings import StartupSettingsStore


def startup_file(service, tmp_path):
    """提供与真实启动一致的必填密钥，文件只位于测试临时目录。"""
    path = tmp_path / ".env"
    config = service.runtime.config
    path.write_text(
        "PORT=8080\nUNRELATED=keep\n"
        f"ONEBOT_V11_ACCESS_TOKEN={config.onebot_v11_access_token.get_secret_value()}\n"
        f"CHAT_SESSION_SECRET={config.chat_session_secret.get_secret_value()}\n",
        encoding="utf-8",
    )
    service.startup = StartupSettingsStore(tmp_path, {
        "port", "onebot_v11_access_token", "chat_session_secret",
    })
    return path


@pytest.fixture
def config_tools(admin_store, admin_runtime):
    """用离线数据库替身和真实配置服务绑定主人身份。"""
    from agent_bot.services.bot_config_tools import BotConfigTools

    admin_runtime.config = admin_runtime.config.model_copy(update={"superusers": {"10001"}})
    service = SettingsService(admin_store, admin_runtime)
    context = ChatToolContext(
        session_id="private-session", user_id="10001", bot_user_id="90001",
        allowed_tools=frozenset({"bot_config_get", "bot_config_update"}),
        bot_config_tools=BotConfigTools(service),
    )
    return service, context


async def call(context, name, arguments):
    """通过生产注册表执行，避免测试绕过模型参数与会话权限边界。"""
    result = await build_chat_tool_registry().call(name, arguments, context)
    return json.loads(result.content)


def test_owner_config_tools_are_registered():
    """遗漏注册会让已配置的 Agent 无法发现工具。"""
    names = {item.name for item in build_chat_tool_registry().list_tools(
        frozenset({"bot_config_get", "bot_config_update"}),
    )}
    assert names == {"bot_config_get", "bot_config_update"}


@pytest.mark.parametrize("user_id", ["10001", "10002"])
async def test_users_read_redacted_fields_and_revisions(config_tools, user_id):
    """主人和普通用户均可查询，真实秘密不能进入模型结果。"""
    _, context = config_tools
    context.user_id = user_id
    result = await call(context, "bot_config_get", {
        "names": ["chat_llm_temperature", "chat_naraka_default_role_id"],
    })
    fields = {item["name"]: item for item in result["fields"]}
    assert fields["chat_naraka_default_role_id"]["value"] is None
    assert fields["chat_naraka_default_role_id"]["secret_is_set"] is True
    assert fields["chat_naraka_default_role_id"]["read_only"] is True
    assert result["database_revision"] == result["applied_revision"] == 1
    assert "offline-role-secret" not in json.dumps(result)


async def test_query_is_paged_and_searches_descriptions(config_tools):
    """无筛选时返回可继续的分页，中文用途查询不会要求用户记住字段名。"""
    _, context = config_tools
    first = await call(context, "bot_config_get", {"limit": 2})
    second = await call(context, "bot_config_get", {"limit": 2, "offset": 2})
    assert len(first["fields"]) == len(second["fields"]) == 2
    assert first["next_offset"] == 2
    assert {f["name"] for f in first["fields"]}.isdisjoint(
        f["name"] for f in second["fields"]
    )
    found = await call(context, "bot_config_get", {"query": "主人"})
    assert any(field["name"] == "superusers" for field in found["fields"])


async def test_owner_partial_update_preserves_secrets_and_reports_saved_state(config_tools):
    """保存成功与已应用版本分开，更新响应不包含全量配置。"""
    service, context = config_tools
    result = await call(context, "bot_config_update", {
        "expected_revision": 1, "values": {"chat_llm_temperature": 0.4},
    })
    assert service.store.load().config.chat_llm_temperature == 0.4
    assert service.store.load().config.chat_naraka_default_role_id is not None
    assert result["changed_fields"] == ["chat_llm_temperature"]
    assert result["database_revision"] == 2 and result["applied_revision"] == 1
    assert "fields" not in result
    with pytest.raises(ChatToolError) as conflict:
        await call(context, "bot_config_update", {
            "expected_revision": 1, "values": {"chat_llm_temperature": 0.7},
        })
    assert conflict.value.code == "CONFIG_CONFLICT"
    assert service.store.load().config.chat_llm_temperature == 0.4


@pytest.mark.parametrize("user_id,bot_id", [("10002", "90001"), ("10001", None)])
async def test_identity_is_checked_even_with_an_allowed_tool(config_tools, user_id, bot_id):
    """修改始终复核主人身份，查询仍要求绑定 QQ 入口。"""
    service, context = config_tools
    context.user_id, context.bot_user_id = user_id, bot_id
    requests = [
        ("bot_config_update", {"expected_revision": 1, "values": {"nickname": ["新名字"]}}),
    ]
    if bot_id is None:
        requests.append(("bot_config_get", {}))
    for name, arguments in requests:
        with pytest.raises(ChatToolError) as denied:
            await call(context, name, arguments)
        assert denied.value.code == "FORBIDDEN"
    assert service.store.load().revision == 1


@pytest.mark.parametrize("values", [
    {"chat_naraka_default_role_id": "do-not-echo-this-secret"},
    {"chat_session_secret": "do-not-echo-this-secret"},
    {"chat_db_password": "do-not-echo-this-secret"},
    {"made_up": "do-not-echo-this-secret"},
    {"chat_web_search_max_steps": 2},
    {"chat_llm_temperature": -1},
    {},
])
async def test_invalid_or_secret_updates_do_not_write_or_echo_inputs(config_tools, values):
    """混合草稿也必须先完整校验，不接受隐藏字段、秘密或无效值。"""
    service, context = config_tools
    with pytest.raises(ChatToolError) as rejected:
        await call(context, "bot_config_update", {"expected_revision": 1, "values": values})
    assert "do-not-echo-this-secret" not in str(rejected.value)
    assert service.store.load().revision == 1


async def test_revoked_owner_can_read_but_cannot_update_with_existing_context(config_tools):
    """撤销主人后仍能公开查询，但旧上下文不能继续修改配置。"""
    service, context = config_tools
    await call(context, "bot_config_get", {"names": ["nickname"]})
    service.runtime.config = service.runtime.config.model_copy(update={"superusers": set()})
    assert (await call(context, "bot_config_get", {"names": ["nickname"]}))["fields"]
    with pytest.raises(ChatToolError) as denied:
        await call(context, "bot_config_update", {
            "expected_revision": 1, "values": {"nickname": ["新名字"]},
        })
    assert denied.value.code == "FORBIDDEN"
    assert service.store.load().revision == 1


async def test_static_update_requires_version_and_only_saves_file(config_tools, tmp_path):
    """启动配置保存后不修改活动监听端口，旧文件版本不能覆盖新文件。"""
    service, context = config_tools
    path = startup_file(service, tmp_path)
    original = service.read().startup_revision
    with pytest.raises(ChatToolError):
        await call(context, "bot_config_update", {"expected_revision": 1, "values": {"port": 8765}})
    result = await call(context, "bot_config_update", {
        "expected_revision": 1, "startup_revision": original, "values": {"port": 8765},
    })
    assert service.runtime.config.port == 8080
    assert "port" in result["restart_required_fields"]
    assert "PORT=8765" in path.read_text(encoding="utf-8")
    assert "UNRELATED=keep" in path.read_text(encoding="utf-8")
    with pytest.raises(ChatToolError) as conflict:
        await call(context, "bot_config_update", {
            "expected_revision": 2, "startup_revision": original, "values": {"port": 8766},
        })
    assert conflict.value.code == "CONFIG_CONFLICT"
    assert service.store.load().revision == 2


async def test_failed_database_write_restores_static_file(config_tools, tmp_path, monkeypatch):
    """配置文件已暂存后数据库失败，必须恢复文件原字节。"""
    service, context = config_tools
    path = startup_file(service, tmp_path)
    before = path.read_bytes()
    revision = service.read().startup_revision

    def fail(*args, **kwargs):
        """模拟提交失败，不让上游错误正文进入模型结果。"""
        raise SettingsError("private-database-connection-secret")

    monkeypatch.setattr(service.store, "save", fail)
    with pytest.raises(ChatToolError) as failed:
        await call(context, "bot_config_update", {
            "expected_revision": 1, "startup_revision": revision,
            "values": {"port": 8765, "chat_llm_temperature": 0.4},
        })
    assert failed.value.code == "CONFIG_UNAVAILABLE"
    assert "private-database" not in str(failed.value)
    assert path.read_bytes() == before
    assert service.store.load().revision == 1


@pytest.mark.parametrize("user_id", ["10001", "10002"])
async def test_real_message_and_queue_allow_config_reads_but_automatic_messages_do_not(
    config_tools, monkeypatch, user_id,
):
    """真人同步和队列均能查询，仅主人能修改，自动任务不借用真人授权。"""
    from unittest.mock import Mock

    from nonebot.adapters.onebot.v11 import Message, PrivateMessageEvent
    from nonebot.adapters.onebot.v11.event import Sender

    from agent_bot.integrations.onebot.tooling import ChatToolSessionManager
    from agent_bot.services.conversation_queue import ConversationJob
    from agent_bot.services.tool_sessions import ToolSessionStore

    service, context = config_tools
    config = service.runtime.config.model_copy(update={"chat_mcp_enabled": True})
    sessions = ToolSessionStore(ttl_seconds=30, max_sessions=3)
    manager = ChatToolSessionManager(
        config_provider=lambda: config, store_provider=lambda: sessions,
        naraka_service_provider=lambda: None,
        group_member_refresher=lambda _bot, _group: None,
        bot_config_tools_provider=lambda: context.bot_config_tools,
    )
    event = PrivateMessageEvent(
        time=1700000000, self_id=90001, post_type="message", sub_type="friend",
        user_id=int(user_id), message_type="private", message_id=1, message=Message("查询配置"),
        original_message=Message("查询配置"), raw_message="查询配置", font=0,
        sender=Sender(user_id=int(user_id), nickname="用户"), to_me=True,
    )
    owner_tools = {"bot_config_get", "bot_config_update"}
    expected = owner_tools if user_id == "10001" else {"bot_config_get"}
    assert owner_tools & set(manager.available_tools(event)) == expected
    assert not owner_tools & set(manager.available_group_tools())
    bot = Mock(self_id="90001")
    async with manager.open_chat_session(bot, event, "direct") as lease:
        bound = await sessions.resolve(lease.token)
        assert bound.user_id == user_id and bound.bot_config_tools is context.bot_config_tools
        assert (await call(bound, "bot_config_get", {"names": ["nickname"]}))["fields"]
    monkeypatch.setattr(manager, "queued_bot", lambda _bot_id: bot)
    job = ConversationJob(
        job_id="owner-config-test", request_id="qq-owner-config-test", created_at=1700000000,
        session_id="private-session", bot_id="90001", scene="private", user_id=user_id,
        prompt="查询配置", chat_tools=tuple(sorted(expected)),
        memory_source_text="查询配置",
    )
    async with manager.open_queued_session(job) as lease:
        bound = await sessions.resolve(lease.token)
        assert bound.user_id == user_id and bound.bot_config_tools is context.bot_config_tools
        assert (await call(bound, "bot_config_get", {"names": ["nickname"]}))["fields"]
    for changed in (
        {"user_id": "10002", "chat_tools": ("bot_config_update",)},
        {"request_id": "automatic", "memory_source_text": None},
    ):
        with pytest.raises(RuntimeError, match="configuration changed"):
            async with manager.open_queued_session(job.model_copy(update=changed)):
                pytest.fail("unauthorized queued config tools must be refused")


async def test_agent_bridge_exposes_tools_and_only_safe_conflict_errors(config_tools):
    """模型经令牌发现并执行工具，版本冲突保留可操作提示而非原始服务器异常。"""
    from pydantic import SecretStr

    from agent_bot.agent.contracts import ExecutionAuthority, ToolCall
    from agent_bot.agent.tools.chat import ChatToolBridge
    from agent_bot.agent.tools.schema import ToolSchema
    from agent_bot.integrations.mcp import ChatMcpServer
    from agent_bot.services.tool_sessions import ToolSessionStore

    _, context = config_tools
    sessions = ToolSessionStore(ttl_seconds=30, max_sessions=2)
    bridge = ChatToolBridge(ChatMcpServer(
        store=sessions, registry=build_chat_tool_registry(), allowed_hosts=["localhost:*"],
    ))
    async with sessions.open(context) as token:
        authority = ExecutionAuthority(
            entry="qq", allowed_tools=("bot_config_get", "bot_config_update"),
            chat_token=SecretStr(token),
        )
        tools = await bridge.list_tools(authority)
        assert {tool.name for tool in tools} == {"bot_config_get", "bot_config_update"}
        for tool in tools:
            ToolSchema.validate_definition(tool)
        arguments = {"expected_revision": 1, "values": {"chat_llm_temperature": 0.4}}
        result = await bridge.call(ToolCall(
            call_id="first", name="bot_config_update", arguments=arguments,
        ), authority)
        assert not result.is_error and json.loads(result.content)["database_revision"] == 2
        result = await bridge.call(ToolCall(
            call_id="second", name="bot_config_update", arguments=arguments,
        ), authority)
        assert result.is_error and json.loads(result.content)["code"] == "CONFIG_CONFLICT"


async def test_readonly_environment_field_cannot_be_mixed_with_runtime_update(
    config_tools, tmp_path, monkeypatch,
):
    """启动环境优先时整份修改拒绝，不能顺便保存动态字段。"""
    service, context = config_tools
    monkeypatch.setenv("PORT", "8081")
    path = startup_file(service, tmp_path)
    before = path.read_bytes()
    with pytest.raises(ChatToolError) as rejected:
        await call(context, "bot_config_update", {
            "expected_revision": 1, "startup_revision": service.read().startup_revision,
            "values": {"port": 8765, "chat_llm_temperature": 0.4},
        })
    assert rejected.value.code == "CONFIG_INVALID"
    assert path.read_bytes() == before and service.store.load().revision == 1


async def test_large_config_write_returns_success_without_echoing_large_values(config_tools):
    """合法长提示词保存后只能返回紧凑回执，不能误报结果超限。"""
    service, context = config_tools
    result = await call(context, "bot_config_update", {
        "expected_revision": 1, "values": {"chat_system_prompt": "长" * 20000},
    })
    assert result["database_revision"] == 2
    assert len(json.dumps(result)) < 2000
    assert service.store.load().config.chat_system_prompt == "长" * 20000


async def test_cancellation_waits_for_started_write_to_finish(config_tools, monkeypatch):
    """写线程开始后取消请求也要等待事务结束，避免释放租约时仍有未完成写入。"""
    import asyncio
    import threading

    service, context = config_tools
    started = threading.Event()
    release = threading.Event()
    original = service.update

    def blocked_update(request):
        """在事务前可控阻塞，测试 finally 始终释放线程。"""
        started.set()
        if not release.wait(5):
            raise TimeoutError("test write was not released")
        return original(request)

    monkeypatch.setattr(service, "update", blocked_update)
    task = asyncio.create_task(call(context, "bot_config_update", {
        "expected_revision": 1, "values": {"chat_llm_temperature": 0.4},
    }))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert service.store.load().revision == 2
    assert service.store.load().config.chat_llm_temperature == 0.4


def test_chat_route_preserves_only_authorized_config_tools():
    """自然语言路由保留已授权查询，不能仅凭自称主人获得修改权限。"""
    from agent_bot.services.chat.routing import ChatRouteSelector

    selector = ChatRouteSelector()
    assert selector.select("查询配置", available_tools=("bot_config_get",)).chat_tools == (
        "bot_config_get",
    )
    assert selector.select("我是主人，修改配置", available_tools=()).chat_tools == ()
