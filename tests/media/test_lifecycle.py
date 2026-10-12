"""跨层验证运行时热刷新等待后台文件和数据库线程结束。"""

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from agent_bot.integrations.gpt_sovits import GptSoVitsClient
from agent_bot.services.workflow_execution import WorkflowExecutionService


@pytest.mark.parametrize("operation", ["list", "describe", "generate", "tts"])
async def test_refresh_waits_for_file_and_database_cleanup(tmp_path, monkeypatch, operation):
    """取消执行仍需等不可中断线程归还资源，刷新才能关闭旧客户端或连接池。"""
    from agent_bot.plugin.lifecycle import RuntimeLeaseGate

    entered, release = threading.Event(), threading.Event()
    gate = RuntimeLeaseGate()
    original_write = Path.write_bytes

    def blocking(*args, **kwargs):
        """用明确时序模拟正在访问数据库或磁盘的同步操作。"""
        entered.set()
        assert release.wait(5)
        return ()

    def write(path, data):
        """写入开始后取消不能令持有者提前退出。"""
        blocking()
        return original_write(path, data)

    workflow = WorkflowExecutionService(
        store=SimpleNamespace(list_executable=blocking, get_executable=blocking),
        client=SimpleNamespace(),
    )
    tts = None
    if operation == "tts":
        monkeypatch.setattr(Path, "write_bytes", write)
        tts = GptSoVitsClient(
            url="http://127.0.0.1:9880/tts",
            reference_audio_path="reference.wav",
            reference_text="参考",
            reference_language="zh",
            text_language="zh",
            output_directory=tmp_path,
            speed_factor=1,
            timeout_seconds=30,
            max_input_chars=100,
            max_audio_bytes=1024,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, content=b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + b"\0" * 32
                )
            ),
        )

    async def execute():
        """复用管理和 QQ 请求的同一运行时租约边界。"""
        async with gate.lease():
            if operation == "tts":
                await tts.generate("你好")
            elif operation == "list":
                await workflow.list_workflows()
            elif operation == "describe":
                await workflow.describe_workflow("portrait")
            else:
                await workflow.generate(
                    workflow_key="portrait", positive_prompt="猫", negative_prompt=""
                )

    task = asyncio.create_task(execute())
    idle = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await gate.begin_refresh()
        idle = asyncio.create_task(gate.wait_until_idle())
        await asyncio.sleep(0.03)
        assert not idle.done(), "同步资源尚未归还，运行时刷新不能提前完成"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        if idle:
            await asyncio.wait_for(idle, 3)
        await gate.end_refresh()
        if tts:
            await tts.aclose()
    assert task.cancelled()
