"""管理端只可暂停或重置演化，不能伪造互动证据和身份。"""

from pydantic import BaseModel, ConfigDict, Field


class EvolutionConfigUpdate(BaseModel):
    """零版本代表尚未显式配置，更新仍需与最新版本一致。"""

    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0)
    enabled: bool


class EvolutionReset(BaseModel):
    """重置只作用于明确选择的当前画像版本。"""

    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1)
