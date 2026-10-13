"""Python 直接调用固定 Laya choice 协议，异常时不给出发言许可。"""

import asyncio
import logging
import math
import time

from mcp import types

from agent_bot.agent.contracts import ToolCall
from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.schema import bounded_json
from agent_bot.ai.errors import AiError
from agent_bot.media.store import finish_cleanup
from agent_bot.models import AutoSpeakDecisionResponse

logger = logging.getLogger(__name__)


def probability(value):
    """拒绝布尔、字符串、非有限数及零到一区间外的概率。"""
    if type(value) not in (int, float) or not 0 <= value <= 1 or not math.isfinite(value):
        raise AgentError("AUTO_SPEAK_RESPONSE_INVALID")
    return float(value)


def parse_prediction(call, result):
    """只接受 structuredContent 中唯一、严格的固定选择答案。"""
    del call
    if (
        not isinstance(result, types.CallToolResult)
        or result.is_error
        or result.structured_content is None
    ):
        raise AgentError("AUTO_SPEAK_RESPONSE_INVALID")
    value = result.structured_content
    text = bounded_json(value, maximum=256000, depth=8)
    if len(text) > 64000 or set(value) != {"model", "answers"} or value["model"] != "multilingual":
        raise AgentError("AUTO_SPEAK_RESPONSE_INVALID")
    answers = value["answers"]
    if not isinstance(answers, list) or len(answers) != 1 or not isinstance(answers[0], dict):
        raise AgentError("AUTO_SPEAK_RESPONSE_INVALID")
    answer = answers[0]
    required = {"id", "type", "choice", "confidence", "probabilities"}
    if (
        not required <= set(answer)
        or set(answer) - required - {"score", "noul"}
        or answer["id"] != "should_speak"
        or answer["type"] != "choice"
        or answer["choice"] not in ("speak", "silent")
        or answer.get("score") is not None
        or answer.get("noul") is not None
    ):
        raise AgentError("AUTO_SPEAK_RESPONSE_INVALID")
    confidence = probability(answer["confidence"])
    items = answer["probabilities"]
    if not isinstance(items, list) or len(items) != 2:
        raise AgentError("AUTO_SPEAK_RESPONSE_INVALID")
    labels, total = set(), 0.0
    for item in items:
        if (
            not isinstance(item, dict)
            or set(item) != {"label", "probability"}
            or item["label"] not in ("speak", "silent")
            or item["label"] in labels
        ):
            raise AgentError("AUTO_SPEAK_RESPONSE_INVALID")
        labels.add(item["label"])
        total += probability(item["probability"])
    if abs(total - 1.0) > 0.002:
        raise AgentError("AUTO_SPEAK_RESPONSE_INVALID")
    return AutoSpeakDecisionResponse(
        shouldSpeak=answer["choice"] == "speak", confidence=confidence, model="multilingual"
    )


def decision_arguments(state):
    """固定服务端问题和候选，不执行 state 中提出的指令。"""
    return {
        "state": state,
        "model": "auto",
        "questions": [
            {
                "id": "should_speak",
                "type": "choice",
                "instructions": "现在是否应该主动发言？",
                "choices": [
                    {
                        "label": "speak",
                        "description": "现在主动发言自然、有明确价值，能够接续当前话题",
                    },
                    {
                        "label": "silent",
                        "description": "上下文不足、话题已结束、成员正在相互交流，或发言会造成打扰",
                    },
                ],
            }
        ],
    }


class AutoSpeakDecisionService:
    """每次固定目录快照并使用一个受限 MCP 连接，不经过聊天模型。"""

    def __init__(self, catalog, connector, records, *, mcp_code, timeout_seconds=30):
        """配置引用来自宿主，网络预算不能超过三百秒。"""
        if not 0 < timeout_seconds <= 300:
            raise ValueError("invalid decision timeout")
        self.catalog, self.connector, self.records = catalog, connector, records
        self.mcp_code, self.timeout_seconds = mcp_code, timeout_seconds

    async def _record(self, session_id, request_id, revision, started, status, response=None):
        """记录故障与分类结果分离，失败不重试分类也不输出敏感正文。"""
        try:
            await self.records.save(
                session_id=session_id,
                request_id=request_id,
                mcp_code=self.mcp_code or "",
                mcp_revision=revision,
                status=status,
                response=response,
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                error_code=None if status == "completed" else "AUTO_SPEAK_UNAVAILABLE",
            )
        except Exception as error:
            logger.warning("自动发言决策记录失败：error=%s", type(error).__name__)

    async def decide_auto_speak(self, *, state, session_id, request_id):
        """输入先校验，异常交给群聊门控层静默处理，取消等连接清理后返回。"""
        if any(
            not isinstance(value, str) or not value.strip() or len(value) > maximum
            for value, maximum in ((state, 8000), (session_id, 256), (request_id, 256))
        ):
            raise AiError("AUTO_SPEAK_INPUT_INVALID", status_code=422)
        started, revision = time.monotonic(), None

        async def execute():
            """只发现指定服务的固定工具，快照在请求内保持一致。"""
            nonlocal revision
            if not self.mcp_code:
                raise AgentError("AUTO_SPEAK_NOT_CONFIGURED", status_code=503)
            server = await self.catalog.decision_mcp(self.mcp_code)
            revision = server.revision
            async with self.connector.open(
                (server,),
                remaining=lambda: max(0, self.timeout_seconds - (time.monotonic() - started)),
                result_converter=parse_prediction,
            ) as connection:
                if len(connection.tools) != 1 or connection.tools[0].name != "laya_predict":
                    raise AgentError("AUTO_SPEAK_TOOL_UNAVAILABLE", status_code=503)
                return await connection.call(
                    ToolCall(
                        call_id=request_id,
                        name="laya_predict",
                        arguments=decision_arguments(state),
                    )
                )

        try:
            response = await asyncio.wait_for(execute(), self.timeout_seconds)
        except asyncio.CancelledError:
            await finish_cleanup(
                self._record(session_id, request_id, revision, started, "cancelled")
            )
            raise
        except Exception as error:
            await self._record(session_id, request_id, revision, started, "failed")
            raise AiError("AUTO_SPEAK_UNAVAILABLE", status_code=503) from error
        await self._record(session_id, request_id, revision, started, "completed", response)
        return response
