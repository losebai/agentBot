"""真实 QQ 会话可查询脱敏配置，仅机器人主人可以修改配置。"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_bot.admin.models import SettingsSnapshot, SettingsUpdate
from agent_bot.admin.settings import SettingsService
from agent_bot.database_pool import finish_database_write
from agent_bot.services.chat_tools import ChatToolContext, ChatToolError
from agent_bot.settings import SettingsError, SettingsRevisionConflictError, SettingsValidationError

BOT_CONFIG_TOOLS = frozenset({"bot_config_get", "bot_config_update"})
# 只有固定业务提示可跨 Chat MCP 桥公开；不透传数据库、文件或校验异常正文。
CONFIG_TOOL_ERROR_MESSAGES = {
    "FORBIDDEN": "查询配置需要 QQ 会话，修改配置仅限机器人主人。",
    "CONFIG_CONFLICT": "配置已被修改，请重新查询版本后提交，不要直接重试旧修改。",
    "CONFIG_INVALID": "配置字段或值无效；只能修改查询结果中可编辑的非密钥字段。",
    "CONFIG_UNAVAILABLE": "配置服务不可用，请检查数据库或启动文件权限。",
    "RESULT_TOO_LARGE": "配置值超过聊天工具展示上限，请在管理页面查看。",
}


class BotConfigGetArguments(BaseModel):
    """按字段或用途筛选分页，不接受模型指定用户或配置文件路径。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    names: list[str] = Field(default_factory=list, max_length=20)
    query: str = Field(default="", max_length=200)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=10, ge=1, le=20)


class BotConfigUpdateArguments(BaseModel):
    """保留稀疏更新，实际字段和跨字段约束统一交给配置服务。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    expected_revision: int = Field(ge=1)
    startup_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    values: dict[str, Any] = Field(min_length=1, max_length=20)


def _error(code: str) -> ChatToolError:
    """公开提示只取服务端常量，不拼接原始参数或上游错误。"""
    return ChatToolError(code, CONFIG_TOOL_ERROR_MESSAGES[code])


class BotConfigTools:
    """复用管理配置服务，查询绑定 QQ 会话，修改还须核对当前主人名单。"""

    def __init__(self, settings: SettingsService) -> None:
        """宿主显式注入同一配置服务，不读取第二份启动环境或创建数据库。"""
        self.settings = settings

    def _authorize(self, context: ChatToolContext, *, require_owner: bool = True) -> None:
        """查询不限制主人，修改每次复核当前名单，避免旧令牌保留已撤销权限。"""
        context.ensure_active()
        if (
            not context.bot_user_id
            or (require_owner and context.user_id not in self.settings.runtime.config.superusers)
        ):
            raise _error("FORBIDDEN")

    async def get(self, arguments: dict[str, object], context: ChatToolContext) -> dict:
        """向已授权 QQ 会话提供脱敏快照；秘密只展示设置状态，不限制主人。"""
        self._authorize(context, require_owner=False)
        args = BotConfigGetArguments.model_validate(arguments)
        try:
            snapshot = await asyncio.to_thread(self.settings.read)
        except SettingsError as error:
            raise _error("CONFIG_UNAVAILABLE") from error
        names = set(args.names)
        if names - {field.name for field in snapshot.fields}:
            raise _error("CONFIG_INVALID")
        query = args.query.strip().casefold()
        fields = [field for field in snapshot.fields
                  if (not names or field.name in names)
                  and (not query or query in field.name.casefold()
                       or query in field.description.casefold())]
        page = fields[args.offset:args.offset + args.limit]
        return {
            "database_revision": snapshot.database_revision,
            "applied_revision": snapshot.applied_revision,
            "startup_revision": snapshot.startup_revision,
            "restart_required_fields": snapshot.restart_required_fields,
            "fields": [field.model_copy(update={
                "read_only": field.read_only or field.kind == "secret",
            }).model_dump(mode="json") for field in page],
            "total": len(fields),
            "next_offset": (args.offset + len(page)
                            if args.offset + len(page) < len(fields) else None),
        }

    async def update(self, arguments: dict[str, object], context: ChatToolContext) -> dict:
        """先校验完整草稿，再提交事务；已开始的写入在取消时也必须完成收尾。"""
        self._authorize(context)
        try:
            args = BotConfigUpdateArguments.model_validate(arguments)
            snapshot = await asyncio.to_thread(self.settings.read)
            editable = {field.name for field in snapshot.fields
                        if not field.read_only and field.kind != "secret"}
            if not set(args.values) <= editable:
                raise _error("CONFIG_INVALID")
            request = SettingsUpdate.model_validate(args.model_dump(exclude_none=True))

            def save() -> SettingsSnapshot:
                """写入前再验证截止时间和身份，不在聊天租约内等待热刷新。"""
                self._authorize(context)
                return self.settings.update(request)

            saved = await finish_database_write(save)
        except SettingsRevisionConflictError as error:
            raise _error("CONFIG_CONFLICT") from error
        except (ValidationError, SettingsValidationError) as error:
            raise _error("CONFIG_INVALID") from error
        except SettingsError as error:
            raise _error("CONFIG_UNAVAILABLE") from error
        # 响应只报告变更字段和版本，避免大配置在提交成功后因结果超限被误报为失败。
        return {
            "changed_fields": sorted(args.values),
            "database_revision": saved.database_revision,
            "applied_revision": saved.applied_revision,
            "startup_revision": saved.startup_revision,
            "restart_required_fields": saved.restart_required_fields,
            "message": ("配置已保存；需重启字段请手动重启后端生效，其余字段等待自动刷新。"
                        if saved.restart_required_fields else "配置已保存，等待自动刷新生效。"),
        }


async def bot_config_get(arguments: dict[str, object], context: ChatToolContext) -> dict:
    """只使用当前 QQ 会话绑定的配置工具适配器。"""
    if context.bot_config_tools is None:
        raise _error("CONFIG_UNAVAILABLE")
    return await context.bot_config_tools.get(arguments, context)


async def bot_config_update(arguments: dict[str, object], context: ChatToolContext) -> dict:
    """调用受控配置保存入口，不允许参数替换身份或配置后端。"""
    if context.bot_config_tools is None:
        raise _error("CONFIG_UNAVAILABLE")
    return await context.bot_config_tools.update(arguments, context)
