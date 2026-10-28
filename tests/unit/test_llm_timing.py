"""验证性能日志在默认服务日志管道可见，不依赖测试临时提升标准日志级别。"""

import json
from time import monotonic

from nonebot.log import logger

from agent_bot.services.llm_timing import record_timing


def test_timing_reaches_service_log_sink_with_safe_structured_fields():
    """未设置根日志 INFO 时，MCP 冷连接计时仍通过 NoneBot 日志输出。"""
    captured = []
    sink = logger.add(lambda message: captured.append(message.record), level="INFO")
    try:
        record_timing("mcp_prepare", None, monotonic(), status="completed", reused=False)
    finally:
        logger.remove(sink)
    assert len(captured) == 1
    fields = json.loads(captured[0]["message"].split(" ", 1)[1])
    assert fields["stage"] == "mcp_prepare" and fields["request_id"] is None
    assert fields["duration_ms"] >= 0 and fields["reused"] is False
    assert set(fields) == {"stage", "request_id", "duration_ms", "status", "reused"}
