"""Responses 图文输入、权威完成输出及私有延续信息。"""

import json

from agent_bot.agent.contracts import ModelEvent
from agent_bot.agent.errors import AgentError
from agent_bot.ai.providers.agent_base import AgentProviderBase, checked_turn, tool_call
from agent_bot.ai.providers.base import json_object, usage
from agent_bot.ai.providers.parsing import parse_sse
from agent_bot.ai.providers.responses import ResponsesProvider


class AgentResponsesProvider(AgentProviderBase):
    """适配 Responses 图片输入及工具往返，保持流式事件约束。"""
    path = "/responses"
    options = ResponsesProvider.options

    def payload(self, call, stream):
        """图片使用 input_image 内容块，原生工具延续按原顺序保留。"""
        inputs = []
        for message in call.messages:
            if message.continuation:
                inputs.extend(message.continuation)
            elif message.tool_result:
                inputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.tool_result.call_id,
                        "output": message.tool_result.content,
                    }
                )
            else:
                if message.images:
                    inputs.append(
                        {
                            "role": message.role,
                            "content": [
                                {"type": "input_text", "text": message.text},
                                *[
                                    {
                                        "type": "input_image",
                                        "image_url": image.data_url(),
                                    }
                                    for image in message.images
                                ],
                            ],
                        }
                    )
                elif message.text:
                    inputs.append({"role": message.role, "content": message.text})
                inputs.extend(
                    {
                        "type": "function_call",
                        "call_id": t.call_id,
                        "name": t.name,
                        "arguments": json.dumps(t.arguments, ensure_ascii=False),
                    }
                    for t in message.tool_calls
                )
        result = {
            "model": call.resolved.model.upstream_model,
            "input": inputs,
            "stream": stream,
            "store": False,
            "include": ["reasoning.encrypted_content"],
            **self.mapped_options(call),
        }
        if call.final_only:
            result["tool_choice"] = "none"
        elif call.tools:
            result["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                    "strict": False,
                }
                for t in call.tools
            ]
        return result

    def result(self, value, call):
        if value.get("status") != "completed":
            raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
        texts, calls = [], []
        for item in value["output"]:
            kind = item.get("type")
            if kind == "function_call":
                calls.append(tool_call(item["call_id"], item["name"], item["arguments"]))
            elif kind == "message" and item.get("role") == "assistant":
                for part in item["content"]:
                    if part.get("type") != "output_text":
                        raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
                    texts.append(part["text"])
            elif kind != "reasoning":
                raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
        return checked_turn(
            call,
            content="".join(texts),
            calls=calls,
            model=value.get("model"),
            usage=usage(value.get("usage"), responses=True),
            continuation=tuple(value["output"]),
        )

    async def parse(self, chunks, call):
        text = ""
        async for data in parse_sse(chunks):
            value = json_object(data)
            kind = value.get("type")
            if kind == "response.output_text.delta":
                event = self.delta(value["delta"])
                text += event.delta
                yield event
            elif kind == "response.completed":
                turn = self.result(value["response"], call)
                if text and text != turn.content:
                    raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
                if not text and turn.content:
                    yield self.delta(turn.content)
                yield ModelEvent(kind="turn_end", turn=turn)
                return
            elif kind in {"response.failed", "response.incomplete", "error"}:
                raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
        raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
