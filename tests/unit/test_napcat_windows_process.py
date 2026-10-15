"""用 Python 假父子进程验证 Windows Job，不执行 QQ 或外部服务。"""

import os
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job 验证")


def test_job_closes_descendants_without_killing_outside_process(tmp_path):
    """启动器退出后仍持有所属后代，关闭 Job 只清理这个进程族。"""
    import win32api
    import win32con
    import win32event

    from agent_bot.napcat.contracts import NapCatLaunch
    from agent_bot.napcat.windows_process import WindowsJob

    marker = tmp_path / "child.pid"
    child = "import time; time.sleep(90)"
    parent = ("import subprocess,sys,pathlib; "
              f"p=subprocess.Popen([sys.executable,'-c',{child!r}]); "
              f"pathlib.Path({str(marker)!r}).write_text(str(p.pid))")
    outside = subprocess.Popen([sys.executable, "-c", child])
    job = WindowsJob()
    owned = job.spawn(NapCatLaunch((sys.executable, "-c", parent), tmp_path, dict(os.environ)))
    handle = None
    try:
        limit = time.monotonic() + 8
        while not marker.exists() and time.monotonic() < limit:
            time.sleep(0.05)
        assert marker.exists()
        handle = win32api.OpenProcess(win32con.SYNCHRONIZE, False, int(marker.read_text()))
        owned.wait(8)
        assert job.active_processes() >= 1
        job.close()
        assert win32event.WaitForSingleObject(handle, 8000) == win32event.WAIT_OBJECT_0
        assert outside.poll() is None
    finally:
        job.close()
        owned.close()
        if handle:
            handle.Close()
        outside.terminate()
        outside.wait(timeout=8)


def test_instance_lock_is_exclusive_and_released(tmp_path):
    """锁释放后可再次托管；不能抢占另一个窗口的实例。"""
    from agent_bot.napcat.windows_process import InstanceLease

    with InstanceLease(tmp_path):
        with pytest.raises(OSError):
            with InstanceLease(tmp_path):
                pass
    with InstanceLease(tmp_path):
        pass


def test_listener_readiness_requires_owned_process(tmp_path):
    """同端口外部服务不能冒充所属 WebUI，属于 Job 的监听者才就绪。"""
    import socket

    from agent_bot.napcat.contracts import NapCatLaunch
    from agent_bot.napcat.windows_process import WindowsJob

    marker = tmp_path / "port.txt"
    code = ("import socket,pathlib,time; s=socket.socket(); s.bind(('127.0.0.1',0)); "
            f"s.listen(); pathlib.Path({str(marker)!r}).write_text(str(s.getsockname()[1])); "
            "time.sleep(90)")
    job = WindowsJob()
    process = job.spawn(NapCatLaunch((sys.executable, "-c", code), tmp_path, dict(os.environ)))
    try:
        limit = time.monotonic() + 8
        while not marker.exists() and time.monotonic() < limit:
            time.sleep(0.02)
        assert marker.exists()
        assert job.owns_listener("127.0.0.1", int(marker.read_text()))
        with socket.socket() as outside:
            outside.bind(("127.0.0.1", 0))
            outside.listen()
            assert not job.owns_listener("127.0.0.1", outside.getsockname()[1])
    finally:
        job.close()
        process.wait(8)
        process.close()
