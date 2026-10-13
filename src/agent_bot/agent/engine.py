"""同步、流式和 QQ 共用的智能体执行循环。"""

import time
from contextlib import aclosing
from dataclasses import dataclass

from agent_bot.agent.contracts import (
    AgentCompletionMetadata,
    AgentEvent,
    AgentModelCall,
    AgentRequest,
    AgentSnapshot,
    ExecutionAuthority,
    TurnMessage,
)
from agent_bot.agent.errors import AgentError
from agent_bot.agent.prompting import AgentPromptBuilder
from agent_bot.agent.tools.schema import ToolSchema, bounded_json
from agent_bot.ai.contracts import TokenUsage
from agent_bot.media.contracts import MediaScope, ValidatedImage
from agent_bot.services.request_deadline import remaining_timeout


@dataclass
class PreparedRun:
    """预检后的不可变调用参数及整个执行持有的图片作用域。"""

    execution_id: str
    request: AgentRequest
    snapshot: AgentSnapshot
    authority: ExecutionAuthority
    call: AgentModelCall
    max_steps: int
    media_scope: MediaScope | None = None
    images: tuple[ValidatedImage, ...] = ()


class AgentEngine:
    """逐轮驱动模型和工具，整批校验后才允许副作用并公布附件。"""
    def __init__(self, provider):
        self.provider = provider

    async def run(self, prepared, tools):
        """文本与图片分别计预算，工具往返只追加消息而不复制用户图片。"""
        messages = list(prepared.call.messages)
        images = [image for message in messages for image in message.images]
        if len(images) > 8 or sum(image.source_size_bytes for image in images) > 20 * 1024 * 1024:
            raise AgentError("MEDIA_TOO_LARGE", status_code=413)
        seen, total, usage = set(), 0, None
        by_name = {t.name: t for t in tools.definitions}
        role = "\n\n".join(m.text for m in messages if m.role == "system")
        base_messages = [m for m in messages if m.role != "system"]
        for turn_number in range(1, prepared.max_steps + 2):
            final_only = turn_number > prepared.max_steps or not tools.definitions
            prompt = AgentPromptBuilder.build(
                role, prepared.snapshot.skills, tools.definitions, final_only=final_only
            )
            messages = [TurnMessage(role="system", text=prompt), *base_messages]
            if (
                sum(
                    len(
                        bounded_json(
                            {**m.model_dump(), "continuation": m.continuation},
                            maximum=1048576,
                            depth=32,
                        )
                    )
                    for m in messages
                )
                + len(bounded_json([t.model_dump() for t in tools.definitions], maximum=1048576))
                > 120000
            ):
                raise AgentError("AGENT_CONTEXT_TOO_LARGE", status_code=422)
            call = prepared.call.model_copy(
                update={
                    "messages": tuple(messages),
                    "tools": () if final_only else tools.definitions,
                    "final_only": final_only,
                    "timeout_seconds": remaining_timeout(prepared.request.timeout_seconds),
                }
            )
            event_base = {"execution_id": prepared.execution_id, "sequence": 0, "turn": turn_number}
            yield AgentEvent(kind="model_start", **event_base)
            completed = None
            text = ""
            async with aclosing(self.provider.stream_turn(call)) as events:
                async for event in events:
                    if completed is not None:
                        raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
                    if event.kind == "text_delta":
                        text += event.delta or ""
                        if len(text.encode()) > 1048576:
                            raise AgentError("AGENT_MODEL_RESPONSE_TOO_LARGE")
                        yield AgentEvent(kind="text_delta", delta=event.delta, **event_base)
                    elif event.kind == "turn_end":
                        completed = event.turn
            if completed is None or completed.content != text:
                raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
            values = completed.usage.model_dump()
            usage = (
                TokenUsage(
                    **{
                        k: (
                            v + getattr(usage, k)
                            if v is not None and getattr(usage, k) is not None
                            else None
                        )
                        for k, v in values.items()
                    }
                )
                if usage is not None
                else completed.usage
            )
            if not completed.calls:
                if not completed.content.strip():
                    raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
                yield AgentEvent(
                    kind="done",
                    metadata=AgentCompletionMetadata(
                        execution_id=prepared.execution_id,
                        request_id=prepared.request.request_id,
                        model=completed.model,
                        usage=usage,
                        duration_ms=0,
                        record_saved=False,
                    ),
                    **event_base,
                )
                return
            if final_only or len(completed.calls) > 8 or total + len(completed.calls) > 40:
                raise AgentError("AGENT_TOOL_LIMIT", status_code=422)
            # Validate the entire batch before any side effect.
            for tool_call in completed.calls:
                if tool_call.call_id in seen or tool_call.name not in by_name:
                    raise AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403)
                seen.add(tool_call.call_id)
                ToolSchema.validate_arguments(by_name[tool_call.name], tool_call.arguments)
                tools.validate(tool_call)
            base_messages.append(
                TurnMessage(
                    role="assistant",
                    text=completed.content,
                    tool_calls=completed.calls,
                    continuation=completed.continuation,
                )
            )
            for tool_call in completed.calls:
                remaining_timeout(prepared.request.timeout_seconds)
                started = time.monotonic()
                yield AgentEvent(
                    kind="tool_start", tool=tool_call.name, call_id=tool_call.call_id, **event_base
                )
                result = await tools.call(tool_call)
                if result.call_id != tool_call.call_id or result.name != tool_call.name:
                    raise AgentError("AGENT_TOOL_RESULT_INVALID")
                base_messages.append(TurnMessage(role="tool", tool_result=result))
                total += 1
                yield AgentEvent(
                    kind="tool_result",
                    tool=tool_call.name,
                    call_id=tool_call.call_id,
                    code="AGENT_TOOL_FAILED" if result.is_error else None,
                    message="工具执行失败" if result.is_error else "工具执行完成",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    **event_base,
                )
                if prepared.authority.entry == "admin":
                    for attachment in result.attachments:
                        yield AgentEvent(kind="artifact", attachment=attachment, **event_base)
        raise AgentError("AGENT_TOOL_LIMIT", status_code=422)
