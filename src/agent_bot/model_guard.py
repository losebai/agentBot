"""本地模型目录预检的错误分类。"""

from agent_bot.ai.chat_errors import ChatAiError

MODEL_CONFIGURATION_ERROR_MESSAGE = (
    "模型未配置或未启用，请在管理页面登记并启用模型及其供应商。"
)
_MODEL_CONFIGURATION_CODES = frozenset({
    "AI_MODEL_NOT_FOUND", "AI_MODEL_DISABLED", "AI_PROVIDER_DISABLED", "AGENT_MODEL_UNAVAILABLE",
})


class ModelNotAvailableError(RuntimeError):
    """配置的模型未登记或未启用。"""


class ModelGuardUnavailableError(ModelNotAvailableError):
    """模型目录暂时无法查询，可由持久队列安全重试。"""


def model_configuration_error_code(error: Exception) -> str | None:
    """只识别明确的模型配置拒绝；目录故障和上游拒绝仍沿用各自处理策略。"""
    if isinstance(error, ModelGuardUnavailableError):
        return None
    if isinstance(error, ModelNotAvailableError):
        return "MODEL_NOT_AVAILABLE"
    if isinstance(error, ChatAiError) and error.error_code in _MODEL_CONFIGURATION_CODES:
        return error.error_code
    return None
