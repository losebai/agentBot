"""Ollama 图文工具轮次；私有思考只保留于内存延续信息。"""

import base64
import uuid

from agent_bot.agent.contracts import ModelEvent
from agent_bot.agent.errors import AgentError
from agent_bot.ai.providers.agent_base import AgentProviderBase, checked_turn, tool_call
from agent_bot.ai.providers.base import json_object
from agent_bot.ai.providers.ollama import OllamaProvider
from agent_bot.ai.providers.parsing import parse_ndjson


class AgentOllamaProvider(AgentProviderBase):
    """适配 Ollama 图片数组与多轮工具消息，不向日志公开图片内容。"""
    path = "/api/chat"
    options = OllamaProvider.options

    def payload(self, call, stream):
        """Ollama 图片使用不带 data URL 前缀的 Base64 数组。"""
        messages = []
        for message in call.messages:
            if message.continuation:
                messages.extend(message.continuation)
                continue
            item = {"role": message.role, "content": message.text}
            if message.images:
                item["images"] = [
                    base64.b64encode(image.data).decode("ascii") for image in message.images
                ]
            if message.tool_result:
                item.update(content=message.tool_result.content, tool_name=message.tool_result.name)
            if message.tool_calls:
                item["tool_calls"] = [
                    {"function": {"name": t.name, "arguments": t.arguments}}
                    for t in message.tool_calls
                ]
            messages.append(item)
        options = self.mapped_options(call)
        think = options.pop("think", None)
        result = {
            "model": call.resolved.model.upstream_model,
            "messages": messages,
            "stream": stream,
            "options": options,
        }
        if think is not None:
            result["think"] = think
        if call.tools and not call.final_only:
            result["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in call.tools
            ]
        return result

    def result(self, value, call):
        if (
            value.get("done") is not True
            or "error" in value
            or value.get("done_reason", "stop") != "stop"
        ):
            raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
        message = value["message"]
        if message.get("role") != "assistant":
            raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
        calls = [
            tool_call(uuid.uuid4().hex, item["function"]["name"], item["function"]["arguments"])
            for item in message.get("tool_calls") or []
        ]
        return checked_turn(
            call,
            content=message.get("content", ""),
            calls=calls,
            model=value.get("model"),
            usage=OllamaProvider.stats(self, value),
            continuation=(message,),
        )

    async def parse(self, chunks, call):
        text, thinking, calls = "", "", []
        async for data in parse_ndjson(chunks):
            value = json_object(data)
            if "error" in value:
                raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
            message = value.get("message", {})
            if message.get("content"):
                event = self.delta(message["content"])
                text += event.delta
                yield event
            thinking += message.get("thinking", "")
            calls.extend(message.get("tool_calls") or [])
            if len(calls) > 8:
                raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
            if value.get("done") is True:
                value["message"] = {
                    "role": "assistant",
                    "content": text,
                    "thinking": thinking,
                    "tool_calls": calls,
                }
                yield ModelEvent(kind="turn_end", turn=self.result(value, call))
                return
        raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
