"""本机目录与命令工具的真实身份、路径、输出及预算边界。"""

import errno
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from agent_bot.agent.contracts import AgentRequest, ExecutionAuthority, ToolCall
from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.host import HostTools
from agent_bot.services.chat_tools import ChatToolContext


def setup_tools(tmp_path, *, entry="admin", context=None, owners=None, result=None):
    """替换命令执行器，目录只访问临时路径，不读取部署配置。"""
    owners = {"owner"} if owners is None else owners
    runner = SimpleNamespace(platform_name="windows", shell_name="PowerShell", run=AsyncMock(
        return_value=result or SimpleNamespace(stdout="中文结果", stderr="", exit_code=0,
                                               timed_out=False, truncated=False),
    ))
    tools = HostTools(lambda: owners, root_directory=tmp_path, runner=runner)
    authority = ExecutionAuthority(entry=entry, allowed_tools=("ls", "bash"),
                                   chat_token=SecretStr("offline") if entry == "qq" else None)
    bridge = SimpleNamespace(context=AsyncMock(return_value=context)) if context else None
    session = tools.bind(AgentRequest(model="offline", prompt="列目录", session_id="s",
                                     request_id="r"), authority, bridge)
    return session, runner


def call(name, **arguments):
    """使用真实工具契约构造本地调用。"""
    return ToolCall(call_id="c", name=name, arguments=arguments)


async def test_ls_lists_native_metadata_with_paging_and_hidden_entries(tmp_path):
    """原生列目录在 Windows/Linux 共用参数，隐藏项需显式请求。"""
    (tmp_path / "目录").mkdir()
    (tmp_path / "中文.txt").write_text("abc", encoding="utf-8")
    (tmp_path / ".hidden").write_text("hidden", encoding="utf-8")
    session, _ = setup_tools(tmp_path)
    first = json.loads((await session.call(call("ls", limit=1))).content)
    result = await session.call(call("ls", offset=first["next_offset"], limit=1))
    second = json.loads(result.content)
    assert first["has_more"] and not second["has_more"]
    assert {row["name"] for row in first["entries"] + second["entries"]} == {"目录", "中文.txt"}
    all_rows = json.loads((await session.call(call("ls", include_hidden=True))).content)["entries"]
    assert {row["name"] for row in all_rows} == {"目录", "中文.txt", ".hidden"}
    assert next(row for row in all_rows if row["name"] == "中文.txt")["size_bytes"] == 3


@pytest.mark.parametrize("path,code", [("missing", "HOST_PATH_NOT_FOUND"),
                                      ("file.txt", "HOST_DIRECTORY_REQUIRED")])
async def test_ls_path_failures_are_explicit(tmp_path, path, code):
    """路径不存在和误传文件均返回可识别错误，不静默退回默认目录。"""
    (tmp_path / "file.txt").write_text("file", encoding="utf-8")
    session, _ = setup_tools(tmp_path)
    result = await session.call(call("ls", path=path))
    assert result.is_error and json.loads(result.content)["error"]["code"] == code


async def test_bash_reports_actual_shell_and_uses_explicit_directory(tmp_path):
    """Windows 名为 bash 的工具执行已说明的 PowerShell，不让模型猜终端语法。"""
    directory = tmp_path / "含 空格目录"
    directory.mkdir()
    session, runner = setup_tools(tmp_path)
    result = await session.call(call("bash", command="Write-Output '中文'", cwd=directory.name,
                                     timeout_seconds=12))
    data = json.loads(result.content)
    assert not result.is_error and data["shell"] == "PowerShell" and data["platform"] == "windows"
    assert data["stdout"] == "中文结果"
    runner.run.assert_awaited_once_with("Write-Output '中文'", directory.resolve(), 12)


@pytest.mark.parametrize("timed_out,exit_code", [(False, 7), (True, -1)])
async def test_bash_failure_preserves_output_and_exit_status(tmp_path, timed_out, exit_code):
    """非零退出和超时不会伪装成功，已获取输出仍交给模型判断。"""
    session, _ = setup_tools(tmp_path, result=SimpleNamespace(
        stdout="部分输出", stderr="失败原因", exit_code=exit_code,
        timed_out=timed_out, truncated=False,
    ))
    result = await session.call(call("bash", command="exit 7"))
    assert result.is_error
    data = json.loads(result.content)
    assert data["stdout"] == "部分输出" and data["exit_code"] == exit_code
    assert data["timed_out"] is timed_out


async def test_bash_result_stays_within_tool_json_budget(tmp_path):
    """控制字符 JSON 转义后仍受总上限约束，不能把超长结果送入下一轮。"""
    session, _ = setup_tools(tmp_path, result=SimpleNamespace(
        stdout="\x01" * 16000, stderr="", exit_code=0, timed_out=False, truncated=False,
    ))
    result = await session.call(call("bash", command="echo output"))
    assert len(result.content) <= 32000
    assert json.loads(result.content)["truncated"] is True


async def test_missing_shell_is_not_reported_as_a_missing_working_directory(tmp_path):
    """工作目录有效但终端不存在时，明确说明安装问题而非自动换终端。"""
    session, runner = setup_tools(tmp_path)
    runner.run.side_effect = FileNotFoundError("missing shell")
    result = await session.call(call("bash", command="echo output"))
    assert result.is_error
    assert json.loads(result.content)["error"]["code"] == "HOST_SHELL_UNAVAILABLE"


async def test_detached_background_command_has_an_explicit_error(tmp_path):
    """不支持的脱离进程组命令不能被误报为路径或安装错误。"""
    session, runner = setup_tools(tmp_path)
    runner.run.side_effect = OSError(errno.ENOTSUP, "detached command")
    result = await session.call(call("bash", command="echo output"))
    assert result.is_error
    assert json.loads(result.content)["error"]["code"] == "HOST_BACKGROUND_UNSUPPORTED"


@pytest.mark.skipif(not (sys.platform == "win32" or sys.platform.startswith("linux")),
                    reason="只验收用户要求的 Windows 和 Linux")
async def test_host_tool_executes_only_a_harmless_native_command_in_temp_directory(tmp_path):
    """贯通实际宿主工具与原生终端，只在测试临时目录输出固定文本。"""
    tools = HostTools(lambda: set(), root_directory=tmp_path)
    request = AgentRequest(model="offline", prompt="测试", session_id="s", request_id="r")
    session = tools.bind(request, ExecutionAuthority(entry="admin", allowed_tools=("bash",)), None)
    command = "[Console]::Write('host-ok')" if sys.platform == "win32" else "printf host-ok"
    result = await session.call(call("bash", command=command))
    data = json.loads(result.content)
    assert not result.is_error and data["exit_code"] == 0
    assert data["stdout"] == "host-ok" and data["cwd"] == str(tmp_path.resolve())


@pytest.mark.parametrize("arguments", [
    {"command": "   "}, {"command": "echo test", "cwd": "\x00"},
    {"command": "echo test", "timeout_seconds": 121},
    {"command": "echo test", "user_id": "owner"},
])
def test_invalid_host_arguments_are_rejected_before_any_tool_side_effect(arguments):
    """整批校验必须拦截本地原始约束，不能等前面的命令执行后才验证后项。"""
    from agent_bot.agent.tools.local_media import validate_local_call

    with pytest.raises(AgentError, match="AGENT_TOOL_ARGUMENTS_INVALID"):
        validate_local_call(call("bash", **arguments))


@pytest.mark.parametrize("user,session_id,allowed,deadline", [
    ("ordinary", "s", frozenset({"ls", "bash"}), None),
    ("owner", "other-session", frozenset({"ls", "bash"}), None),
    ("owner", "s", frozenset(), None),
    ("owner", "s", frozenset({"ls", "bash"}), 0),
])
async def test_qq_requires_current_owner_and_matching_active_session(
    tmp_path, user, session_id, allowed, deadline,
):
    """模型不能借主人姓名、其他会话或过期令牌取得文件和命令权限。"""
    context = ChatToolContext(session_id=session_id, user_id=user, bot_user_id="bot",
                              allowed_tools=allowed, deadline_at=deadline)
    session, runner = setup_tools(tmp_path, entry="qq", context=context)
    with pytest.raises(AgentError):
        await session.call(call("bash", command="echo denied"))
    runner.run.assert_not_awaited()


async def test_qq_rechecks_owner_before_each_tool_call(tmp_path):
    """同一执行内撤销主人权限后，旧工具会话也不能继续访问本机。"""
    owners = {"owner"}
    context = ChatToolContext(session_id="s", user_id="owner", bot_user_id="bot",
                              allowed_tools=frozenset({"ls", "bash"}))
    session, runner = setup_tools(tmp_path, entry="qq", context=context, owners=owners)
    await session.call(call("ls"))
    owners.clear()
    with pytest.raises(AgentError, match="AGENT_TOOL_UNAUTHORIZED"):
        await session.call(call("bash", command="echo denied"))
    runner.run.assert_not_awaited()
