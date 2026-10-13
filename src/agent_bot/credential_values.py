"""数据库凭据的输入、写入和仅公开状态的快照边界。"""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field, SecretStr, computed_field

from agent_bot.ai.errors import AiError


def validate_credential(value: SecretStr) -> SecretStr:
    """拒绝无效 HTTP 凭据，错误不包含输入内容。"""
    raw = value.get_secret_value()
    if not raw.strip() or len(raw) > 4096 or any(c in raw for c in "\r\n\0"):
        raise ValueError("invalid credential")
    if raw == "**********":
        raise ValueError("display mask is not a credential")
    return value


CredentialValue = Annotated[SecretStr, AfterValidator(validate_credential)]


class CredentialSnapshot(BaseModel):
    """内部保留一次请求的密钥，公开响应只包含状态。"""

    credential: CredentialValue | None = Field(default=None, exclude=True, repr=False)
    credential_required: bool = Field(default=False, exclude=True, repr=False)

    @computed_field
    @property
    def credential_set(self) -> bool:
        """表示数据库中已存凭据，不泄露值或长度。"""
        return self.credential is not None

    @computed_field
    @property
    def credential_available(self) -> bool:
        """明确无需鉴权也可用，旧引用待录入不可用。"""
        return self.credential_set or not self.credential_required


def write_credential(values: dict, body, previous=None) -> None:
    """省略保留、非空替换、显式 null 清除，同事务写入鉴权要求。"""
    if previous is not None and "credential" not in body.model_fields_set:
        secret, required = previous.credential, previous.credential_required
    else:
        secret = body.credential
        required = secret is not None
    values["credential"] = secret.get_secret_value() if secret is not None else None
    values["credential_required"] = required


def row_values(row: dict, model) -> dict:
    """只投影当前契约字段，旧环境引用列保留在数据库中但不参与运行。"""
    # 只有完整的新表结构才能判断无需鉴权，不能用默认值解释未迁移的旧表。
    if (
        issubclass(model, CredentialSnapshot)
        and not {"credential", "credential_required"} <= row.keys()
    ):
        raise AiError("AI_NOT_INITIALIZED", status_code=503)
    return {key: value for key, value in row.items() if key in model.model_fields}
