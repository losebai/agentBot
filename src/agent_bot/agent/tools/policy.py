"""按调用入口求配置能力与授权的交集，区分 QQ 专属及共享媒体工具。"""

from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.schema import ToolSchema

ADMIN_MEDIA_TOOLS = frozenset(
    {"local_workflow_list", "local_workflow_generate_image", "tts_generate"}
)
QQ_ONLY_TOOLS = frozenset(
    {
        "memory",
        "naraka_query",
        "naraka_search",
        "mention_member",
        "local_workflow_tasks",
        "local_workflow_cancel",
        "job",
    }
)
MEDIA_TOOLS = ADMIN_MEDIA_TOOLS | {"local_workflow_tasks", "local_workflow_cancel"}
CHAT_TOOLS = (QQ_ONLY_TOOLS - {"job"}) | ADMIN_MEDIA_TOOLS
LEGACY_TOOLS = frozenset({"soloncode"})


class ToolPolicy:
    """根据入口、请求和配置快照收紧工具授权，禁止跨入口使用能力。"""
    @staticmethod
    def select(*, configured, requested, authority, available):
        """管理端仅排除 QQ 专属工具，共享媒体仍须显式授权。"""
        by_name = {item.name: item for item in available}
        if len(by_name) != len(available):
            raise AgentError("AGENT_TOOL_NAME_CONFLICT", status_code=409)
        allowed = set(configured) & set(authority.allowed_tools)
        if authority.entry == "admin":
            allowed -= QQ_ONLY_TOOLS
        selected = allowed if requested is None else set(requested)
        if not selected <= allowed:
            raise AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403)
        if not selected <= by_name.keys():
            raise AgentError("AGENT_TOOL_UNAVAILABLE", status_code=503)
        result = tuple(by_name[name] for name in sorted(selected))
        for tool in result:
            ToolSchema.validate_definition(tool)
        return result
