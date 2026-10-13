"""只使用数据库供应商快照中的密钥，禁止环境变量兜底。"""

from pydantic import SecretStr

from agent_bot.ai.errors import AiError


class CredentialResolver:
    """从当前请求的供应商快照读取凭据。"""

    def resolve(self, provider) -> SecretStr | None:
        """旧引用迁移后必须重新录入，不能误作匿名服务。"""
        if not provider.credential_available:
            raise AiError("AI_CREDENTIAL_MISSING", status_code=503)
        return provider.credential

    def is_available(self, provider) -> bool:
        """状态探测只检查数据库快照，不连接上游。"""
        return provider.credential_available
