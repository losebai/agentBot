"""当前可信入口和会话绑定的素材检索工具。"""

from agent_bot.agent.contracts import ToolDefinition, ToolResult
from agent_bot.agent.tools.local_media import project_local_schema, validate_local_call
from agent_bot.materials.contracts import MATERIAL_TOOL_DESCRIPTION, MaterialSearchArguments


class MaterialSearchTool:
    """只返回元数据和真实 ID，不返回本地路径或视觉输入。"""

    definition = ToolDefinition(
        name="material_search", source="builtin", description=MATERIAL_TOOL_DESCRIPTION,
        input_schema=project_local_schema(
            "material_search", MaterialSearchArguments.model_json_schema(),
        ),
    )

    def __init__(self, store, owner):
        """归属由执行作用域注入，模型参数不能覆盖。"""
        self.store, self.owner = store, owner

    async def call(self, call) -> ToolResult:
        """检索失败沿用宿主明确错误，不能伪造或扩大素材范围。"""
        validate_local_call(call)
        result = await self.store.search(
            self.owner, MaterialSearchArguments.model_validate(call.arguments),
        )
        return ToolResult(call_id=call.call_id, name=call.name, content=result.model_dump_json())
