"""Chat Completions 图文消息及按索引拼接的增量工具参数。"""

import json

from agent_bot.agent.contracts import ModelEvent
from agent_bot.agent.errors import AgentError
from agent_bot.ai.providers.agent_base import AgentProviderBase, checked_turn, tool_call
from agent_bot.ai.providers.base import json_object, usage
from agent_bot.ai.providers.chat_completions import ChatCompletionsProvider
from agent_bot.ai.providers.parsing import parse_sse


class AgentChatProvider(AgentProviderBase):
    """适配 Chat Completions 的多轮文本、图片和工具调用。"""
    path = "/chat/completions"
    options = ChatCompletionsProvider.options

    def payload(self, call, stream):
        """只在协议边界编码私有图片，纯文本负载保持原格式。"""
        messages = []
        for message in call.messages:
            item = {"role": message.role, "content": message.text}
            if message.images:
                item["content"] = [
                    {"type": "text", "text": message.text},
                    *[
                        {
                            "type": "image_url",
                            "image_url": {"url": image.data_url()},
                        }
                        for image in message.images
                    ],
                ]
            if message.tool_result:
                item.update(
                    content=message.tool_result.content, tool_call_id=message.tool_result.call_id
                )
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": t.call_id,
                        "type": "function",
                        "function": {
                            "name": t.name,
                            "arguments": json.dumps(t.arguments, ensure_ascii=False),
                        },
                    }
                    for t in message.tool_calls
                ]
            messages.append(item)
        result = {
            "model": call.resolved.model.upstream_model,
            "messages": messages,
            "stream": stream,
            **self.mapped_options(call),
        }
        if stream:
            result["stream_options"] = {"include_usage": True}
        if call.final_only:
            result["tool_choice"] = "none"
        elif call.tools:
            result["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                        "strict": False,
                    },
                }
                for t in call.tools
            ]
        return result

    def result(self, value, call):
        if len(value["choices"]) != 1:
            raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
        choice = value["choices"][0]
        message = choice["message"]
        if message.get("role") != "assistant" or choice["finish_reason"] not in {
            "stop",
            "tool_calls",
        }:
            raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
        calls = []
        for item in message.get("tool_calls") or []:
            if item.get("type") != "function":
                raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
            calls.append(
                tool_call(item["id"], item["function"]["name"], item["function"]["arguments"])
            )
        if bool(calls) != (choice["finish_reason"] == "tool_calls"):
            raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
        return checked_turn(
            call,
            content=message.get("content") or "",
            calls=calls,
            model=value.get("model"),
            usage=usage(value.get("usage")),
        )

    async def parse(self, chunks, call):
        text, model, final, stats, calls = "", None, None, None, {}
        async for data in parse_sse(chunks):
            if data == "[DONE]":
                if final is None:
                    break
                value = {
                    "model": model,
                    "usage": stats,
                    "choices": [
                        {
                            "finish_reason": final,
                            "message": {
                                "role": "assistant",
                                "content": text,
                                "tool_calls": [calls[i] for i in sorted(calls)],
                            },
                        }
                    ],
                }
                yield ModelEvent(kind="turn_end", turn=self.result(value, call))
                return
            value = json_object(data)
            model = value.get("model") or model
            stats = value.get("usage") or stats
            choices = value.get("choices", [])
            if not choices:
                continue
            if len(choices) != 1 or choices[0].get("index", 0) != 0 or final is not None:
                raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
            choice = choices[0]
            delta = choice.get("delta", {})
            if delta.get("content") is not None:
                event = self.delta(delta["content"])
                text += event.delta
                yield event
            for part in delta.get("tool_calls") or []:
                index = part["index"]
                if type(index) is not int or not 0 <= index < 8:
                    raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
                item = calls.setdefault(
                    index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                if part.get("type", "function") != "function":
                    raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
                if part.get("id"):
                    if item["id"] and part["id"] != item["id"]:
                        raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
                    item["id"] = part["id"]
                for key in ("name", "arguments"):
                    item["function"][key] += part.get("function", {}).get(key) or ""
                    if len(item["function"][key].encode()) > 65536:
                        raise AgentError("AGENT_MODEL_RESPONSE_TOO_LARGE")
            final = choice.get("finish_reason")
        raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
