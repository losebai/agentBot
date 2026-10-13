"""在项目契约与 LangChain 消息之间转换，私有延续不进入输出记录。"""

from langchain.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages import BaseMessage

from agent_bot.agent.contracts import AgentModelCall, ModelTurn, ToolCall, TurnMessage
from agent_bot.agent.errors import AgentError
from agent_bot.ai.contracts import TokenUsage
from agent_bot.ai.providers.langchain_http import strict_object


def to_langchain_messages(messages: tuple[TurnMessage, ...]) -> list[BaseMessage]:
    """图片只编码于模型输入，工具正文保持独立角色和关联 ID。"""
    result = []
    for message in messages:
        if message.role == "system":
            result.append(SystemMessage(content=message.text))
        elif message.role == "user":
            content = message.text
            if message.images:
                content = [
                    {"type": "text", "text": message.text},
                    *[
                        {"type": "image_url", "image_url": {"url": image.data_url()}}
                        for image in message.images
                    ],
                ]
            result.append(HumanMessage(content=content))
        elif message.tool_result is not None:
            tool = message.tool_result
            result.append(
                ToolMessage(
                    content=tool.content,
                    name=tool.name,
                    tool_call_id=tool.call_id,
                    status="error" if tool.is_error else "success",
                )
            )
        elif message.continuation:
            # LangChain 的原始 AIMessage 保留 Responses 推理项及工具 item ID。
            result.append(AIMessage.model_validate(message.continuation[0]))
        else:
            result.append(
                AIMessage(
                    content=message.text,
                    tool_calls=[
                        {
                            "id": item.call_id,
                            "name": item.name,
                            "args": item.arguments,
                            "type": "tool_call",
                        }
                        for item in message.tool_calls
                    ],
                )
            )
    return result


def to_model_turn(message: AIMessage, call: AgentModelCall) -> ModelTurn:
    """只取可见文本及已完整解析的工具调用，拒绝 SDK 容忍的非法参数。"""
    if message.invalid_tool_calls:
        raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
    for raw in message.additional_kwargs.get("tool_calls", []):
        arguments = raw.get("function", {}).get("arguments")
        if isinstance(arguments, str):
            strict_object(arguments)
    for block in message.content if isinstance(message.content, list) else []:
        if isinstance(block, dict) and block.get("type") == "function_call":
            arguments = block.get("arguments")
            if isinstance(arguments, str):
                strict_object(arguments)
    calls = tuple(
        ToolCall(call_id=item["id"], name=item["name"], arguments=item["args"])
        for item in message.tool_calls
    )
    text = str(message.text)
    if (
        len(text.encode()) > 1048576
        or len(calls) > 8
        or len({item.call_id for item in calls}) != len(calls)
        or (call.final_only and calls)
        or (not calls and not text.strip())
    ):
        raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
    stats = message.usage_metadata or {}
    usage = TokenUsage(
        prompt_tokens=stats.get("input_tokens"),
        completion_tokens=stats.get("output_tokens"),
        total_tokens=stats.get("total_tokens"),
    )
    return ModelTurn(
        content=text,
        calls=calls,
        model=message.response_metadata.get("model_name")
        or message.response_metadata.get("model")
        or call.resolved.model.upstream_model,
        usage=usage,
        continuation=(message.model_dump(),),
    )
