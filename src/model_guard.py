"""每次调用 AI 前确认明确选择的模型在 ai_model 表中处于启用状态。"""

from __future__ import annotations

import asyncio
import math
import time
from collections import OrderedDict
from collections.abc import Callable
from contextlib import closing
from typing import Any

import pymysql
from pymysql.err import DatabaseError, InterfaceError, OperationalError

from agent_bot.settings import DatabaseParams


class ModelNotAvailableError(RuntimeError):
    """配置的模型在 ai_model 表中不存在或未启用。"""


class ModelGuardUnavailableError(ModelNotAvailableError):
    """模型目录暂时无法查询，可由持久队列安全重试。"""


class ModelGuard:
    """查询 ai_model 表，确认指定 model_id 当前 enabled=1。"""

    def __init__(
        self,
        params: DatabaseParams,
        *,
        connect: Callable[..., Any] | None = None,
        cache_ttl_seconds: float = 5,
        cache_max_size: int = 128,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """设置短期有界启用缓存，数据库查询仍由 Java 最终校验。"""
        if not math.isfinite(cache_ttl_seconds) or not 0 <= cache_ttl_seconds <= 30:
            raise ValueError("model cache TTL must be between 0 and 30 seconds")
        if not 1 <= cache_max_size <= 1024:
            raise ValueError("model cache capacity must be between 1 and 1024")
        self._params = params
        self._connect = connect or pymysql.connect
        self._cache_ttl = cache_ttl_seconds
        self._cache_max_size = cache_max_size
        self._clock = clock
        self._enabled: OrderedDict[str, float] = OrderedDict()
        self._checks: dict[str, asyncio.Task[None]] = {}

    async def ensure_enabled(self, model_id: str, *, capability: str = "java") -> None:
        """复用近期成功结果及同模型查询，取消等待者不影响其他调用。"""
        now = self._clock()
        if self._enabled.get(model_id, 0) > now:
            self._enabled.move_to_end(model_id)
            return
        self._enabled.pop(model_id, None)
        task = self._checks.get(model_id)
        if task is None:
            if len(self._checks) >= self._cache_max_size:
                raise ModelGuardUnavailableError("model status checks are busy")
            task = asyncio.create_task(asyncio.to_thread(self._check, model_id))
            self._checks[model_id] = task
            task.add_done_callback(lambda done: self._finish_check(model_id, done))
        await asyncio.shield(task)

    def _finish_check(self, model_id: str, task: asyncio.Task[None]) -> None:
        """只缓存成功启用状态，不缓存禁用或数据库错误。"""
        self._checks.pop(model_id, None)
        if task.cancelled() or task.exception() is not None or self._cache_ttl == 0:
            return
        self._enabled[model_id] = self._clock() + self._cache_ttl
        self._enabled.move_to_end(model_id)
        while len(self._enabled) > self._cache_max_size:
            self._enabled.popitem(last=False)

    def _check(self, model_id: str) -> None:
        """查询数据库并将连接错误转换为可重试错误。"""
        try:
            with closing(self._open()) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT enabled
                        FROM ai_model
                        WHERE model_id = %s AND type = 'chat'
                        LIMIT 1
                        """,
                        (model_id,),
                    )
                    row = cursor.fetchone()
        except (InterfaceError, OperationalError, DatabaseError) as exc:
            raise ModelGuardUnavailableError("failed to query ai_model table") from exc

        if row is None:
            raise ModelNotAvailableError(
                f"model '{model_id}' is not registered in ai_model table"
            )

        enabled = row[0] if isinstance(row, (tuple, list)) else row.get("enabled")
        if enabled != 1:
            raise ModelNotAvailableError(
                f"model '{model_id}' is disabled in ai_model table"
            )

    def _open(self) -> Any:
        """使用显式网络时限打开一次查询租用。"""
        return self._connect(
            host=self._params.host,
            port=self._params.port,
            user=self._params.user,
            password=self._params.password,
            database=self._params.database,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=5,
            read_timeout=10,
            write_timeout=10,
        )
