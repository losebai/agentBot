"""Bounded DB-API transactions through the existing connection owner."""

from collections.abc import Callable
from contextlib import closing
from typing import Any, TypeVar

import pymysql

from agent_bot.ai.errors import AiError
from agent_bot.database_pool import finish_database_write
from agent_bot.settings import DatabaseParams

T = TypeVar("T")


class AiDatabase:
    def __init__(self, database: DatabaseParams, *, connect: Callable[..., Any]):
        self.params = database
        self.connect = connect

    async def run(self, operation: Callable[[Any], T], *, write: bool = False) -> T:
        def transaction() -> T:
            try:
                with closing(
                    self.connect(
                        host=self.params.host,
                        port=self.params.port,
                        user=self.params.user,
                        password=self.params.password,
                        database=self.params.database,
                        charset="utf8mb4",
                        cursorclass=pymysql.cursors.DictCursor,
                        autocommit=False,
                        connect_timeout=5,
                        read_timeout=10,
                        write_timeout=10,
                    )
                ) as connection:
                    connection.begin()
                    try:
                        result = operation(connection)
                        if write:
                            connection.commit()
                        else:
                            connection.rollback()
                        return result
                    except BaseException:
                        connection.rollback()
                        raise
            except pymysql.IntegrityError as error:
                raise AiError("AI_CONFIG_CONFLICT", status_code=409) from error
            except pymysql.DatabaseError as error:
                code = (
                    "AI_NOT_INITIALIZED"
                    if error.args[0] in {1146, 1054}
                    else "AI_DATABASE_UNAVAILABLE"
                )
                raise AiError(code, status_code=503) from error

        # Reads also own connections: cancellation must join their bounded worker
        # before callers may release a runtime lease or close its database pool.
        return await finish_database_write(transaction)


def query(connection, sql: str, args: tuple = (), *, one: bool = False):
    with connection.cursor() as cursor:
        cursor.execute(sql, args)
        return cursor.fetchone() if one else cursor.fetchall()


def page_limit(limit: int) -> None:
    if isinstance(limit, bool) or not 1 <= limit <= 100:
        raise AiError("AI_INVALID_PAGE", status_code=422)
