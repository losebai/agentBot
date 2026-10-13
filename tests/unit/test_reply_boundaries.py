"""AST audits of user-visible sends and LLM reply provenance ownership."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[2]
# 包源码已迁入 agent_bot；从真实包根检查投递与回复来源边界。
SOURCE_ROOT = PROJECT_ROOT / "src" / "agent_bot"
USER_REPLY_PATHS = (
    Path("integrations/onebot"),
    Path("plugin/handlers.py"),
    Path("services/chat_tools.py"),
    Path("services/tool_sessions.py"),
)
GATEWAY = Path("integrations/onebot/replies.py")
QUEUE_PROCESSOR = Path("integrations/onebot/queue_processor.py")
PROVENANCE_OWNERS = {
    (Path("services/replies.py"), "LlmReplyComposer.compose"),
    (QUEUE_PROCESSOR, "generated_reply_from_model"),
    (QUEUE_PROCESSOR, "ConversationQueueProcessor.deliver_start_part"),
    (QUEUE_PROCESSOR, "ConversationQueueProcessor.deliver_part"),
}
SEND_APIS = {"send_group_msg", "send_private_msg", "send_msg"}


class ReplyBoundaryAudit(ast.NodeVisitor):
    """Find forbidden calls without treating protocol declarations as sends."""

    def __init__(self, path: Path, tree: ast.Module, *, check_transport: bool) -> None:
        """Record the source owner and resolve imported symbol aliases."""

        self.path = path
        self.check_transport = check_transport
        self.scope: list[str] = []
        self.violations: list[str] = []
        self.aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    self.aliases[alias.asname or alias.name] = alias.name

    def name(self, node: ast.AST) -> str:
        """Resolve a dotted expression sufficiently to identify boundary calls."""

        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return f"{self.name(node.value)}.{node.attr}"
        return ""

    def reject(self, node: ast.AST, reason: str) -> None:
        """Attach a source location to one actionable architecture violation."""

        self.violations.append(f"{self.path.as_posix()}:{node.lineno}: {reason}")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Track the declaring class for precise constructor ownership."""

        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """Track functions and forbid legacy queue failure-message builders."""

        self.scope.append(node.name)
        if self.path == QUEUE_PROCESSOR and node.name in {
            "_failure_message", "_media_failure_message",
        }:
            self.reject(node, "queue failure text builder")
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        """Reject direct send/text construction and untrusted provenance creation."""

        name = self.name(node.func)
        method = name.rsplit(".", 1)[-1]
        if self.check_transport and self.path != GATEWAY:
            if method == "send" or method in SEND_APIS:
                self.reject(node, "direct OneBot send")
            elif method == "finish" and (node.args or node.keywords):
                self.reject(node, "matcher.finish with message")
            elif method == "call_api":
                api = node.args[0] if node.args else next(
                    (keyword.value for keyword in node.keywords if keyword.arg == "api"),
                    None,
                )
                if not isinstance(api, ast.Constant) or api.value in SEND_APIS:
                    self.reject(node, "direct or dynamic OneBot send API")
            elif name.endswith("MessageSegment.text"):
                self.reject(node, "user-visible text segment outside gateway")
        creates_reply = method == "GeneratedReply" or (
            ".GeneratedReply." in f".{name}"
            and method in {"model_validate", "model_validate_json", "model_construct", "parse_obj"}
        )
        if creates_reply and (self.path, ".".join(self.scope)) not in PROVENANCE_OWNERS:
            self.reject(node, "GeneratedReply outside trusted source adapter")
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        """Reject queue exception-to-fixed-text maps while allowing typed facts."""

        if self.path == QUEUE_PROCESSOR:
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    key is not None
                    and self.name(key).endswith(("Error", "Exception"))
                    and isinstance(value, (ast.Constant, ast.JoinedStr))
                ):
                    self.reject(node, "queue exception-to-fixed-text map")
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        """Reject fixed text returned by queue exception dispatch branches."""

        if self.path == QUEUE_PROCESSOR and any(
            isinstance(child, ast.Call) and self.name(child.func) == "isinstance"
            and any(
                self.name(part).endswith(("Error", "Exception"))
                for argument in child.args[1:] for part in ast.walk(argument)
            )
            for child in ast.walk(node.test)
        ):
            for statement in node.body:
                for child in ast.walk(statement):
                    if isinstance(child, ast.Return) and isinstance(
                        child.value, (ast.Constant, ast.JoinedStr),
                    ) and not (
                        isinstance(child.value, ast.Constant) and child.value.value is None
                    ):
                        self.reject(child, "queue exception-to-fixed-text branch")
        self.generic_visit(node)


def audit_source(path: Path, source: str, *, check_transport: bool = True) -> list[str]:
    """Parse source and return located violations of the reply boundary."""

    tree = ast.parse(source)
    audit = ReplyBoundaryAudit(path, tree, check_transport=check_transport)
    audit.visit(tree)
    return audit.violations


def test_user_reply_surfaces_only_send_through_gateway() -> None:
    """Business handlers cannot introduce a new direct text or media exit."""

    violations: list[str] = []
    for relative in USER_REPLY_PATHS:
        root = SOURCE_ROOT / relative
        assert root.exists(), f"reply surface is missing: {relative}"
        for path in sorted(root.rglob("*.py")) if root.is_dir() else [root]:
            violations.extend(audit_source(
                path.relative_to(SOURCE_ROOT), path.read_text(encoding="utf-8"),
            ))
    assert not violations, "\n".join(violations)


def test_generated_reply_is_created_only_by_trusted_source_adapters() -> None:
    """No module may manufacture LLM provenance outside explicit source adapters."""

    violations = [
        violation
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        for violation in audit_source(
            path.relative_to(SOURCE_ROOT), path.read_text(encoding="utf-8"),
            check_transport=False,
        )
    ]
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize("source", [
    "matcher.send(message)",
    "matcher.finish(message='fixed')",
    "matcher.finish(*messages)",
    "bot.send_group_msg(group_id=1, message=message)",
    "bot.send_private_msg(user_id=1, message=message)",
    "bot.call_api('send_group_msg', message=message)",
    "bot.call_api(api='send_private_msg', message=message)",
    "bot.call_api(api_name, message=message)",
    "MessageSegment.text('fixed')",
    "from nonebot.adapters.onebot.v11 import MessageSegment as Segment\nSegment.text('fixed')",
    "GeneratedReply(text='fixed', requestId='request')",
    "from agent_bot.services.replies import GeneratedReply as Reply\nReply(text='fixed')",
    "GeneratedReply.model_validate(payload)",
])
def test_audit_rejects_send_and_provenance_bypasses(source: str) -> None:
    """Each forbidden call shape produces a located architecture violation."""

    assert audit_source(Path("plugin/handlers.py"), source)


@pytest.mark.parametrize("source", [
    "matcher.finish()",
    "bot.call_api('get_group_member_list', group_id=1)",
    "gateway.send_media_matcher(matcher, segment)",
    "gateway.send_bot(bot, reply=reply)",
    "def send_group_msg(self, *, group_id, message): ...",
    "tool_result = 'Tool feedback for the outer LLM'",
])
def test_audit_allows_non_sending_declarations_and_gateway_calls(source: str) -> None:
    """Protocol declarations, inbound reads and typed gateway calls are permitted."""

    assert audit_source(Path("services/chat_tools.py"), source) == []


@pytest.mark.parametrize("source", [
    "def _failure_message(error): return 'fixed'",
    "def _media_failure_message(error): return 'fixed'",
    "messages = {TimeoutError: 'fixed'}",
    "def explain(error):\n    if isinstance(error, TimeoutError):\n        return 'fixed'",
])
def test_queue_audit_rejects_fixed_exception_replies(source: str) -> None:
    """Restoring static failure builders or mappings fails the queue audit."""

    assert audit_source(QUEUE_PROCESSOR, source)
