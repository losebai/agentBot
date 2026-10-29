"""受控本地媒体与 Job 工具的 schema 投影和原始参数校验。"""

from copy import deepcopy

from pydantic import ValidationError

from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.host_contracts import BashArguments, LsArguments
from agent_bot.jobs.contracts import JobArguments
from agent_bot.materials.contracts import MaterialGetArguments, MaterialSearchArguments
from agent_bot.plugins.bilibili.contracts import TOOL_MODELS
from agent_bot.plugins.stickers.tool_contracts import StickerSearchArguments, StickerSendArguments
from agent_bot.services.bot_config_tools import BotConfigGetArguments, BotConfigUpdateArguments
from agent_bot.services.chat_tools import (
    ImageGenerateArguments,
    TtsGenerateArguments,
    WorkflowCancelArguments,
    WorkflowGenerateArguments,
    WorkflowTasksArguments,
    WorkflowVideoArguments,
    _StrictArguments,
)
from agent_bot.services.tagged_memory import MemoryArguments

ARGUMENT_MODELS = {
    "ls": LsArguments,
    "bash": BashArguments,
    **TOOL_MODELS,
    "sticker_search": StickerSearchArguments,
    "sticker_send": StickerSendArguments,
    "bot_config_get": BotConfigGetArguments,
    "bot_config_update": BotConfigUpdateArguments,
    "image_generate": ImageGenerateArguments,
    "memory": MemoryArguments,
    "material_search": MaterialSearchArguments,
    "material_get": MaterialGetArguments,
    "job": JobArguments,
    "local_workflow_list": _StrictArguments,
    "local_workflow_generate_image": WorkflowGenerateArguments,
    "local_workflow_generate_video": WorkflowVideoArguments,
    "local_workflow_tasks": WorkflowTasksArguments,
    "local_workflow_cancel": WorkflowCancelArguments,
    "tts_generate": TtsGenerateArguments,
}


def project_local_schema(name: str, schema: dict) -> dict:
    """只投影登记过的本地工具，不放宽远程 MCP 的 schema 规则。"""
    projected = deepcopy(schema)
    if name not in ARGUMENT_MODELS:
        return projected

    def visit(value):
        """模型侧移除不支持的本地格式注解，原始参数仍由Pydantic强制校验。"""
        if isinstance(value, dict):
            value.pop("pattern", None)
            # 日期、时区和有效区间由原模型校验，不向远程schema开放format关键字。
            if value.get("format") == "date-time":
                value.pop("format")
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(projected)
    return projected


def validate_local_call(call) -> None:
    """使用原参数类型验证而不执行工具，避免非法后项排在已执行前项之后。"""
    model = ARGUMENT_MODELS.get(call.name)
    if model is not None:
        try:
            model.model_validate(call.arguments)
        except (ValueError, ValidationError) as error:
            raise AgentError("AGENT_TOOL_ARGUMENTS_INVALID", status_code=422) from error
