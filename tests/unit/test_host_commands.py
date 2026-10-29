"""通过临时目录和无害命令验证宿主命令的输出与进程所有权。"""

import asyncio
import base64
import errno
import io
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest


class _LinuxPipe(io.BytesIO):
    """提供可控制的描述符，不创建平台相关的真实管道。"""

    def fileno(self):
        """用对象身份区分两路输出，交给非阻塞读取替身解析。"""
        return id(self)


def _patch_linux_io(monkeypatch, module, stdout, stderr, ready=True):
    """替换 OS 边界以验证非阻塞管道协议，保留真实读流和退出逻辑。"""
    streams = {stream.fileno(): stream for stream in (stdout, stderr)}
    signals = []

    def select(readers, writers, errors, timeout):
        """模拟 EOF 已就绪，或脱离组的后代持续占有写端。"""
        if not ready:
            time.sleep(min(timeout, 0.01))
        return (readers if ready else [], [], [])

    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(module, "signal", SimpleNamespace(SIGKILL=9))
    monkeypatch.setattr(module, "select", SimpleNamespace(select=select), raising=False)
    monkeypatch.setattr(module, "os", SimpleNamespace(
        killpg=lambda *args: signals.append(args),
        set_blocking=lambda *args: None,
        read=lambda descriptor, size: streams[descriptor].read(size),
    ))
    return signals


def test_pre_cancelled_worker_does_not_spawn(tmp_path, monkeypatch):
    """排队时已被取消的工作线程不得在获得执行机会后启动命令。"""
    from agent_bot.services import host_commands

    monkeypatch.setattr(host_commands, "sys", SimpleNamespace(platform="linux"))

    def spawn(*args, **kwargs):
        """任何启动都是取消边界失效。"""
        pytest.fail("已取消的命令不应启动进程")

    monkeypatch.setattr(host_commands.subprocess, "Popen", spawn)
    cancelled = threading.Event()
    cancelled.set()
    result = host_commands.HostCommandRunner()._run_sync("touch marker", tmp_path, 1, cancelled)
    assert result.stdout == result.stderr == ""


async def test_linux_detached_pipe_reports_error_with_bounded_cleanup(tmp_path, monkeypatch):
    """壳已退出但后代保留管道时，不可无界等 EOF 或报告正常完成。"""
    from agent_bot.services import host_commands

    class Process:
        """模拟脱离进程组后仍持有管道的后台命令。"""

        pid = 321
        stdout = _LinuxPipe()
        stderr = _LinuxPipe()

        def poll(self):
            """父壳已完成，后台写端始终没有 EOF。"""
            return 0

        def wait(self, timeout=None):
            """父壳可立即回收。"""
            return 0

    _patch_linux_io(monkeypatch, host_commands, Process.stdout, Process.stderr, ready=False)
    monkeypatch.setattr(host_commands.subprocess, "Popen", lambda *args, **kwargs: Process())
    started = time.monotonic()
    with pytest.raises(OSError, match=r"detached.*process group") as error:
        await host_commands.HostCommandRunner().run("setsid background", tmp_path, 1)
    assert error.value.errno == errno.ENOTSUP
    assert time.monotonic() - started < 1.5


@pytest.mark.skipif(sys.platform != "win32", reason="实跑 Windows 挂起取消")
def test_cancel_during_job_assignment_never_runs_command(tmp_path, monkeypatch):
    """创建阶段收到取消时，PowerShell 必须在执行任何用户代码之前被终止。"""
    from agent_bot.services.host_commands import HostCommandRunner, WindowsJob, _WindowsCommand

    cancelled = threading.Event()
    job = WindowsJob()
    original_assign = job.assign
    command = None
    marker = tmp_path / "unexpected.txt"

    def assign(pid):
        """在绑定完成、恢复线程之前确定性地触发取消。"""
        original_assign(pid)
        cancelled.set()

    monkeypatch.setattr(job, "assign", assign)
    try:
        script = f"[IO.File]::WriteAllText('{marker}', 'unexpected')"
        command = _WindowsCommand(script, tmp_path, job, cancelled)
        assert command.wait() != 0
        assert not marker.exists()
    finally:
        HostCommandRunner._finish_job(job)
        if command is not None:
            command.stdout.close()
            command.stderr.close()
            command.owned.close()


@pytest.mark.skipif(sys.platform != "win32", reason="实跑 Windows PowerShell")
async def test_powershell_preserves_unicode_cwd_and_nonzero_exit(tmp_path):
    """编码或工作目录错误时，模型会收到损坏的结果。"""
    from agent_bot.services.host_commands import HostCommandRunner

    (tmp_path / "powershell.exe").write_bytes(b"untrusted local executable")
    runner = HostCommandRunner()
    result = await runner.run(
        "[Console]::Write((Get-Location).Path + '中文🙂'); "
        "[Console]::Error.Write('错误'); exit 7", tmp_path, 10,
    )
    assert runner.shell_name == "powershell"
    assert runner.platform_name == "windows"
    assert result.stdout == str(tmp_path) + "中文🙂"
    assert result.stderr == "错误"
    assert result.exit_code == 7
    assert not result.timed_out
    assert not result.truncated


@pytest.mark.skipif(sys.platform != "win32", reason="实跑 Windows PowerShell")
async def test_output_limit_drains_both_streams(tmp_path):
    """超限后仍排空两个管道，否则大量输出会卡住命令。"""
    from agent_bot.services.host_commands import HostCommandRunner

    result = await HostCommandRunner().run(
        "[Console]::Write(('中' * 90000)); [Console]::Error.Write(('错' * 90000))",
        tmp_path, 10,
    )
    assert len(result.stdout) + len(result.stderr) == 16000
    assert result.truncated
    assert not result.timed_out
    assert result.exit_code == 0


@pytest.mark.skipif(sys.platform != "win32", reason="实跑 Windows 启动失败")
async def test_missing_system_shell_reports_file_not_found(tmp_path, monkeypatch):
    """找不到系统壳时保留可辨识异常，不把原生错误变成未知工具故障。"""
    from agent_bot.services.host_commands import HostCommandRunner

    (tmp_path / "System32" / "WindowsPowerShell" / "v1.0").mkdir(parents=True)
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    with pytest.raises(FileNotFoundError) as error:
        await HostCommandRunner().run("Write-Output unused", tmp_path, 1)
    assert error.value.winerror == 2


def _windows_child_command(marker, wait):
    """后台子进程留下 PID，父壳可选择等待以覆盖各个释放入口。"""
    child = base64.b64encode("Start-Sleep -Seconds 60".encode("utf-16-le")).decode()
    command = (
        "$p = Start-Process powershell.exe -WindowStyle Hidden -PassThru "
        f"-ArgumentList '-NoProfile -NonInteractive -EncodedCommand {child}'; "
        f"[IO.File]::WriteAllText('{str(marker).replace(chr(39), chr(39) * 2)}', "
        "[string]$p.Id); "
    )
    return command + ("Start-Sleep -Seconds 60" if wait else "exit 0")


def _wait_for_marker(marker):
    """在工作线程轮询本地文件，不阻塞测试事件循环。"""
    deadline = time.monotonic() + 8
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)


@pytest.mark.skipif(sys.platform != "win32", reason="实跑 Windows Job")
@pytest.mark.parametrize("mode", ["complete", "timeout", "cancel"])
async def test_each_finish_path_releases_only_owned_descendants(tmp_path, mode):
    """父壳退出、超时和取消都必须回收后代，且不能终止无关进程。"""
    import pywintypes
    import win32api
    import win32con
    import win32event

    from agent_bot.services.host_commands import HostCommandRunner

    outside = await asyncio.to_thread(
        subprocess.Popen, [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    marker = tmp_path / "child.pid"
    task = asyncio.create_task(HostCommandRunner().run(
        _windows_child_command(marker, mode != "complete"), tmp_path,
        5 if mode == "timeout" else 10,
    ))
    handle = None
    try:
        await asyncio.to_thread(_wait_for_marker, marker)
        assert marker.exists()
        if mode != "complete":
            handle = win32api.OpenProcess(win32con.SYNCHRONIZE, False, int(marker.read_text()))
        if mode == "cancel":
            task.cancel("用户取消")
            with pytest.raises(asyncio.CancelledError, match="用户取消"):
                await task
        else:
            result = await task
            assert result.timed_out == (mode == "timeout")
        if handle is not None:
            assert win32event.WaitForSingleObject(handle, 0) == win32event.WAIT_OBJECT_0
        else:
            # 正常完成在返回前已关闭 Job；PID 不应再对应一个运行进程。
            try:
                handle = win32api.OpenProcess(
                    win32con.SYNCHRONIZE, False, int(marker.read_text()),
                )
            except pywintypes.error as exc:
                assert exc.winerror == 87
            else:
                assert win32event.WaitForSingleObject(handle, 0) == win32event.WAIT_OBJECT_0
        assert outside.poll() is None
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        if handle is not None:
            handle.Close()
        outside.terminate()
        outside.wait(timeout=8)


async def test_linux_uses_bash_session_and_reclaims_its_group(tmp_path, monkeypatch):
    """跨平台替身检查 Linux 启动协议和对所属进程组的清理。"""
    from agent_bot.services import host_commands

    calls = []

    class Process:
        """模拟已经完成的 Bash，保留真实二进制读取接口。"""

        pid = 321
        stdout = _LinuxPipe("中文".encode())
        stderr = _LinuxPipe(b"failure")

        def poll(self):
            """壳已经退出，但进程组仍需回收。"""
            return 5

        def wait(self, timeout=None):
            """等候接口保持与 Popen 一致。"""
            return 5

    def spawn(command, **kwargs):
        """仅截获宿主边界，不改变 runner 的读流与清理逻辑。"""
        calls.append((command, kwargs))
        return Process()

    signals = _patch_linux_io(monkeypatch, host_commands, Process.stdout, Process.stderr)
    monkeypatch.setattr(host_commands.subprocess, "Popen", spawn)
    runner = host_commands.HostCommandRunner()
    result = await runner.run("printf hello", tmp_path, 1)
    assert runner.shell_name == "bash"
    assert runner.platform_name == "linux"
    assert result.stdout == "中文"
    assert result.stderr == "failure"
    assert result.exit_code == 5
    assert calls[0][0] == ["/bin/bash", "--noprofile", "--norc", "-c", "printf hello"]
    assert calls[0][1]["start_new_session"] is True
    assert calls[0][1]["cwd"] == tmp_path
    assert calls[0][1]["stdin"] == subprocess.DEVNULL
    assert signals == [(321, 9)]
