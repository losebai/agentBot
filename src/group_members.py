"""持久化 NapCat OneBot 群成员目录，供结构化 @ 解析使用。"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass
from typing import Any

import pymysql
from pymysql.err import DatabaseError, InterfaceError, OperationalError

from agent_bot.settings import DatabaseParams

_TABLE = "dispatch_chat_group_member"
_QQ_ID_PATTERN = re.compile(r"^[1-9][0-9]{4,19}$")
_MAX_GROUP_MEMBERS = 2000
_MAX_NAME_UTF16_UNITS = 128
_MAX_TIMESTAMP = 9_223_372_036_854_775_807
_MEMBER_ROLES = frozenset({"owner", "admin", "member"})


class GroupMemberStoreError(RuntimeError):
    """群成员目录数据库操作失败。"""


class GroupMemberValidationError(ValueError):
    """NapCat 群成员数据不满足安全边界。"""


@dataclass(frozen=True, slots=True, repr=False)
class _GroupMember:
    user_id: str
    nickname: str | None
    card: str | None
    role: str
    join_time: int | None
    last_sent_time: int | None

    def mention_payload(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "nickname": self.nickname or "",
            "card": self.card or "",
        }


class GroupMemberStore:
    """以 Bot + 群 + 成员为主键保存可用于 @ 解析的最小成员信息。"""

    def __init__(
        self,
        params: DatabaseParams,
        *,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self._params = params
        self._connect = connect or pymysql.connect

    async def replace_group(
        self,
        *,
        bot_id: object,
        group_id: object,
        group_name: object,
        members: object,
    ) -> list[dict[str, object]]:
        normalized_bot_id = _normalize_id(bot_id, "bot_id")
        normalized_group_id = _normalize_id(group_id, "group_id")
        normalized_group_name = _normalize_name(group_name)
        normalized_members = _normalize_members(members)
        await asyncio.to_thread(
            self._replace_group,
            normalized_bot_id,
            normalized_group_id,
            normalized_group_name,
            normalized_members,
        )
        return [member.mention_payload() for member in normalized_members]

    async def list_members(
        self,
        bot_id: object,
        group_id: object,
    ) -> list[dict[str, object]]:
        normalized_bot_id = _normalize_id(bot_id, "bot_id")
        normalized_group_id = _normalize_id(group_id, "group_id")
        return await asyncio.to_thread(
            self._list_members,
            normalized_bot_id,
            normalized_group_id,
        )

    async def upsert_member(
        self,
        *,
        bot_id: object,
        group_id: object,
        group_name: object,
        member: object,
    ) -> None:
        normalized_bot_id = _normalize_id(bot_id, "bot_id")
        normalized_group_id = _normalize_id(group_id, "group_id")
        normalized_group_name = _normalize_name(group_name)
        normalized_member = _normalize_member(member)
        await asyncio.to_thread(
            self._upsert_member,
            normalized_bot_id,
            normalized_group_id,
            normalized_group_name,
            normalized_member,
        )

    async def delete_member(
        self,
        bot_id: object,
        group_id: object,
        user_id: object,
    ) -> None:
        parameters = (
            _normalize_id(bot_id, "bot_id"),
            _normalize_id(group_id, "group_id"),
            _normalize_id(user_id, "user_id"),
        )
        await asyncio.to_thread(
            self._write_one,
            f"DELETE FROM `{_TABLE}` WHERE bot_id = %s AND group_id = %s AND user_id = %s",
            parameters,
            "failed to delete group member",
        )

    async def delete_group(self, bot_id: object, group_id: object) -> None:
        parameters = (
            _normalize_id(bot_id, "bot_id"),
            _normalize_id(group_id, "group_id"),
        )
        await asyncio.to_thread(
            self._write_one,
            f"DELETE FROM `{_TABLE}` WHERE bot_id = %s AND group_id = %s",
            parameters,
            "failed to delete group member snapshot",
        )

    def _replace_group(
        self,
        bot_id: str,
        group_id: str,
        group_name: str | None,
        members: tuple[_GroupMember, ...],
    ) -> None:
        rows = [
            (
                bot_id,
                group_id,
                member.user_id,
                group_name,
                member.nickname,
                member.card or member.nickname,
                member.role,
                member.join_time,
                member.last_sent_time,
            )
            for member in members
        ]
        try:
            with closing(self._open()) as connection:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            f"DELETE FROM `{_TABLE}` WHERE bot_id = %s AND group_id = %s",
                            (bot_id, group_id),
                        )
                        if rows:
                            cursor.executemany(
                                _INSERT_SQL,
                                rows,
                            )
                    connection.commit()
                except Exception:
                    with suppress(Exception):
                        connection.rollback()
                    raise
        except (InterfaceError, OperationalError, DatabaseError) as exc:
            raise GroupMemberStoreError("failed to persist group member snapshot") from exc

    def _list_members(self, bot_id: str, group_id: str) -> list[dict[str, object]]:
        try:
            with closing(self._open()) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        SELECT user_id, nickname, card
                        FROM `{_TABLE}`
                        WHERE bot_id = %s AND group_id = %s
                        ORDER BY user_id
                        """,
                        (bot_id, group_id),
                    )
                    rows = cursor.fetchall()
        except (InterfaceError, OperationalError, DatabaseError) as exc:
            raise GroupMemberStoreError("failed to load group member snapshot") from exc

        if (
            not isinstance(rows, Sequence)
            or isinstance(rows, (str, bytes, bytearray))
            or len(rows) > _MAX_GROUP_MEMBERS
        ):
            raise GroupMemberStoreError("group member snapshot is invalid")
        result: list[dict[str, object]] = []
        try:
            for row in rows:
                if not isinstance(row, Mapping):
                    raise GroupMemberValidationError("group member row is invalid")
                result.append(
                    {
                        "user_id": _normalize_id(row.get("user_id"), "user_id"),
                        "nickname": _normalize_name(row.get("nickname")) or "",
                        "card": _normalize_name(row.get("card")) or "",
                    }
                )
        except GroupMemberValidationError as exc:
            raise GroupMemberStoreError("group member snapshot is invalid") from exc
        return result

    def _upsert_member(
        self,
        bot_id: str,
        group_id: str,
        group_name: str | None,
        member: _GroupMember,
    ) -> None:
        self._write_one(
            _UPSERT_SQL,
            (
                bot_id,
                group_id,
                member.user_id,
                group_name,
                member.nickname,
                member.card,
                member.role,
                member.join_time,
                member.last_sent_time,
            ),
            "failed to upsert group member",
        )

    def _write_one(
        self,
        sql: str,
        parameters: tuple[object, ...],
        error_message: str,
    ) -> None:
        try:
            with closing(self._open()) as connection:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(sql, parameters)
                    connection.commit()
                except Exception:
                    with suppress(Exception):
                        connection.rollback()
                    raise
        except (InterfaceError, OperationalError, DatabaseError) as exc:
            raise GroupMemberStoreError(error_message) from exc

    def _open(self) -> Any:
        return self._connect(
            host=self._params.host,
            port=self._params.port,
            user=self._params.user,
            password=self._params.password,
            database=self._params.database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
        )


_INSERT_SQL = f"""
    INSERT INTO `{_TABLE}` (
        bot_id, group_id, user_id, group_name, nickname, card, member_role,
        join_time, last_sent_time
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_UPSERT_SQL = _INSERT_SQL + """
    ON DUPLICATE KEY UPDATE
        group_name = COALESCE(VALUES(group_name), group_name),
        nickname = VALUES(nickname),
        card = VALUES(card),
        member_role = VALUES(member_role),
        join_time = VALUES(join_time),
        last_sent_time = VALUES(last_sent_time)
"""


def _normalize_members(value: object) -> tuple[_GroupMember, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise GroupMemberValidationError("group member snapshot must be a sequence")
    if len(value) > _MAX_GROUP_MEMBERS:
        raise GroupMemberValidationError(
            f"group member snapshot cannot exceed {_MAX_GROUP_MEMBERS} rows"
        )
    members = tuple(_normalize_member(item) for item in value)
    if len({member.user_id for member in members}) != len(members):
        raise GroupMemberValidationError("group member snapshot contains duplicate users")
    return members


def _normalize_member(value: object) -> _GroupMember:
    if not isinstance(value, Mapping):
        raise GroupMemberValidationError("group member row must be an object")
    role_value = value.get("role", "member")
    role = role_value if isinstance(role_value, str) else "member"
    if role not in _MEMBER_ROLES:
        role = "member"
    return _GroupMember(
        user_id=_normalize_id(value.get("user_id"), "user_id"),
        nickname=_normalize_name(value.get("nickname")),
        card=_normalize_name(value.get("card")),
        role=role,
        join_time=_normalize_timestamp(value.get("join_time")),
        last_sent_time=_normalize_timestamp(value.get("last_sent_time")),
    )


def _normalize_id(value: object, field_name: str) -> str:
    rendered = str(value).strip()
    if _QQ_ID_PATTERN.fullmatch(rendered) is None:
        raise GroupMemberValidationError(f"{field_name} is invalid")
    return rendered


def _normalize_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    sanitized = "".join(
        " " if character.isspace() else character
        for character in value[:1024]
        if character.isspace() or not unicodedata.category(character).startswith("C")
    )
    normalized = " ".join(unicodedata.normalize("NFKC", sanitized).split())
    if not normalized:
        return None
    units = 0
    result: list[str] = []
    for character in normalized:
        character_units = 2 if ord(character) > 0xFFFF else 1
        if units + character_units > _MAX_NAME_UTF16_UNITS:
            break
        result.append(character)
        units += character_units
    return "".join(result) or None


def _normalize_timestamp(value: object) -> int | None:
    if type(value) is not int or value < 0 or value > _MAX_TIMESTAMP:
        return None
    return value
