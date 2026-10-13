"""MCP 仅使用数据库服务快照中的访问令牌。"""

from agent_bot.agent.errors import AgentError


class McpCredentials:
    """每个 MCP 服务独立提供凭据，不读取环境变量或文件。"""

    def resolve(self, server):
        """要求鉴权但尚未录入时明确失败。"""
        if not server.credential_available:
            raise AgentError("AGENT_CREDENTIAL_MISSING", status_code=503)
        return server.credential

    def is_available(self, server):
        """只暴露已配置状态，不访问远端。"""
        return server.credential_available
