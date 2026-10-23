"""工作流管理只公开业务元数据，原始 JSON 在详情接口单独读取。"""

from typing import Literal

from pydantic import Field

from agent_bot.admin.models import ApiModel
from agent_bot.ai.errors import AiError
from agent_bot.services.workflows import MAX_WORKFLOW_FILE_BYTES, WorkflowMetadataPatch


class WorkflowManagementError(AiError):
    """供 HTTP 边界使用的稳定工作流错误代码。"""


class WorkflowSummary(ApiModel):
    """包含执行状态和完整内容版本，不在列表重复传输大体积 JSON。"""

    workflow_key: str
    display_name: str
    source_file: str
    source_format: Literal["ui", "api"]
    content_sha256: str
    revision: str
    enabled: bool
    executable: bool
    description: str
    additional_positive_prompt: str
    additional_negative_prompt: str
    workflow_type: Literal["text_to_image", "image_to_image", "fixed_reference", "image_to_video"]
    positive_node_id: str | None
    negative_node_id: str | None
    output_node_id: str | None
    seed_node_id: str | None
    input_image_node_id: str | None
    positive_input_name: str
    negative_input_name: str


class WorkflowDetail(WorkflowSummary):
    """仅显式打开详情时返回原始画布和可执行 API 模板。"""

    ui_json: str | None
    api_json: str | None


class WorkflowCatalog(ApiModel):
    """默认选择使用机器人配置版本，工作流编辑使用独立记录版本。"""

    items: list[WorkflowSummary]
    default_key: str
    settings_revision: int
    directory_configured: bool
    next_cursor: str | None


class WorkflowImport(ApiModel):
    """客户端提交内容和展示文件名，不能指定服务器的文件路径。"""

    filename: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=MAX_WORKFLOW_FILE_BYTES)


class WorkflowImportResult(ApiModel):
    """明确返回本次同步记录数，零记录也是有效结果。"""

    imported: int


class WorkflowUpdate(ApiModel):
    """只修改业务属性，完整摘要版本防止覆盖导入更新。"""

    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    values: WorkflowMetadataPatch


class WorkflowDefaultUpdate(ApiModel):
    """默认工作流变更与其他机器人配置共享乐观锁。"""

    expected_revision: int = Field(ge=1)
