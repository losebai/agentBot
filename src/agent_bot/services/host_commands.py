"""执行宿主原生命令，仅管理所属 Linux 进程组或 Windows Job。"""

import asyncio
import base64
import codecs
import errno
import os
import select
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from agent_bot.napcat.windows_process import OwnedProcess, WindowsJob

_WINDOWS_SPAWN_LOCK = threading.Lock()


@dataclass(frozen=True)
class CommandResult:
    """保留命令的两路输出、退出状态及超时和截断标记。"""

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    truncated: bool


class _OutputCapture:
    """两个读取线程共享字符预算，超限后继续排空以免阻塞子进程。"""

    def __init__(self) -> None:
        """预算按解码后的字符计数，避免截断 UTF-8 多字节字符。"""
        self.remaining = 16000
        self.truncated = False
        self.lock = threading.Lock()
        self.output: dict[str, list[str]] = {"stdout": [], "stderr": []}
        self.errors: list[Exception] = []

    def read(self, stream: BinaryIO, name: str) -> None:
        """分别排空管道；只保存预算内文本，读取失败由执行线程向上传播。"""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                data = stream.read(4096)
                self._record(name, decoder.decode(data, final=not data))
                if not data:
                    break
        except Exception as exc:
            self.errors.append(exc)

    def read_linux(self, stream: BinaryIO, name: str, finished: threading.Event) -> None:
        """轮询非阻塞管道，组回收后限定收尾时间，拒绝脱离组仍占用管道的命令。"""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        deadline = None
        try:
            descriptor = stream.fileno()
            os.set_blocking(descriptor, False)
            while True:
                if finished.is_set() and deadline is None:
                    deadline = time.monotonic() + 0.3
                if deadline is not None and time.monotonic() >= deadline:
                    raise OSError(
                        errno.ENOTSUP,
                        "Commands with detached background processes retaining pipes "
                        "outside the owned process group are unsupported",
                    )
                readable, _, _ = select.select([descriptor], [], [], 0.025)
                if not readable:
                    continue
                try:
                    data = os.read(descriptor, 4096)
                except BlockingIOError:
                    continue  # 就绪观察和读取之间写端可能变化，下一次轮询重新观察。
                self._record(name, decoder.decode(data, final=not data))
                if not data:
                    break
        except Exception as exc:
            self.errors.append(exc)

    def _record(self, name: str, decoded: str) -> None:
        """共享字符预算，两个管道解码完成后再截断，防止损坏多字节文本。"""
        with self.lock:
            if self.remaining and decoded:
                self.output[name].append(decoded[:self.remaining])
            self.truncated |= len(decoded) > self.remaining
            self.remaining = max(0, self.remaining - len(decoded))


class _WindowsCommand:
    """持有原生 PowerShell 观察句柄与二进制输出管道。"""

    def __init__(
        self, command: str, cwd: Path, job: WindowsJob, cancelled: threading.Event,
    ) -> None:
        """挂起创建，加入 Job 后再恢复线程，防止后代逃逸所有权边界。"""
        # 原生启动的短暂句柄继承窗口互斥，避免并发命令继承彼此的管道。
        with _WINDOWS_SPAWN_LOCK:
            self._start(command, cwd, job, cancelled)

    def _start(
        self, command: str, cwd: Path, job: WindowsJob, cancelled: threading.Event,
    ) -> None:
        """只在创建子进程时开放输出写句柄继承，并在恢复后立即关闭父端。"""
        import msvcrt

        import pywintypes
        import win32api
        import win32con
        import win32event
        import win32file
        import win32pipe
        import win32process

        security = pywintypes.SECURITY_ATTRIBUTES()
        security.bInheritHandle = True
        handles = []
        streams = []
        process = thread = None
        try:
            stdout_read, stdout_write = win32pipe.CreatePipe(security, 0)
            handles.extend((stdout_read, stdout_write))
            stderr_read, stderr_write = win32pipe.CreatePipe(security, 0)
            handles.extend((stderr_read, stderr_write))
            for reader in (stdout_read, stderr_read):
                win32api.SetHandleInformation(reader, win32con.HANDLE_FLAG_INHERIT, 0)
            stdin = win32file.CreateFile(
                "NUL", win32con.GENERIC_READ,
                win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                security, win32con.OPEN_EXISTING, 0, None,
            )
            handles.append(stdin)
            startup = win32process.STARTUPINFO()
            startup.dwFlags = win32con.STARTF_USESTDHANDLES | win32con.STARTF_USESHOWWINDOW
            startup.wShowWindow = win32con.SW_HIDE
            startup.hStdInput, startup.hStdOutput, startup.hStdError = (
                stdin, stdout_write, stderr_write,
            )
            script = (
                "$ProgressPreference = 'SilentlyContinue'; "
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                "$OutputEncoding = [Console]::OutputEncoding; " + command
            )
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            # 固定系统壳路径，工作目录和 PATH 中的同名程序不能替换解释器。
            powershell = (
                Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell"
                / "v1.0" / "powershell.exe"
            )
            argv = [
                str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive",
                "-OutputFormat", "Text", "-EncodedCommand", encoded,
            ]
            process, thread, pid, _ = win32process.CreateProcess(
                str(powershell), subprocess.list2cmdline(argv), None, None, True,
                win32con.CREATE_SUSPENDED | win32con.CREATE_NO_WINDOW,
                None, str(cwd), startup,
            )
            job.assign(pid)
            # Detach 把读句柄所有权交给 Python 文件对象，避免重复关闭。
            for reader in (stdout_read, stderr_read):
                descriptor = msvcrt.open_osfhandle(reader.Detach(), os.O_RDONLY | os.O_BINARY)
                handles.remove(reader)
                streams.append(os.fdopen(descriptor, "rb", buffering=0))
            self.stdout, self.stderr = streams
            self.owned = OwnedProcess(process, pid)
            if cancelled.is_set():
                # 排队或原生创建期间取消时，壳保持挂起，不允许用户命令产生副作用。
                win32process.TerminateProcess(process, 1)
                win32event.WaitForSingleObject(process, win32event.INFINITE)
            else:
                win32process.ResumeThread(thread)
        except BaseException:
            if process is not None:
                win32process.TerminateProcess(process, 1)
                win32event.WaitForSingleObject(process, win32event.INFINITE)
                process.Close()
            for stream in streams:
                stream.close()
            raise
        finally:
            if thread is not None:
                thread.Close()
            for handle in handles:
                handle.Close()

    def poll(self) -> int | None:
        """通过原生句柄观察退出，避免 PID 重用造成误判。"""
        return self.owned.poll()

    def wait(self) -> int:
        """所属 Job 已结束后等待壳进程退出。"""
        return self.owned.wait(10)


class HostCommandRunner:
    """Windows 使用原生 PowerShell，Linux 使用无配置 Bash。"""

    def __init__(self) -> None:
        """只接受明确支持的平台，不自动切换其他命令解释器。"""
        if sys.platform == "win32":
            self.platform_name, self.shell_name = "windows", "powershell"
        elif sys.platform.startswith("linux"):
            self.platform_name, self.shell_name = "linux", "bash"
        else:
            raise OSError(f"Unsupported host command platform: {sys.platform}")

    async def run(self, command: str, cwd: Path, timeout_seconds: float) -> CommandResult:
        """在线程内执行并排空管道，取消须等待所属进程与管道清理后原样传播。"""
        cancelled = threading.Event()
        worker = asyncio.create_task(asyncio.to_thread(
            self._run_sync, command, cwd, timeout_seconds, cancelled,
        ))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled.set()
            # 重复取消也不能中断清理；最初的 CancelledError 保留其消息向上传播。
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not worker.cancelled():
                worker.exception()
            raise
        except Exception as exc:
            if self.platform_name == "windows":
                import ctypes

                import pywintypes

                if isinstance(exc, pywintypes.error):
                    # 原生库异常不继承 OSError，统一为保留 winerror 的标准异常。
                    raise ctypes.WinError(exc.winerror) from exc
            raise

    def _run_sync(
        self, command: str, cwd: Path, timeout_seconds: float, cancelled: threading.Event,
    ) -> CommandResult:
        """观察父壳状态，只回收所属 Job 或进程组，并等待输出读取结束。"""
        if cancelled.is_set():
            return CommandResult("", "", -1, False, False)
        capture = _OutputCapture()
        finished = threading.Event()
        job = None
        process = None
        readers = []
        timed_out = False
        exit_code = 0
        try:
            if self.platform_name == "windows":
                job = WindowsJob()
                process = _WindowsCommand(command, cwd, job, cancelled)
            else:
                process = subprocess.Popen(
                    ["/bin/bash", "--noprofile", "--norc", "-c", command],
                    cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, start_new_session=True,
                )
            for name in ("stdout", "stderr"):
                target = capture.read if job is not None else capture.read_linux
                arguments = (getattr(process, name), name)
                if job is None:
                    arguments += (finished,)
                reader = threading.Thread(
                    target=target, args=arguments, daemon=True,
                )
                reader.start()
                readers.append(reader)
            deadline = time.monotonic() + timeout_seconds
            while process.poll() is None:
                if cancelled.is_set():
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                cancelled.wait(0.025)
        finally:
            try:
                if job is not None:
                    self._finish_job(job)
                elif process is not None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass  # 正常退出且没有后台后代时，进程组已由系统删除。
                if process is not None:
                    exit_code = process.wait()
            finally:
                finished.set()
                for reader in readers:
                    reader.join()
                if process is not None:
                    process.stdout.close()
                    process.stderr.close()
                    if isinstance(process, _WindowsCommand):
                        process.owned.close()
        if capture.errors:
            raise capture.errors[0]
        return CommandResult(
            stdout="".join(capture.output["stdout"]), stderr="".join(capture.output["stderr"]),
            exit_code=exit_code, timed_out=timed_out, truncated=capture.truncated,
        )

    @staticmethod
    def _finish_job(job: WindowsJob) -> None:
        """Job 计数先于进程句柄归零，需同时等待实际成员句柄才算释放结束。"""
        import pywintypes
        import win32api
        import win32con
        import win32event
        import win32job

        handles = []
        try:
            for pid in win32job.QueryInformationJobObject(
                job.handle, win32job.JobObjectBasicProcessIdList,
            ):
                try:
                    handle = win32api.OpenProcess(win32con.SYNCHRONIZE | 0x1000, False, pid)
                except pywintypes.error as exc:
                    if exc.winerror != 87:
                        raise
                    continue  # 列举后成员已自行退出，不再持有该进程的观察句柄。
                if not win32job.IsProcessInJob(handle, job.handle):
                    handle.Close()
                    continue  # PID 已复用时，不能把无关进程加入等待集合。
                handles.append(handle)
            win32job.TerminateJobObject(job.handle, 1)
            while job.active_processes():
                time.sleep(0.005)
            for handle in handles:
                win32event.WaitForSingleObject(handle, win32event.INFINITE)
        finally:
            job.close()
            for handle in handles:
                handle.Close()
