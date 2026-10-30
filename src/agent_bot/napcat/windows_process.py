"""Windows Job 约束所属后代；不按 QQ 进程名称查杀其他实例。"""

import ctypes
import os
import socket
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

from agent_bot.napcat.contracts import NapCatLaunch
from agent_bot.napcat.payload import is_link


class OwnedProcess:
    """持有单个原生进程句柄，进程树的终止由 Job 负责。"""

    def __init__(self, handle: Any, pid: int) -> None:
        """接收已加入 Job 的进程句柄。"""
        self.handle, self.pid = handle, pid

    def poll(self) -> int | None:
        """通过句柄观察，不使用可能复用的 PID 判断退出。"""
        import win32event
        import win32process

        if win32event.WaitForSingleObject(self.handle, 0) == win32event.WAIT_TIMEOUT:
            return None
        return win32process.GetExitCodeProcess(self.handle)

    def wait(self, timeout: float) -> int:
        """有界等待，超时交给所属 Job 清理。"""
        import win32event
        import win32process

        result = win32event.WaitForSingleObject(self.handle, int(timeout * 1000))
        if result == win32event.WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired("owned process", timeout)
        return win32process.GetExitCodeProcess(self.handle)

    def close(self) -> None:
        """释放观察句柄，允许部分启动失败时重复清理。"""
        if self.handle is not None:
            self.handle.Close()
            self.handle = None


class WindowsJob:
    """最后一个 Job 句柄关闭时，系统终止整个所属进程树。"""

    def __init__(self) -> None:
        """仅在 Windows 实际启动路径导入原生库，普通 CLI 不依赖它。"""
        if sys.platform != "win32":
            raise OSError("Windows Job is unavailable")
        import win32job

        self.handle = win32job.CreateJobObject(None, "")
        try:
            limits = win32job.QueryInformationJobObject(
                self.handle, win32job.JobObjectExtendedLimitInformation)
            limits["BasicLimitInformation"]["LimitFlags"] = (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
            win32job.SetInformationJobObject(
                self.handle, win32job.JobObjectExtendedLimitInformation, limits)
        except BaseException:
            self.close()
            raise

    def assign(self, pid: int) -> None:
        """桌面后端先等待私有握手，再被加入 Job，避免派生进程竞态。"""
        import win32api
        import win32job

        handle = win32api.OpenProcess(0x0100 | 0x0001, False, pid)
        try:
            win32job.AssignProcessToJobObject(self.handle, handle)
        finally:
            handle.Close()

    def spawn(self, launch: NapCatLaunch) -> OwnedProcess:
        """挂起创建后先加入 Job，再恢复主线程；不继承上游原始日志管道。"""
        import win32con
        import win32job
        import win32process

        startup = win32process.STARTUPINFO()
        startup.dwFlags = win32con.STARTF_USESHOWWINDOW
        startup.wShowWindow = win32con.SW_HIDE
        process, thread, pid, _ = win32process.CreateProcess(
            launch.command[0], subprocess.list2cmdline(launch.command), None, None, False,
            win32con.CREATE_SUSPENDED | win32con.CREATE_NO_WINDOW,
            launch.environment, str(launch.cwd), startup,
        )
        try:
            win32job.AssignProcessToJobObject(self.handle, process)
            win32process.ResumeThread(thread)
        except BaseException:
            win32process.TerminateProcess(process, 1)
            process.Close()
            raise
        finally:
            thread.Close()
        return OwnedProcess(process, pid)

    def active_processes(self) -> int:
        """启动器提前退出时仍观察 Job 中的后代。"""
        import win32job

        return win32job.QueryInformationJobObject(
            self.handle, win32job.JobObjectBasicAccountingInformation)["ActiveProcesses"]

    def owns_listener(self, host: str, port: int) -> bool:
        """用系统 TCP 表核对监听进程归属，不把外部 WebUI 认作本实例。"""
        import pywintypes
        import win32api
        import win32job

        # NapCat 可能绑定回环、IPv4 通配或 IPv6 通配(双栈)；端口一致且
        # 监听进程属于本 Job 即可确认归属，地址只需是本机监听形态。
        local_forms = {"127.0.0.1", "0.0.0.0", host}
        size = ctypes.c_ulong(0)
        api = ctypes.windll.iphlpapi.GetExtendedTcpTable
        result = api(None, ctypes.byref(size), False, socket.AF_INET, 3, 0)
        if result not in (0, 122):
            raise OSError("TCP table query failed")
        buffer = ctypes.create_string_buffer(size.value)
        if api(buffer, ctypes.byref(size), False, socket.AF_INET, 3, 0) != 0:
            raise OSError("TCP table query failed")
        count = struct.unpack_from("=I", buffer, 0)[0]
        for index in range(count):
            state, address, local_port, _, _, pid = struct.unpack_from(
                "=6I", buffer, 4 + index * 24)
            if (state != 2 or socket.ntohs(local_port & 0xFFFF) != port
                    or socket.inet_ntoa(struct.pack("=I", address)) not in local_forms):
                continue
            try:
                handle = win32api.OpenProcess(0x1000, False, pid)
            except pywintypes.error:
                return False  # 查询期间监听者已退出，下一次观察重新读取。
            try:
                return win32job.IsProcessInJob(handle, self.handle)
            finally:
                handle.Close()
        return False

    def close(self) -> None:
        """释放唯一所有权，关闭后不得再派生进程。"""
        if self.handle is not None:
            handle, self.handle = self.handle, None
            handle.Close()


class InstanceLease:
    """同一托管目录的非阻塞文件锁，进程异常退出由 OS 自动释放。"""

    def __init__(self, directory: Path) -> None:
        """仅记录目录，构造不创建运行数据。"""
        self.directory = directory
        self.stream = None

    def __enter__(self) -> "InstanceLease":
        """锁只含占位字节，不保存进程控制命令或秘密。"""
        import msvcrt

        if is_link(self.directory):
            raise OSError("Instance directory cannot be a link")
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / ".instance.lock"
        if is_link(target):
            raise OSError("Instance lock cannot be a link")
        self.stream = target.open("a+b")
        try:
            if self.stream.seek(0, os.SEEK_END) == 0:
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
        except BaseException:
            self.stream.close()
            self.stream = None
            raise
        return self

    def __exit__(self, *args) -> None:
        """释放本进程持有的锁，不删除其他实例的文件。"""
        import msvcrt

        if self.stream is not None:
            try:
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self.stream.close()
                self.stream = None
