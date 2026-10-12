"""服务层与外部适配层的包边界测试。"""

from __future__ import annotations

import ast
from pathlib import Path


def test_service_and_integration_types_have_explicit_owners() -> None:
    """核心服务与外部客户端必须从各自分层包导入。"""

    from agent_bot.integrations.dispatch_ai import DispatchAiClient
    from agent_bot.integrations.mcp import ChatMcpServer
    from agent_bot.integrations.naraka import NarakaClient
    from agent_bot.integrations.onebot import (
        ChatToolSessionManager,
        ConversationHandler,
        ConversationQueueProcessor,
        EventContextService,
        OneBotReplyGateway,
        OneBotReplyService,
    )
    from agent_bot.integrations.redis_queue import build_conversation_queue
    from agent_bot.services.chat import ChatService
    from agent_bot.services.naraka import NarakaQueryService
    from agent_bot.services.replies import GeneratedReply, LlmReplyComposer, ReplyIntent

    assert all(
        value is not None
        for value in (
            ChatService,
            ConversationHandler,
            NarakaQueryService,
            ConversationQueueProcessor,
            DispatchAiClient,
            NarakaClient,
            ChatMcpServer,
            ChatToolSessionManager,
            EventContextService,
            build_conversation_queue,
            GeneratedReply,
            ReplyIntent,
            LlmReplyComposer,
            OneBotReplyGateway,
            OneBotReplyService,
        )
    )


def test_service_modules_do_not_import_transport_libraries() -> None:
    """服务层不得直接依赖 HTTP、Redis、MCP 或 OneBot 适配库。"""

    source_root = Path(__file__).parents[2] / "src" / "services"
    forbidden_roots = {"httpx", "mcp", "nonebot", "redis", "starlette"}
    violations: list[str] = []
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots = {alias.name.partition(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_roots = {node.module.partition(".")[0]}
            else:
                continue
            if imported_roots & forbidden_roots:
                violations.append(f"{path.name}:{node.lineno}")
    assert violations == []
