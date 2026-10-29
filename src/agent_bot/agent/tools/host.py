"""向管理端和真实 QQ 主人提供经过 Agent 显式授权的本机工具。"""

from __future__ import annotations

import asyncio
import errno
import json
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from agent_bot.agent.contracts import (
    AgentRequest,
    ExecutionAuthority,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.host_contracts import HOST_TOOLS, BashArguments, LsArguments
from agent_bot.services.chat_tools import ChatToolError
from agent_bot.services.request_deadline import remaining_timeout

if TYPE_CHECKING:
    from agent_bot.agent.tools.chat import ChatToolBridge
    from agent_bot.services.host_commands import HostCommandRunner


def _directory(root: Path, value: str) -> Path:
    """相对路径从服务工作目录解析；不把目录解析伪装成文件系统沙箱。"""
    path = Path(value)
    resolved = (path if path.is_absolute() else root / path).resolve(strict=True)
    if not resolved.is_dir():
        raise NotADirectoryError(str(resolved))
    return resolved


def _listing(root: Path, args: LsArguments) -> dict:
    """仅扫描到所需分页末尾，避免列目录时读取文件正文或收集整个目录。"""
    path = _directory(root, args.path)
    entries, visible, more = [], 0, False
    with os.scandir(path) as iterator:
        for entry in iterator:
            if not args.include_hidden and entry.name.startswith("."):
                continue
            info = entry.stat(follow_symlinks=False)
            if not args.include_hidden and getattr(info, "st_file_attributes", 0) & 2:
                continue
            if visible < args.offset:
                visible += 1
                continue
            if len(entries) == args.limit:
                more = True
                break
            entries.append({
                "name": entry.name,
                "type": ("symlink" if entry.is_symlink() else "directory"
                         if entry.is_dir(follow_symlinks=False) else "file"),
                "size_bytes": info.st_size,
                "modified_at": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
            })
    return {"path": str(path), "entries": entries, "offset": args.offset,
            "next_offset": args.offset + len(entries) if more else None,
            "has_more": more, "truncated": False}


class HostTools:
    """保持真实主人提供器和工作目录，不修改部署配置或自动授权任何 Agent。"""

    allowed_tools = HOST_TOOLS

    def __init__(self, superusers_provider: Callable[[], set[str]], *,
                 root_directory: Path | None = None,
                 runner: HostCommandRunner | None = None) -> None:
        """允许测试注入执行器；生产只使用后端启动账户与当前配置的主人身份。"""
        if runner is None:
            from agent_bot.services.host_commands import HostCommandRunner

            runner = HostCommandRunner()
        self.superusers_provider = superusers_provider
        self.root_directory = (root_directory or Path.cwd()).resolve()
        self.runner = runner

    def bind(self, request: AgentRequest, authority: ExecutionAuthority,
             chat_bridge: ChatToolBridge | None) -> HostToolSession:
        """绑定当前请求，定义与执行均继续受到 ToolPolicy 的授权交集约束。"""
        return HostToolSession(self, request, authority, chat_bridge)


class HostToolSession:
    """每次操作重新检查 QQ 会话与主人，不接受模型传入的身份断言。"""

    def __init__(self, owner: HostTools, request: AgentRequest, authority: ExecutionAuthority,
                 bridge: ChatToolBridge | None) -> None:
        """把实际终端写入工具说明，让模型在 Windows 使用 PowerShell 语法。"""
        self.owner, self.request, self.authority, self.bridge = owner, request, authority, bridge
        self.definitions = (
            ToolDefinition(name="ls", source="builtin",
                           input_schema=LsArguments.model_json_schema(),
                           description="列出本机目录的文件元数据，按原生目录顺序分页，不读取文件正文。"
                                       "path 支持绝对路径；相对路径从服务工作目录解析。"
                                       "仅管理端或真实 QQ 主人可用，且需 Agent 显式授权。"),
            ToolDefinition(name="bash", source="builtin",
                           input_schema=BashArguments.model_json_schema(), description=(
                               f"执行本机 {owner.runner.platform_name} 的 "
                               f"{owner.runner.shell_name} 命令。"
                               "Windows 必须使用 PowerShell 语法，Linux 使用 Bash 语法。"
                               "cwd 支持绝对路径，相对路径从服务工作目录解析；每次调用独立，"
                               "不继承上次 cd。命令以服务账户权限运行，不要启动常驻后台进程。"
                               "仅管理端或真实 QQ 主人可用，且需 Agent 显式授权。"
                           )),
        )

    async def _authorize(self, name: str) -> None:
        """QQ 令牌必须来自同一会话和当前主人；管理身份由既有管理 API 签发。"""
        if name not in HOST_TOOLS or name not in self.authority.allowed_tools:
            raise AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403)
        if self.authority.entry == "admin":
            return
        if self.bridge is None or self.authority.chat_token is None:
            raise AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403)
        context = await self.bridge.context(self.authority)
        try:
            context.ensure_active()
        except ChatToolError as error:
            raise AgentError("AGENT_TOOL_SESSION_EXPIRED", status_code=403) from error
        if (context.session_id != self.request.session_id or not context.bot_user_id
                or name not in context.allowed_tools
                or context.user_id not in self.owner.superusers_provider()):
            raise AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403)

    async def call(self, call: ToolCall) -> ToolResult:
        """先复核身份再访问文件或执行命令，失败形成明确结果且不自动重试。"""
        await self._authorize(call.name)
        try:
            model = LsArguments if call.name == "ls" else BashArguments
            args = model.model_validate(call.arguments)
        except ValidationError as error:
            raise AgentError("AGENT_TOOL_ARGUMENTS_INVALID", status_code=422) from error
        try:
            if isinstance(args, LsArguments):
                value = await asyncio.to_thread(_listing, self.owner.root_directory, args)
                content = json.dumps(value, ensure_ascii=False)
                # 文件名转义也计入工具正文预算，后续分页从实际返回的最后一项继续。
                while len(content) > 32000 and value["entries"]:
                    value["entries"].pop()
                    value.update(has_more=True, truncated=True,
                                 next_offset=args.offset + len(value["entries"]))
                    content = json.dumps(value, ensure_ascii=False)
                return ToolResult(call_id=call.call_id, name=call.name, content=content)
            cwd = await asyncio.to_thread(_directory, self.owner.root_directory, args.cwd)
            try:
                result = await self.owner.runner.run(
                    args.command, cwd, remaining_timeout(args.timeout_seconds),
                )
            except FileNotFoundError:
                return ToolResult(call_id=call.call_id, name=call.name, is_error=True,
                                  content=json.dumps({"error": {
                                      "code": "HOST_SHELL_UNAVAILABLE",
                                      "message": "未找到本机终端，请检查 PowerShell 或 Bash 安装。",
                                  }}, ensure_ascii=False))
            value = {"cwd": str(cwd), "platform": self.owner.runner.platform_name,
                     "shell": self.owner.runner.shell_name, "stdout": result.stdout,
                     "stderr": result.stderr, "exit_code": result.exit_code,
                     "timed_out": result.timed_out, "truncated": result.truncated}
            content = json.dumps(value, ensure_ascii=False)
            # 控制字符在 JSON 中会膨胀，不能只依赖执行器的原始字符限制。
            while len(content) > 32000 and (value["stdout"] or value["stderr"]):
                value.update(stdout=value["stdout"][:len(value["stdout"]) // 2],
                             stderr=value["stderr"][:len(value["stderr"]) // 2], truncated=True)
                content = json.dumps(value, ensure_ascii=False)
            return ToolResult(call_id=call.call_id, name=call.name, content=content,
                              is_error=result.timed_out or result.exit_code != 0)
        except (OSError, ValueError) as error:
            code, message = "HOST_OPERATION_FAILED", "本机操作失败，请检查路径、权限及终端安装。"
            if isinstance(error, FileNotFoundError):
                code, message = "HOST_PATH_NOT_FOUND", "指定路径不存在。"
            elif isinstance(error, NotADirectoryError):
                code, message = "HOST_DIRECTORY_REQUIRED", "指定路径必须是目录。"
            elif isinstance(error, PermissionError):
                code, message = "HOST_ACCESS_DENIED", "服务账户没有访问权限。"
            elif isinstance(error, OSError) and error.errno == errno.ENOTSUP:
                code = "HOST_BACKGROUND_UNSUPPORTED"
                message = "不支持脱离进程组并占用输出管道的后台命令，请以前台方式执行。"
            return ToolResult(call_id=call.call_id, name=call.name, is_error=True,
                              content=json.dumps({"error": {"code": code, "message": message}},
                                                 ensure_ascii=False))
