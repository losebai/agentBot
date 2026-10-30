"""拥有手动启动的 ComfyUI 子进程，通过 HTTP 确认服务状态。"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO
from urllib.parse import urlsplit

import httpx

if TYPE_CHECKING:
    from agent_bot.config import RuntimeConfig

STARTUP_TIMEOUT_SECONDS = 120


class _WindowsProcess:
    """挂起加入私有 Job，包装解释器退出后仍拥有全部后代。"""

    def __init__(self, command: tuple[str, ...], cwd: str, log: BinaryIO) -> None:
        """隐藏创建并先登记所属关系，避免 venv 包装器的派生竞态。"""
        import msvcrt

        import win32api
        import win32con
        import win32job
        import win32process

        self._process = None
        self._job = win32job.CreateJobObject(None, '')
        thread = None
        log_handle = msvcrt.get_osfhandle(log.fileno())
        try:
            limits = win32job.QueryInformationJobObject(
                self._job, win32job.JobObjectExtendedLimitInformation)
            limits['BasicLimitInformation']['LimitFlags'] = (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
            win32job.SetInformationJobObject(
                self._job, win32job.JobObjectExtendedLimitInformation, limits)
            startup = win32process.STARTUPINFO()
            startup.dwFlags = win32con.STARTF_USESHOWWINDOW | win32con.STARTF_USESTDHANDLES
            startup.wShowWindow = win32con.SW_HIDE
            # 文件句柄仅在同步创建期间可继承，恢复主线程前归属已固定。
            win32api.SetHandleInformation(log_handle, win32con.HANDLE_FLAG_INHERIT, 1)
            with open(os.devnull, 'rb') as stdin:
                stdin_handle = msvcrt.get_osfhandle(stdin.fileno())
                win32api.SetHandleInformation(stdin_handle, win32con.HANDLE_FLAG_INHERIT, 1)
                startup.hStdInput = stdin_handle
                startup.hStdOutput = startup.hStdError = log_handle
                self._process, thread, self.pid, _ = win32process.CreateProcess(
                    command[0], subprocess.list2cmdline(command), None, None, True,
                    win32con.CREATE_SUSPENDED | win32con.CREATE_NO_WINDOW,
                    None, cwd, startup,
                )
            win32job.AssignProcessToJobObject(self._job, self._process)
            win32process.ResumeThread(thread)
        except BaseException:
            if self._process is not None:
                win32process.TerminateProcess(self._process, 1)
            self.close()
            raise
        finally:
            win32api.SetHandleInformation(log_handle, win32con.HANDLE_FLAG_INHERIT, 0)
            if thread is not None:
                thread.Close()

    @property
    def returncode(self) -> int | None:
        """以 Job 中存活数量判断退出，不误判 venv 包装器正常退出。"""
        import win32job
        import win32process

        info = win32job.QueryInformationJobObject(
            self._job, win32job.JobObjectBasicAccountingInformation)
        return None if info['ActiveProcesses'] else win32process.GetExitCodeProcess(self._process)

    def terminate(self) -> None:
        """终止所属 Job 中所有进程，不按名称或复用的 PID 查杀。"""
        import win32job

        win32job.TerminateJobObject(self._job, 1)

    def kill(self) -> None:
        """使用同一 Job 所有权边界强制释放。"""
        self.terminate()

    async def wait(self) -> int:
        """异步观察 Job 排空，由调用者施加退出期限。"""
        while self.returncode is None:  # noqa: ASYNC110 -- Job 没有异步完成事件。
            await asyncio.sleep(0.05)
        return self.returncode

    def close(self) -> None:
        """关闭 Job 时系统回收后代，再释放根进程观察句柄。"""
        if self._job is not None:
            self._job.Close()
            self._job = None
        if self._process is not None:
            self._process.Close()
            self._process = None


async def _spawn_process(*command: str, cwd: str, stdout: BinaryIO,
                         stderr: BinaryIO) -> asyncio.subprocess.Process | _WindowsProcess:
    """按平台创建所属进程，Windows 必须使用完整进程树所有权。"""
    if sys.platform == 'win32':
        try:
            return _WindowsProcess(command, cwd, stdout)
        except Exception as error:
            raise OSError('ComfyUI process creation failed') from error
    return await asyncio.create_subprocess_exec(*command, cwd=cwd, stdout=stdout, stderr=stderr)


class ComfyLauncher:
    """固定使用启动配置，外部在线服务始终不属于本监督器。"""

    def __init__(self, config: RuntimeConfig) -> None:
        """快照启动字段，页面保存静态配置后必须重启后端才生效。"""
        self._python = config.agent_bot_comfy_python
        self._directory = config.agent_bot_comfy_directory
        args = config.agent_bot_comfy_args
        self._args = list(args) if isinstance(args, list) else args
        self._lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | _WindowsProcess | None = None
        self._managed_url: str | None = None
        self._observe_task: asyncio.Task[None] | None = None
        self._log: BinaryIO | None = None
        self._state = 'offline'
        self._error: str | None = None
        self._closed = False
        self._client: httpx.AsyncClient | None = None
        self._start_tasks: set[asyncio.Task[dict]] = set()
        self._close_task: asyncio.Task[None] | None = None

    @staticmethod
    def _endpoint(base_url: str) -> tuple[str, int] | None:
        """只允许本机 HTTP 根地址；实际绑定本机临时端口识别局域网地址。"""
        try:
            parsed = urlsplit(base_url)
            if (parsed.scheme != 'http' or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
                return None
            host = parsed.hostname
            port = parsed.port if parsed.port is not None else 80
            if not 1 <= port <= 65535:
                return None
            if host == 'localhost':
                return host, port
            address = ipaddress.ip_address(host)
            if address.is_unspecified or address.is_multicast:
                return None
            family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.bind((host, 0))
            return host, port
        except (ValueError, OSError):
            return None

    @staticmethod
    def _port_available(host: str, port: int) -> bool:
        """启动前检测目标监听端口，绑定失败明确报告占用。"""
        family = socket.AF_INET6 if ':' in host else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.bind((host, port))
            return True
        except OSError:
            return False

    async def _online(self, base_url: str) -> bool:
        """验证 ComfyUI 系统与设备结构，普通 HTTP 成功不能视为在线。"""
        try:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    timeout=2, trust_env=False, follow_redirects=False)
            response = await self._client.get(base_url.rstrip('/') + '/system_stats')
            response.raise_for_status()
            payload = response.json()
            return (isinstance(payload, dict) and isinstance(payload.get('system'), dict)
                    and isinstance(payload.get('devices'), list))
        except (httpx.HTTPError, ValueError):
            return False

    def _configuration_error(self) -> str | None:
        """检查路径与结构化参数，阻止覆盖从服务地址推导的监听参数。"""
        if not self._python or not self._directory:
            return 'configuration_missing'
        if (not isinstance(self._args, list)
                or any(not isinstance(arg, str) or '\x00' in arg for arg in self._args)
                or any(arg.startswith('--') and any(
                    flag.startswith(arg.split('=', 1)[0]) for flag in ('--listen', '--port')
                ) for arg in self._args)):
            return 'invalid_configuration'
        if not Path(self._python).is_file() or not (Path(self._directory) / 'main.py').is_file():
            return 'invalid_configuration'
        return None

    def _result(self, base_url: str, state: str, error: str | None = None) -> dict:
        """只公开路径及有限状态，不回传可能包含密钥的参数或日志。"""
        return {
            'state': state,
            'configured': self._configuration_error() is None,
            'local': self._endpoint(base_url) is not None,
            'managed': self._process is not None,
            'error_code': error,
            'python': self._python,
            'directory': self._directory,
        }

    async def _snapshot(self, base_url: str) -> dict:
        """调用者持锁，在线服务优先于缺少启动配置等条件。"""
        if self._closed:
            return self._result(base_url, 'failed', 'closed')
        if (self._state != 'starting' and self._process is not None
                and self._process.returncode is not None):
            self._state, self._error = 'failed', 'process_exited'
            await self._release_process()
        if await self._online(base_url):
            return self._result(base_url, 'online')
        if self._process is not None and self._managed_url != base_url.rstrip('/'):
            return self._result(base_url, 'failed', 'restart_required')
        if self._state == 'starting':
            return self._result(base_url, 'starting')
        if self._state == 'failed':
            return self._result(base_url, 'failed', self._error)
        error = self._configuration_error()
        if error is not None:
            return self._result(base_url, 'not_configured', error)
        if self._endpoint(base_url) is None:
            return self._result(base_url, 'failed', 'unsupported_url')
        return self._result(base_url, 'offline')

    async def snapshot(self, base_url: str) -> dict:
        """读取真实 HTTP 状态，并串行化与启动、关闭之间的资源操作。"""
        async with self._lock:
            return await self._snapshot(base_url)

    async def start(self, base_url: str) -> dict:
        """将已接收的意图交给独立任务，浏览器断开不取消所属进程登记。"""
        task = asyncio.create_task(self._start(base_url))
        self._start_tasks.add(task)
        task.add_done_callback(self._start_tasks.discard)
        return await asyncio.shield(task)

    async def _start(self, base_url: str) -> dict:
        """接受无参数启动意图，锁内验证并只创建固定命令所属进程。"""
        async with self._lock:
            status = await self._snapshot(base_url)
            if status['state'] in ('online', 'starting') or self._closed:
                return status
            if self._process is not None:
                return self._result(base_url, 'failed', 'restart_required')
            endpoint = self._endpoint(base_url)
            error = 'unsupported_url' if endpoint is None else self._configuration_error()
            if error is not None:
                return self._result(base_url, 'not_configured' if error == 'configuration_missing'
                                    else 'failed', error)
            host, port = endpoint
            if not self._port_available(host, port):
                return self._result(base_url, 'failed', 'port_in_use')
            try:
                directory, python = self._prepare_files()
                self._process = await _spawn_process(
                    python, str(directory / 'main.py'), '--listen', host, '--port', str(port),
                    *self._args, cwd=str(directory), stdout=self._log, stderr=self._log,
                )
            except (OSError, ValueError):
                self._close_log()
                self._state, self._error = 'failed', 'launch_failed'
                return self._result(base_url, self._state, self._error)
            self._managed_url = base_url.rstrip('/')
            self._state, self._error = 'starting', None
            self._observe_task = asyncio.create_task(
                self._observe(self._managed_url, self._process))
            return self._result(base_url, 'starting')

    def _prepare_files(self) -> tuple[Path, str]:
        """使用绝对路径并创建本地追加日志，避免工作目录改变解释器含义。"""
        directory = Path(self._directory).resolve()
        python = str(Path(self._python).resolve())
        self._log = (directory / 'comfy-launch.log').open('ab')
        return directory, python

    async def _observe(
        self, base_url: str, process: asyncio.subprocess.Process | _WindowsProcess,
    ) -> None:
        """持锁观察固定一代进程，回收结束前不允许读取或启动接管资源。"""
        deadline = asyncio.get_running_loop().time() + STARTUP_TIMEOUT_SECONDS
        while True:
            async with self._lock:
                # 观察任务的地址和期限只能用于创建它时的进程，不能越代回收。
                if self._process is not process or self._closed:
                    return
                if process.returncode is not None:
                    self._state, self._error = 'failed', 'process_exited'
                    await self._release_process()
                    return
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    self._state, self._error = 'failed', 'startup_timeout'
                    await self._release_process()
                    return
                try:
                    online = await asyncio.wait_for(self._online(base_url), remaining)
                except asyncio.TimeoutError:
                    online = False
                if online:
                    self._state, self._error = 'online', None
                    return
            # 关闭持有同一锁，只会取消正在锁外休眠或等锁的观察任务。
            await asyncio.sleep(min(1, max(0, deadline - asyncio.get_running_loop().time())))

    def _close_log(self) -> None:
        """关闭本地日志句柄，避免失败或插件退出时泄漏文件资源。"""
        if self._log is not None:
            self._log.close()
            self._log = None

    async def _release_process(self) -> None:
        """有界终止且只触碰由本监督器创建的子进程。"""
        process = self._process
        try:
            if process is not None:
                if process.returncode is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(process.wait(), 5)
                    except asyncio.TimeoutError:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        await asyncio.wait_for(process.wait(), 5)
                else:
                    await process.wait()
        finally:
            if isinstance(process, _WindowsProcess):
                process.close()
            self._process = None
            self._managed_url = None
            self._close_log()

    async def aclose(self) -> None:
        """取消关闭请求也先等待共享关闭任务完成，保证所属资源不泄漏。"""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        cancellation = None
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError as error:
                cancellation = error
        self._close_task.result()
        if cancellation is not None:
            raise cancellation

    async def _close(self) -> None:
        """插件停止后取消观察并释放所属进程，绝不接管外部实例。"""
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._observe_task is not None:
                self._observe_task.cancel()
                try:
                    await self._observe_task
                except asyncio.CancelledError:
                    pass
            try:
                await self._release_process()
            finally:
                if self._client is not None:
                    await self._client.aclose()
