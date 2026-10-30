"""使用 HTTP 和进程替身验证本机启动边界，不启动真实 ComfyUI。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from agent_bot.plugins.comfy import launcher as module


class FakeProcess:
    """记录所属进程释放并允许测试模拟提前退出。"""

    def __init__(self) -> None:
        """构造尚未退出的进程。"""
        self.returncode = None
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        """模拟正常终止。"""
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        """模拟强制终止。"""
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        """等待测试控制的终止结果。"""
        assert self.returncode is not None
        return self.returncode


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """创建独立路径、固定 HTTP 响应及无副作用进程边界。"""
    python = tmp_path / 'python.exe'
    python.touch()
    directory = tmp_path / 'ComfyUI'
    directory.mkdir()
    (directory / 'main.py').touch()
    state = SimpleNamespace(online=False, payload=None)

    def respond(request):
        """模拟有效的 ComfyUI 系统状态接口。"""
        assert request.url.path.endswith('/system_stats')
        if not state.online:
            raise httpx.ConnectError('offline', request=request)
        return httpx.Response(200, json=state.payload or {'system': {}, 'devices': []})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, 'AsyncClient', lambda **kw: real_client(
        transport=httpx.MockTransport(respond), **kw,
    ))
    process = FakeProcess()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(module, '_spawn_process', spawn)
    monkeypatch.setattr(module.ComfyLauncher, '_port_available', lambda *args: True)
    config = SimpleNamespace(agent_bot_comfy_python=str(python),
                             agent_bot_comfy_directory=str(directory),
                             agent_bot_comfy_args=['--cpu'])
    return SimpleNamespace(config=config, state=state, spawn=spawn, process=process)


@pytest.mark.asyncio
async def test_external_online_without_config_is_never_spawned_or_closed(harness):
    """已在线外部实例优先于配置条件且不归监督器所有。"""
    harness.config.agent_bot_comfy_python = None
    harness.state.online = True
    launcher = module.ComfyLauncher(harness.config)
    result = await launcher.start('http://127.0.0.1:8188')
    assert result['state'] == 'online'
    assert result['configured'] is False
    assert result['managed'] is False
    await launcher.aclose()
    harness.spawn.assert_not_awaited()
    assert harness.process.terminated is False


@pytest.mark.asyncio
async def test_concurrent_start_uses_fixed_command_and_closes_only_owned(harness):
    """并发点击只启动一次，并派生端口与监听地址。"""
    launcher = module.ComfyLauncher(harness.config)
    results = await asyncio.gather(*(launcher.start('http://127.0.0.1:8199') for _ in range(3)))
    assert all(result['state'] == 'starting' for result in results)
    harness.spawn.assert_awaited_once()
    args, kwargs = harness.spawn.call_args
    assert args == (harness.config.agent_bot_comfy_python,
                    str(module.Path(harness.config.agent_bot_comfy_directory) / 'main.py'),
                    '--listen', '127.0.0.1', '--port', '8199', '--cpu')
    assert kwargs['cwd'] == harness.config.agent_bot_comfy_directory
    assert 'shell' not in kwargs
    assert kwargs['stdout'] is kwargs['stderr']
    assert kwargs['stdout'].name.endswith('comfy-launch.log')
    await launcher.aclose()
    assert harness.process.terminated


@pytest.mark.asyncio
@pytest.mark.parametrize('url', ['https://127.0.0.1:8188', 'http://example.com:8188',
                                  'http://127.0.0.1:8188/proxy'])
async def test_remote_or_nonroot_url_cannot_start(harness, url):
    """远程或代理连接不授予本机启动权限。"""
    launcher = module.ComfyLauncher(harness.config)
    result = await launcher.start(url)
    assert result['state'] == 'failed'
    assert result['local'] is False
    assert result['error_code'] == 'unsupported_url'
    harness.spawn.assert_not_awaited()
    await launcher.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('args', [['--port=9000'], ['--listen', '0.0.0.0'], ' --cpu'])
async def test_reserved_or_unstructured_arguments_rejected(harness, args):
    """附加参数不能覆盖服务地址或作为命令字符串执行。"""
    harness.config.agent_bot_comfy_args = args
    launcher = module.ComfyLauncher(harness.config)
    result = await launcher.start('http://127.0.0.1:8188')
    assert result['error_code'] == 'invalid_configuration'
    harness.spawn.assert_not_awaited()
    await launcher.aclose()


@pytest.mark.asyncio
async def test_other_http_service_is_not_comfy_and_occupied_port_is_reported(harness, monkeypatch):
    """普通 JSON HTTP 服务不能被误报为 Comfy 在线。"""
    harness.state.online = True
    harness.state.payload = {'status': 'ok'}
    monkeypatch.setattr(module.ComfyLauncher, '_port_available', lambda *args: False)
    launcher = module.ComfyLauncher(harness.config)
    result = await launcher.start('http://127.0.0.1:8188')
    assert result['state'] == 'failed'
    assert result['error_code'] == 'port_in_use'
    harness.spawn.assert_not_awaited()
    await launcher.aclose()


@pytest.mark.asyncio
async def test_startup_observation_detects_exit(harness):
    """启动后立即退出必须显示失败且关闭日志。"""
    launcher = module.ComfyLauncher(harness.config)
    await launcher.start('http://127.0.0.1:8188')
    harness.process.returncode = 1
    await launcher._observe_task
    result = await launcher.snapshot('http://127.0.0.1:8188')
    assert result['state'] == 'failed'
    assert result['error_code'] == 'process_exited'
    assert harness.spawn.call_args.kwargs['stdout'].closed
    await launcher.aclose()


@pytest.mark.asyncio
async def test_startup_timeout_reclaims_owned_process(harness, monkeypatch):
    """有限观察期限到达后只回收此次所属进程。"""
    monkeypatch.setattr(module, 'STARTUP_TIMEOUT_SECONDS', 0)
    launcher = module.ComfyLauncher(harness.config)
    await launcher.start('http://127.0.0.1:8188')
    await launcher._observe_task
    result = await launcher.snapshot('http://127.0.0.1:8188')
    assert result['error_code'] == 'startup_timeout'
    assert harness.process.terminated
    await launcher.aclose()


@pytest.mark.asyncio
async def test_http_ready_and_changed_url_never_spawns_second_process(harness):
    """HTTP 确认在线后保留所属关系，地址改变要求重启。"""
    launcher = module.ComfyLauncher(harness.config)
    await launcher.start('http://127.0.0.1:8188')
    harness.state.online = True
    await launcher._observe_task
    assert (await launcher.snapshot('http://127.0.0.1:8188'))['state'] == 'online'
    harness.state.online = False
    result = await launcher.start('http://127.0.0.1:8199')
    assert result['error_code'] == 'restart_required'
    harness.spawn.assert_awaited_once()
    await launcher.aclose()

@pytest.mark.asyncio
async def test_windows_job_contains_venv_children_before_resume(monkeypatch, tmp_path):
    """挂起时加入 Job，父解释器退出后仍持有子进程并可整体清理。"""
    import sys

    events = []
    active = {'count': 1}

    class Handle:
        """提供不会触碰操作系统的原生句柄替身。"""

        def __init__(self, name):
            """记录句柄名称。"""
            self.name = name

        def Close(self):
            """记录句柄释放。"""
            events.append('close_' + self.name)

    job, process, thread = Handle('job'), Handle('process'), Handle('thread')

    def assign(*args):
        """记录挂起进程的 Job 归属。"""
        events.append('assign')

    def resume(*args):
        """记录主线程恢复。"""
        events.append('resume')

    def terminate(*args):
        """同时释放包装器的后代。"""
        active['count'] = 0
        events.append('terminate_job')

    monkeypatch.setitem(sys.modules, 'msvcrt', SimpleNamespace(get_osfhandle=lambda fd: 99))
    monkeypatch.setitem(sys.modules, 'win32api', SimpleNamespace(
        SetHandleInformation=lambda *args: None))
    monkeypatch.setitem(sys.modules, 'win32con', SimpleNamespace(
        STARTF_USESHOWWINDOW=1, STARTF_USESTDHANDLES=2, SW_HIDE=0,
        CREATE_SUSPENDED=4, CREATE_NO_WINDOW=8, HANDLE_FLAG_INHERIT=1))
    monkeypatch.setitem(sys.modules, 'win32job', SimpleNamespace(
        CreateJobObject=lambda *args: job,
        QueryInformationJobObject=lambda *args: {'BasicLimitInformation': {},
                                               'ActiveProcesses': active['count']},
        SetInformationJobObject=lambda *args: None,
        JobObjectExtendedLimitInformation=9, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE=8192,
        JobObjectBasicAccountingInformation=1,
        AssignProcessToJobObject=assign, TerminateJobObject=terminate))
    monkeypatch.setitem(sys.modules, 'win32process', SimpleNamespace(
        STARTUPINFO=SimpleNamespace,
        CreateProcess=lambda *args: (process, thread, 123, 456), ResumeThread=resume,
        GetExitCodeProcess=lambda *args: 0, TerminateProcess=lambda *args: None))
    monkeypatch.setitem(sys.modules, 'win32event', SimpleNamespace(
        WaitForSingleObject=lambda *args: 0, WAIT_TIMEOUT=258))
    with (tmp_path / 'log').open('ab') as log:
        owned = module._WindowsProcess(('python.exe', 'main.py'), str(tmp_path), log)
        assert events.index('assign') < events.index('resume')
        # 包装器已退出，Job 中的子进程仍被视为运行。
        assert owned.returncode is None
        owned.terminate()
        assert await owned.wait() == 0
        owned.close()
    assert 'terminate_job' in events
    assert 'close_job' in events

@pytest.mark.asyncio
async def test_online_owned_process_exit_allows_relaunch(harness):
    """在线后的所属进程退出应释放句柄，允许管理员再次手动启动。"""
    launcher = module.ComfyLauncher(harness.config)
    await launcher.start('http://127.0.0.1:8188')
    harness.state.online = True
    await launcher._observe_task
    harness.process.returncode = 1
    harness.state.online = False
    assert (await launcher.snapshot('http://127.0.0.1:8188'))['error_code'] == 'process_exited'
    harness.spawn.return_value = FakeProcess()
    assert (await launcher.start('http://127.0.0.1:8188'))['state'] == 'starting'
    assert harness.spawn.await_count == 2
    await launcher.aclose()


@pytest.mark.asyncio
async def test_constructor_does_not_allocate_http_resources(harness, monkeypatch):
    """仅准备插件但尚未启动时不分配需要异步关闭的 HTTP 连接。"""
    client_factory = module.httpx.AsyncClient
    created = []

    def client(**kwargs):
        """追踪实际分配的资源。"""
        created.append(True)
        return client_factory(**kwargs)

    monkeypatch.setattr(module.httpx, 'AsyncClient', client)
    launcher = module.ComfyLauncher(harness.config)
    assert created == []
    await launcher.aclose()
    assert created == []


@pytest.mark.asyncio
async def test_browser_cancel_during_spawn_does_not_cancel_accepted_operation(harness):
    """浏览器取消请求不会中断已接收的启动，也不会遗留无人管理的进程。"""
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_spawn(*args, **kwargs):
        """在创建进程的 await 边界注入请求取消。"""
        entered.set()
        await release.wait()
        return harness.process

    harness.spawn.side_effect = delayed_spawn
    launcher = module.ComfyLauncher(harness.config)
    request = asyncio.create_task(launcher.start('http://127.0.0.1:8188'))
    await entered.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    release.set()
    # 再次状态读取和启动任务共享锁，确保创建结果已登记。
    result = await launcher.snapshot('http://127.0.0.1:8188')
    assert result['managed']
    await launcher.aclose()
    assert harness.process.terminated

@pytest.mark.asyncio
async def test_close_cancellation_waits_until_owned_process_is_reclaimed(harness, monkeypatch):
    """关闭请求取消仍先完成所有权回收，再向调用者传播取消。"""
    launcher = module.ComfyLauncher(harness.config)
    await launcher.start('http://127.0.0.1:8188')
    entered, release = asyncio.Event(), asyncio.Event()
    original_release = launcher._release_process

    async def delayed_release():
        """模拟操作系统尚未确认进程退出。"""
        entered.set()
        await release.wait()
        await original_release()

    monkeypatch.setattr(launcher, '_release_process', delayed_release)
    closing = asyncio.create_task(launcher.aclose())
    await entered.wait()
    closing.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert harness.process.terminated


@pytest.mark.asyncio
async def test_spawn_failure_closes_log_without_exposing_exception(harness):
    """创建失败不会泄漏日志句柄或将潜在敏感异常回传。"""
    harness.spawn.side_effect = OSError('secret-sensitive-command')
    launcher = module.ComfyLauncher(harness.config)
    result = await launcher.start('http://127.0.0.1:8188')
    assert result['error_code'] == 'launch_failed'
    assert 'secret-sensitive-command' not in str(result)
    assert harness.spawn.call_args.kwargs['stdout'].closed
    assert not result['managed']
    await launcher.aclose()


def test_local_lan_address_requires_actual_binding(monkeypatch):
    """仅接受可在本机绑定的局域网地址，不把任意私网地址视为本机。"""
    from unittest.mock import MagicMock

    sock = MagicMock()
    sock.__enter__.return_value = sock
    monkeypatch.setattr(module.socket, 'socket', lambda *args: sock)
    assert module.ComfyLauncher._endpoint('http://192.168.1.2:8188') == ('192.168.1.2', 8188)
    sock.bind.assert_called_once_with(('192.168.1.2', 0))
    sock.bind.side_effect = OSError('not local')
    assert module.ComfyLauncher._endpoint('http://192.168.1.3:8188') is None

@pytest.mark.asyncio
async def test_timeout_cleanup_serializes_retry_without_losing_new_process(harness, monkeypatch):
    """旧进程回收期间阻塞重试，避免旧观察任务清除新进程与日志所有权。"""
    monkeypatch.setattr(module, 'STARTUP_TIMEOUT_SECONDS', 0)
    cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event()
    waits = 0

    async def delayed_first_wait():
        """只阻塞首次回收，让无锁的第二次回收暴露竞态。"""
        nonlocal waits
        waits += 1
        if waits == 1:
            cleanup_entered.set()
            await cleanup_release.wait()
        return 0

    harness.process.wait = delayed_first_wait
    launcher = module.ComfyLauncher(harness.config)
    await launcher.start('http://127.0.0.1:8188')
    old_observer = launcher._observe_task
    await cleanup_entered.wait()
    monkeypatch.setattr(module, 'STARTUP_TIMEOUT_SECONDS', 120)
    replacement = FakeProcess()
    harness.spawn.return_value = replacement
    retry = asyncio.create_task(launcher.start('http://127.0.0.1:8188'))
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(retry), 0.05)
    finally:
        cleanup_release.set()
        await old_observer
        result = await retry
        assert result['state'] == 'starting'
        snapshot = await launcher.snapshot('http://127.0.0.1:8188')
        assert snapshot['managed']
        assert not harness.spawn.call_args.kwargs['stdout'].closed
        await launcher.aclose()
    assert replacement.terminated


@pytest.mark.asyncio
async def test_close_waits_for_observer_cleanup_without_deadlock(harness, monkeypatch):
    """关闭与观察任务共享锁时，等待正在进行的回收而不取消半完成的回收。"""
    monkeypatch.setattr(module, 'STARTUP_TIMEOUT_SECONDS', 0)
    cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event()

    async def delayed_wait():
        """模拟 Job 正在排空，关闭必须等待所属进程回收完成。"""
        cleanup_entered.set()
        await cleanup_release.wait()
        return 0

    harness.process.wait = delayed_wait
    launcher = module.ComfyLauncher(harness.config)
    await launcher.start('http://127.0.0.1:8188')
    await cleanup_entered.wait()
    closing = asyncio.create_task(launcher.aclose())
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(closing), 0.05)
    finally:
        cleanup_release.set()
        await asyncio.wait_for(closing, 1)
    assert not (await launcher.snapshot('http://127.0.0.1:8188'))['managed']
