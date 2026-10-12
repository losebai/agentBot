"""永劫无间战绩领域模型、输入规范与查询编排服务。"""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

_ROLE_ID_PATTERN = re.compile(r"^(?:[0-9]{1,18}|[A-Za-z0-9]{4}[0-9]{18})$")
_MATCH_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{4}[0-9]{18}\.[0-9]{10}$")
_MAX_NICKNAME_CHARS = 32

Scalar = str | int | float
_ResultT = TypeVar("_ResultT")


class NarakaError(RuntimeError):
    """永劫查询错误基类。"""


class NarakaAuthenticationError(NarakaError):
    """小黑盒登录态或签名已失效。"""


class NarakaUnavailableError(NarakaError):
    """网络、限流或上游服务暂时不可用。"""


class NarakaRejectedError(NarakaError):
    """小黑盒拒绝了查询。"""


class NarakaNotFoundError(NarakaError):
    """未找到或未绑定目标角色。"""


class NarakaDataUpdatingError(NarakaError):
    """小黑盒正在更新目标角色的战绩数据。"""


class NarakaProtocolError(NarakaError):
    """小黑盒响应不符合预期。"""


class NarakaBusyError(RuntimeError):
    """当前会话或全局永劫查询已占满。"""


class NarakaRateLimitedError(RuntimeError):
    """同一会话查询过于频繁。"""

    def __init__(self, retry_after: float) -> None:
        super().__init__("Naraka query rate limited")
        self.retry_after = retry_after


class _NarakaModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class NarakaPlayerInfo(_NarakaModel):
    name: str = Field(min_length=1)
    rating: Scalar | None = None
    level: Scalar | None = None
    lv: Scalar | None = None


class NarakaMetric(_NarakaModel):
    desc: str = ""
    value: Scalar | None = None
    grade: str | None = None


class NarakaMatch(_NarakaModel):
    time: Scalar | None = None
    map_name: str | None = None
    rank: Scalar | None = None
    kill_times: Scalar | None = None
    damage: Scalar | None = None
    rating_delta: Scalar | None = None
    match_id: str | None = None


class NarakaWeapon(_NarakaModel):
    name: str = ""
    kill_times: Scalar | None = None
    damage: Scalar | None = None


class NarakaTag(_NarakaModel):
    name: str = ""
    desc: str = ""


class NarakaMatchDetail(_NarakaModel):
    name: str = Field(min_length=1)
    rating: Scalar | None = None
    battle_tid: str | None = None
    level: Scalar | None = None
    grade: str | None = None
    rank: Scalar | None = None
    rating_delta: Scalar | None = None
    time: Scalar | None = None
    map_name: str | None = None
    play_num: Scalar | None = None
    weapon_list: list[NarakaWeapon] = Field(default_factory=list)
    data: list[NarakaMetric] = Field(default_factory=list)
    tags: list[NarakaTag] = Field(default_factory=list)

    @field_validator("weapon_list", "data", "tags", mode="before")
    @classmethod
    def replace_null_lists(cls, value: object) -> object:
        return [] if value is None else value


class _NarakaPlayerSearchColumn(_NarakaModel):
    text: Scalar | None = None
    type: str = ""


class _NarakaPlayerSearchRow(_NarakaModel):
    column_list: list[_NarakaPlayerSearchColumn] = Field(default_factory=list)
    ext: Scalar | None = None
    game_id: str = ""

    @field_validator("column_list", mode="before")
    @classmethod
    def replace_null_columns(cls, value: object) -> object:
        return [] if value is None else value


class _NarakaPlayerSearchPayload(_NarakaModel):
    header: list[_NarakaPlayerSearchColumn] = Field(default_factory=list)
    player_list: list[_NarakaPlayerSearchRow] = Field(default_factory=list)

    @field_validator("header", "player_list", mode="before")
    @classmethod
    def replace_null_lists(cls, value: object) -> object:
        return [] if value is None else value


class NarakaPlayerSearchItem(_NarakaModel):
    name: str = Field(min_length=1, max_length=80)
    game_id: str = Field(pattern=r"^[A-Za-z0-9]{4}[0-9]{18}$")
    server: str | None = None
    rank: str | None = None
    total_score: str | None = None
    match_count: str | None = None


class NarakaPlayerSearchResult(_NarakaModel):
    query: str = Field(min_length=1, max_length=_MAX_NICKNAME_CHARS)
    players: list[NarakaPlayerSearchItem] = Field(default_factory=list)

    @field_validator("players", mode="before")
    @classmethod
    def replace_null_players(cls, value: object) -> object:
        return [] if value is None else value


class NarakaScoreInfo(_NarakaModel):
    composite_score: Scalar | None = None
    score_grade: str | None = None


class NarakaResult(_NarakaModel):
    player_info: NarakaPlayerInfo
    server_desc: str | None = None
    season: str | None = None
    battle_tid: str | None = None
    bind_account: Scalar | None = None
    message: str | None = None
    score_info: NarakaScoreInfo = Field(default_factory=NarakaScoreInfo)
    recent_avg_rank: Scalar | None = None
    overview: list[NarakaMetric] = Field(default_factory=list)
    matches: list[NarakaMatch] = Field(default_factory=list)

    @field_validator("score_info", mode="before")
    @classmethod
    def replace_null_score_info(cls, value: object) -> object:
        return {} if value is None else value

    @field_validator("overview", "matches", mode="before")
    @classmethod
    def replace_null_lists(cls, value: object) -> object:
        return [] if value is None else value


class _NarakaQueryClient(Protocol):
    async def query(
        self,
        *,
        role_id: str,
        server: str | None = None,
    ) -> NarakaResult: ...

    async def query_match_detail(self, *, match_id: str) -> NarakaMatchDetail: ...

    async def search_players(self, *, nickname: str) -> NarakaPlayerSearchResult: ...

    async def report_player_search(
        self,
        *,
        game_id: str,
        nickname: str,
        server: str,
    ) -> None: ...


class NarakaQueryService:
    """为第三方查询提供独立的会话冷却和快速并发拒绝。"""

    def __init__(
        self,
        *,
        client: _NarakaQueryClient,
        cooldown_seconds: float,
        max_concurrent_requests: int,
        max_rate_limit_entries: int = 1000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if cooldown_seconds < 0 or max_concurrent_requests <= 0:
            raise ValueError("invalid Naraka query limits")
        if max_rate_limit_entries <= 0:
            raise ValueError("invalid Naraka rate-limit capacity")
        self._client = client
        self._cooldown_seconds = cooldown_seconds
        self._max_rate_limit_entries = max_rate_limit_entries
        self._clock = clock
        self._semaphore = asyncio.Semaphore(max_concurrent_requests)
        self._in_flight: set[str] = set()
        self._next_allowed: OrderedDict[str, float] = OrderedDict()

    async def query(self, session_id: str, role_id: str) -> NarakaResult:
        normalized_role_id = role_id.strip()
        if not _ROLE_ID_PATTERN.fullmatch(normalized_role_id):
            raise ValueError("invalid Naraka role ID")
        return await self._run_query(
            session_id,
            lambda: self._client.query(role_id=normalized_role_id),
        )

    async def query_match_detail(self, session_id: str, match_id: str) -> NarakaMatchDetail:
        normalized_match_id = match_id.strip()
        if not _MATCH_ID_PATTERN.fullmatch(normalized_match_id):
            raise ValueError("invalid Naraka match ID")
        return await self._run_query(
            session_id,
            lambda: self._client.query_match_detail(match_id=normalized_match_id),
        )

    async def search_players(self, session_id: str, nickname: str) -> NarakaPlayerSearchResult:
        normalized_nickname = _normalize_nickname(nickname)
        return await self._run_query(
            session_id,
            lambda: self._client.search_players(nickname=normalized_nickname),
        )

    async def query_by_nickname(self, session_id: str, nickname: str) -> NarakaResult:
        normalized_nickname = _normalize_nickname(nickname)

        async def query_first_player() -> NarakaResult:
            search_result = await self._client.search_players(nickname=normalized_nickname)
            if not search_result.players:
                raise NarakaNotFoundError("Naraka nickname was not found")
            player = search_result.players[0]
            assert player.server is not None
            try:
                await self._client.report_player_search(
                    game_id=player.game_id,
                    nickname=player.name,
                    server=player.server,
                )
            except NarakaError:
                # This endpoint only records the selected search result.  Its
                # failure must not prevent the authoritative profile request.
                pass
            return await self._client.query(
                role_id=player.game_id,
                server=player.server,
            )

        return await self._run_query(session_id, query_first_player)

    async def _run_query(
        self,
        session_id: str,
        operation: Callable[[], Awaitable[_ResultT]],
    ) -> _ResultT:
        if session_id in self._in_flight or self._semaphore.locked():
            raise NarakaBusyError("Naraka query already in progress")
        self._consume_cooldown(session_id)
        self._in_flight.add(session_id)
        try:
            async with self._semaphore:
                return await operation()
        finally:
            self._in_flight.discard(session_id)

    def _consume_cooldown(self, session_id: str) -> None:
        if self._cooldown_seconds == 0:
            return
        now = self._clock()
        next_allowed = self._next_allowed.get(session_id, 0)
        if now < next_allowed:
            raise NarakaRateLimitedError(next_allowed - now)
        self._next_allowed[session_id] = now + self._cooldown_seconds
        self._next_allowed.move_to_end(session_id)
        while len(self._next_allowed) > self._max_rate_limit_entries:
            self._next_allowed.popitem(last=False)


def _looks_not_found(message: str) -> bool:
    return any(keyword in message for keyword in ("未绑定", "不存在", "未找到"))


def is_naraka_role_id(value: str) -> bool:
    """判断用户输入是否为数字角色 ID 或完整 game_id。"""

    return _ROLE_ID_PATTERN.fullmatch(value.strip()) is not None


def is_naraka_match_id(value: str) -> bool:
    """判断是否为 ukg10000{角色ID}.{10位时间戳} 格式的比赛 ID。"""

    return _MATCH_ID_PATTERN.fullmatch(value.strip()) is not None


def _normalize_nickname(value: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > _MAX_NICKNAME_CHARS
        or any(unicodedata.category(character).startswith("C") for character in normalized)
    ):
        raise ValueError("invalid Naraka nickname")
    return normalized


def _normalize_player_name(value: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 80
        or any(unicodedata.category(character).startswith("C") for character in normalized)
    ):
        raise ValueError("invalid Naraka player name")
    return normalized


def _safe_text(value: object, *, limit: int = 80) -> str:
    raw = str(value).strip()
    cleaned = "".join(
        " " if character.isspace() else character
        for character in raw
        if not unicodedata.category(character).startswith("C") or character.isspace()
    )
    return " ".join(cleaned.split())[:limit]


def _safe_optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = _safe_text(value)
    return text or None


