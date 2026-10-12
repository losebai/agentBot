"""数据库异常的受限重试分类与脱敏诊断。"""

from __future__ import annotations

from dataclasses import dataclass

from pymysql.err import DatabaseError, InterfaceError, OperationalError

_TRANSIENT_MYSQL_CODES = frozenset({1040, 1205, 1213, 2002, 2003, 2006, 2013, 2055})


class DatabasePoolTimeoutError(OperationalError):
    """连接池暂时没有可用容量，调用方可按既有预算重试。"""


@dataclass(frozen=True, slots=True)
class DatabaseFailure:
    """仅保留驱动类型和数字错误码，不保存驱动正文或连接参数。"""

    driver: str
    code: int | None
    transient: bool

    @classmethod
    def from_error(cls, error: BaseException) -> DatabaseFailure | None:
        """沿显式异常原因定位数据库故障，避免从消息文本猜测或泄露凭据。"""
        current: BaseException | None = error
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, (DatabaseError, InterfaceError)):
                code = current.args[0] if current.args and type(current.args[0]) is int else None
                transient = (
                    isinstance(current, DatabasePoolTimeoutError)
                    or (isinstance(current, OperationalError) and code in _TRANSIENT_MYSQL_CODES)
                    or (isinstance(current, InterfaceError) and code == 0)
                )
                return cls(type(current).__name__, code, transient)
            current = current.__cause__
        return None

    def __str__(self) -> str:
        """提供可记录到内部日志的固定格式诊断，不包含异常正文。"""
        return f"{self.driver}(code={self.code if self.code is not None else 'unknown'})"
