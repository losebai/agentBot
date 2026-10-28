"""以固定安全字段记录 LLM 链路耗时，不接受正文、地址或凭据负载。"""

import json
import logging
from time import monotonic

from nonebot.log import LoguruHandler

logger = logging.getLogger(__name__)
# 应用默认只桥接 Uvicorn 日志；独立接入当前指标，避免 INFO 在根日志级别被丢弃。
logger.setLevel(logging.INFO)
logger.addHandler(LoguruHandler())


def record_timing(
    stage: str,
    request_id: str | None,
    started_monotonic: float,
    *,
    status: str,
    model: str | None = None,
    turn: int | None = None,
    first_text_ms: float | None = None,
    reused: bool | None = None,
    operation: str | None = None,
) -> None:
    """记录可信阶段名、生成的执行标识和模型记录引用；首正文从同一起点计时。"""
    fields = {
        "stage": stage,
        "request_id": request_id,
        "duration_ms": (monotonic() - started_monotonic) * 1000,
        "status": status,
    }
    fields.update({name: value for name, value in {
        "model": model, "turn": turn, "first_text_ms": first_text_ms,
        "reused": reused, "operation": operation,
    }.items() if value is not None})
    # 文本日志也输出 JSON，未启用结构化格式器时仍可观察；extra 便于日志采集与测试。
    logger.info("llm_timing %s", json.dumps(fields, ensure_ascii=False),
                extra={"llm_timing": fields})
