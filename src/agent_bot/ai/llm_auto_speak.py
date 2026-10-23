"""独立模型和提示词的无历史普通调用，只输出是否允许自动发言。"""

import json

from agent_bot.ai.call_origin import auto_speak_scope
from agent_bot.ai.contracts import TextRequest
from agent_bot.ai.errors import AiError
from agent_bot.ai.service import AiService
from agent_bot.models import ChatMessage
from agent_bot.services.auto_speak_decision import AutoSpeakDecision

_OUTPUT_RULE = (
    "根据本次提供的决策提示词判断是否主动发言。"
    '仅返回 JSON 对象 {"choice":"speak"} 或 {"choice":"silent"}，'
    "不添加其他字段、解释或 Markdown。"
)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """拒绝重复键，避免不同 JSON 解析器对发言许可产生歧义。"""
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate decision key")
    return result


def parse_llm_choice(content: str) -> bool:
    """只接受唯一固定选择对象，不从自由文本中恢复或猜测许可。"""
    try:
        if len(content) > 1000:
            raise ValueError("decision response too long")
        value = json.loads(content, object_pairs_hook=_unique_object)
        if (not isinstance(value, dict) or set(value) != {"choice"}
                or value["choice"] not in ("speak", "silent")):
            raise ValueError("invalid decision choice")
    except (ValueError, RecursionError) as error:
        raise AiError("AUTO_SPEAK_RESPONSE_INVALID", status_code=502) from error
    return value["choice"] == "speak"


class LlmAutoSpeakDecisionService:
    """直接使用普通文本服务，不进入聊天路由、Agent、记忆或会话历史。"""

    def __init__(
        self, service: AiService, *, model: str, prompt: str, timeout_seconds: float = 30,
    ) -> None:
        """显式绑定独立模型与系统提示词，不继承正文生成的推理参数。"""
        self.service = service
        self.model = model
        self.prompt = prompt
        self.timeout_seconds = timeout_seconds

    async def decide_auto_speak(
        self, *, state: str, session_id: str, request_id: str,
    ) -> AutoSpeakDecision:
        """仅发送独立提示词；显式丢弃共用接口的上下文，避免意外带入群聊资料。"""
        del state
        choice: bool | None = None

        def validate(content: str) -> None:
            """在普通调用的异常和归档边界内固定判断结果，避免重复解析。"""
            nonlocal choice
            choice = parse_llm_choice(content)

        request = TextRequest(
            model=self.model, prompt=self.prompt, session_id=session_id, request_id=request_id,
            timeout_seconds=self.timeout_seconds,
            messages=(ChatMessage(role="system", content=_OUTPUT_RULE),
                      ChatMessage(role="user", content=self.prompt)),
        )
        async with auto_speak_scope():
            await self.service.complete(request, validate_content=validate)
        assert choice is not None
        return AutoSpeakDecision(should_speak=choice)
