"""管理端可研究既有反馈并手动应用，不能伪造互动证据、身份或任意档位。"""

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


class EvolutionAdvance(EvolutionReset):
    """手动执行绑定只读预览的证据摘要，禁止网页提交任意档位或伪造反馈。"""

    preview_key: str = Field(pattern=r'^[a-f0-9]{64}$')
