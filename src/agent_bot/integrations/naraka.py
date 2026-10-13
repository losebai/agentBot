"""小黑盒永劫无间第三方 HTTP API 适配器。"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from pydantic import ValidationError

from agent_bot.services.naraka import (
    _MATCH_ID_PATTERN,
    _ROLE_ID_PATTERN,
    NarakaAuthenticationError,
    NarakaDataUpdatingError,
    NarakaError,
    NarakaMatchDetail,
    NarakaNotFoundError,
    NarakaPlayerSearchItem,
    NarakaPlayerSearchResult,
    NarakaProtocolError,
    NarakaRejectedError,
    NarakaResult,
    NarakaUnavailableError,
    _looks_not_found,
    _NarakaPlayerSearchPayload,
    _NarakaPlayerSearchRow,
    _normalize_nickname,
    _normalize_player_name,
    _safe_optional_text,
    _safe_text,
)

NARAKA_API_URL = "https://api.xiaoheihe.cn/game/yjwj/home/data"
NARAKA_API_PATH = "/game/yjwj/home/data"
NARAKA_MATCH_DETAIL_API_URL = "https://api.xiaoheihe.cn/game/yjwj/match/detail"
NARAKA_MATCH_DETAIL_API_PATH = "/game/yjwj/match/detail"
NARAKA_PLAYER_SEARCH_API_URL = "https://api.xiaoheihe.cn/game/player_search/do"
NARAKA_PLAYER_SEARCH_API_PATH = "/game/player_search/do"
NARAKA_PLAYER_SEARCH_REPORT_API_URL = (
    "https://api.xiaoheihe.cn/game/player_search/report_search"
)
NARAKA_PLAYER_SEARCH_REPORT_API_PATH = "/game/player_search/report_search"
_WEB_ORIGIN = "https://web.xiaoheihe.cn"
_WEB_USER_AGENT = (
    "Mozilla/5.0 (Phone; OpenHarmony 7.0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 "
    "ArkWeb/7.0.0.33 Mobile ApiMaxJia/1.0"
)
_HKEY_ALPHABET = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"
_NUMERIC_ROLE_ID_PATTERN = re.compile(r"^[0-9]{1,18}$")
_GAME_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{4}[0-9]{18}$")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_REQUIRED_COOKIE_NAMES = frozenset({"pkey", "x_heybox_id", "x_pkey", "x_xhh_tokenid"})
_MAX_COOKIE_CHARS = 8192
_MAX_RESPONSE_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class WebSignature:
    """单次小黑盒 Web 请求签名。"""

    timestamp: int
    nonce: str
    hkey: str


def _canonical_path(path: str) -> str:
    """将请求路径规范为签名所需格式。"""

    components = [component for component in path.split("/") if component]
    if not components:
        raise ValueError("Naraka signature path cannot be empty")
    return f"/{'/'.join(components)}/"


def _map_chars(value: str, alphabet: str) -> str:
    """把字符按固定字母表映射。"""

    return "".join(alphabet[ord(character) % len(alphabet)] for character in value)


def _interleave_by_length(values: list[str]) -> str:
    """按长度排序后交错组合签名材料。"""

    ordered = sorted(values, key=len)
    return "".join(
        value[index]
        for index in range(max(map(len, ordered)))
        for value in ordered
        if index < len(value)
    )


def _xtime(value: int) -> int:
    """执行签名校验和的有限域倍乘。"""

    return (((value << 1) ^ 0x1B) & 0xFF) if value & 0x80 else value << 1


def _q(value: int) -> int:
    """执行签名校验和的 Q 变换。"""

    return _xtime(value) ^ value


def _dollar(value: int) -> int:
    """执行签名校验和的二阶变换。"""

    return _q(_xtime(value))


def _y(value: int) -> int:
    """执行签名校验和的三阶变换。"""

    return _dollar(_q(_xtime(value)))


def _g(value: int) -> int:
    """组合签名校验和变换。"""

    return _y(value) ^ _dollar(value) ^ _q(value)


def _mix_checksum(values: list[int]) -> list[int]:
    """混合六个摘要字节生成签名校验和。"""

    if len(values) != 6:
        raise ValueError("Naraka checksum input must contain six bytes")
    first, second, third, fourth = values[:4]
    return [
        _g(first) ^ _y(second) ^ _dollar(third) ^ _q(fourth),
        _q(first) ^ _g(second) ^ _y(third) ^ _dollar(fourth),
        _dollar(first) ^ _q(second) ^ _g(third) ^ _y(fourth),
        _y(first) ^ _dollar(second) ^ _q(third) ^ _g(fourth),
        values[4],
        values[5],
    ]


def create_hkey(
    path: str,
    *,
    timestamp: int,
    nonce: str,
    time_offset: int = 3,
) -> str:
    """复现小黑盒 Web 端 path/time/nonce 签名，不含账号密钥。"""

    if timestamp <= 0 or not re.fullmatch(r"[0-9A-F]{32}", nonce):
        raise ValueError("invalid Naraka signature inputs")
    canonical_path = _canonical_path(path)
    mapped_time = _map_chars(str(timestamp + time_offset), _HKEY_ALPHABET[:-2])
    mapped_path = _map_chars(canonical_path, _HKEY_ALPHABET)
    mapped_nonce = _map_chars(nonce, _HKEY_ALPHABET)
    material = _interleave_by_length([mapped_time, mapped_path, mapped_nonce])[:20]
    digest = hashlib.md5(material.encode(), usedforsecurity=False).hexdigest()
    checksum_values = [ord(character) for character in digest[-6:]]
    checksum = sum(_mix_checksum(checksum_values)) % 100
    prefix = _map_chars(digest[:5], _HKEY_ALPHABET[:-4])
    return f"{prefix}{checksum:02d}"


class WebHkeySigner:
    """为每个请求生成新的 Web hkey、时间戳与 nonce。"""

    def __init__(
        self,
        *,
        timestamp_factory: Callable[[], int] = lambda: int(time.time()),
        nonce_factory: Callable[[], str] = lambda: secrets.token_hex(16).upper(),
    ) -> None:
        """保存可替换的时间戳与随机数提供器。"""

        self._timestamp_factory = timestamp_factory
        self._nonce_factory = nonce_factory

    def sign(self, path: str) -> WebSignature:
        """为单个固定路径创建签名。"""

        timestamp = self._timestamp_factory()
        nonce = self._nonce_factory()
        return WebSignature(
            timestamp=timestamp,
            nonce=nonce,
            hkey=create_hkey(path, timestamp=timestamp, nonce=nonce),
        )


class NarakaClient:
    """调用固定小黑盒端点；每次请求隔离 Cookie jar 且不自动重试。"""

    def __init__(
        self,
        *,
        heybox_id: str,
        cookie: str,
        server: str,
        season: str,
        battle_tid: str,
        timeout_seconds: float,
        signer: WebHkeySigner | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """校验第三方 API 配置并保存无状态请求参数。"""

        if not heybox_id.isdigit() or len(heybox_id) > 32:
            raise ValueError("invalid Naraka account configuration")
        if not all(_IDENTIFIER_PATTERN.fullmatch(value) for value in (server, season, battle_tid)):
            raise ValueError("invalid Naraka query configuration")
        self._validate_cookie(cookie)
        if timeout_seconds <= 0:
            raise ValueError("Naraka timeout must be positive")
        self._heybox_id = heybox_id
        self._cookie = cookie
        self._server = server
        self._season = season
        self._battle_tid = battle_tid
        self._timeout = httpx.Timeout(timeout_seconds)
        self._signer = signer or WebHkeySigner()
        self._transport = transport

    @staticmethod
    def _validate_cookie(cookie: str) -> None:
        """校验 Cookie 长度、控制字符和必需字段。"""

        if not cookie or len(cookie) > _MAX_COOKIE_CHARS:
            raise ValueError("invalid Naraka cookie configuration")
        if any(character in cookie for character in "\r\n\0"):
            raise ValueError("invalid Naraka cookie configuration")
        names = {part.partition("=")[0].strip() for part in cookie.split(";") if "=" in part}
        if not _REQUIRED_COOKIE_NAMES.issubset(names):
            raise ValueError("incomplete Naraka cookie configuration")

    async def query(self, *, role_id: str, server: str | None = None) -> NarakaResult:
        """查询单个角色的战绩。"""

        if not _ROLE_ID_PATTERN.fullmatch(role_id):
            raise ValueError("invalid Naraka role ID")
        query_server = server or self._server
        if not _IDENTIFIER_PATTERN.fullmatch(query_server):
            raise ValueError("invalid Naraka server")
        query_role_id = (
            f"ukg1{role_id.zfill(18)}"
            if _NUMERIC_ROLE_ID_PATTERN.fullmatch(role_id)
            else role_id
        )
        raw_result = await self._request(
            url=NARAKA_API_URL,
            path=NARAKA_API_PATH,
            query_params={"server": query_server, "role_id": query_role_id},
        )
        if raw_result.get("wait_update") is True:
            raise NarakaDataUpdatingError("Naraka role data is updating")
        bind_account = raw_result.get("bind_account")
        result_message = raw_result.get("message")
        if bind_account in {0, "0"} or (
            isinstance(result_message, str) and _looks_not_found(result_message)
        ):
            raise NarakaNotFoundError("Naraka role is not bound")
        self._rebuild_match_ids(raw_result, query_role_id)
        try:
            return NarakaResult.model_validate(raw_result)
        except ValidationError:
            raise NarakaProtocolError("Naraka API result is invalid") from None

    @staticmethod
    def _rebuild_match_ids(raw_result: dict[str, object], query_role_id: str) -> None:
        """由角色 ID 和时间戳重建可查询的比赛 ID。"""

        matches = raw_result.get("matches")
        if not isinstance(matches, list):
            return
        for match in matches:
            if not isinstance(match, dict):
                continue
            raw_time = match.get("time")
            if isinstance(raw_time, bool):
                continue
            timestamp = str(raw_time).strip() if isinstance(raw_time, (int, str)) else ""
            candidate = f"{query_role_id}.{timestamp}"
            if _MATCH_ID_PATTERN.fullmatch(candidate):
                match["match_id"] = candidate

    async def query_match_detail(self, *, match_id: str) -> NarakaMatchDetail:
        """查询单场比赛详情。"""

        if not _MATCH_ID_PATTERN.fullmatch(match_id):
            raise ValueError("invalid Naraka match ID")
        raw_result = await self._request(
            url=NARAKA_MATCH_DETAIL_API_URL,
            path=NARAKA_MATCH_DETAIL_API_PATH,
            query_params={"match_id": match_id},
        )
        try:
            return NarakaMatchDetail.model_validate(raw_result)
        except ValidationError:
            raise NarakaProtocolError("Naraka match detail is invalid") from None

    async def search_players(self, *, nickname: str) -> NarakaPlayerSearchResult:
        """按昵称搜索角色列表。"""

        normalized_nickname = _normalize_nickname(nickname)
        raw_result = await self._request(
            url=NARAKA_PLAYER_SEARCH_API_URL,
            path=NARAKA_PLAYER_SEARCH_API_PATH,
            query_params={
                "client_type": "mobile",
                "game_type": "yjwj",
                "q": normalized_nickname,
                "offset": "0",
                "limit": "20",
            },
        )
        try:
            payload = _NarakaPlayerSearchPayload.model_validate(raw_result)
        except ValidationError:
            raise NarakaProtocolError("Naraka player search result is invalid") from None
        self._validate_search_header(payload)
        players = [
            player
            for row in payload.player_list
            if (player := self._decode_search_row(row)) is not None
        ]
        if payload.player_list and not players:
            raise NarakaProtocolError("Naraka player search rows are invalid")
        return NarakaPlayerSearchResult(query=normalized_nickname, players=players)

    async def report_player_search(
        self,
        *,
        game_id: str,
        nickname: str,
        server: str,
    ) -> None:
        """向第三方 API 报告用户选中的搜索结果。"""

        if not _GAME_ID_PATTERN.fullmatch(game_id):
            raise ValueError("invalid Naraka game ID")
        normalized_nickname = _normalize_player_name(nickname)
        if not _IDENTIFIER_PATTERN.fullmatch(server):
            raise ValueError("invalid Naraka server")
        await self._request(
            url=NARAKA_PLAYER_SEARCH_REPORT_API_URL,
            path=NARAKA_PLAYER_SEARCH_REPORT_API_PATH,
            query_params={
                "client_type": "mobile",
                "game_type": "yjwj",
                "game_id": game_id,
                "nickname": normalized_nickname,
                "ext": server,
            },
        )

    @staticmethod
    def _validate_search_header(payload: _NarakaPlayerSearchPayload) -> None:
        """验证第三方搜索列表的固定列契约。"""

        if not payload.player_list:
            return
        expected = ("斗士", "境界", "总积分", "场次")
        actual = tuple(_safe_text(column.text) for column in payload.header[:4])
        if actual != expected:
            raise NarakaProtocolError("Naraka player search header is invalid")

    @staticmethod
    def _decode_search_row(
        row: _NarakaPlayerSearchRow,
    ) -> NarakaPlayerSearchItem | None:
        """将通过 Pydantic 校验的第三方搜索行转换为领域模型。"""

        if not _GAME_ID_PATTERN.fullmatch(row.game_id) or len(row.column_list) < 4:
            return None
        columns = row.column_list
        if tuple(column.type for column in columns[:4]) != (
            "user_info",
            "icon_text",
            "text",
            "text",
        ):
            return None
        name = _safe_text(columns[0].text)
        server = _safe_optional_text(row.ext)
        if not name or server is None or not _IDENTIFIER_PATTERN.fullmatch(server):
            return None
        return NarakaPlayerSearchItem(
            name=name,
            game_id=row.game_id,
            server=server,
            rank=_safe_optional_text(columns[1].text),
            total_score=_safe_optional_text(columns[2].text),
            match_count=_safe_optional_text(columns[3].text),
        )

    async def _request(
        self,
        *,
        url: str,
        path: str,
        query_params: dict[str, str],
    ) -> dict[str, object]:
        """发送有大小限制、无重试的第三方 GET 请求。"""

        signature = self._signer.sign(path)
        params = {
            "app": "heybox",
            "heybox_id": self._heybox_id,
            "os_type": "web",
            "x_app": "heybox",
            "x_client_type": "web",
            "x_os_type": "ohos",
            "x_client_version": "1.3.390",
            "version": "999.0.4",
            "hkey": signature.hkey,
            "_time": str(signature.timestamp),
            "nonce": signature.nonce,
            **query_params,
        }
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Cookie": self._cookie,
            "Origin": _WEB_ORIGIN,
            "Referer": f"{_WEB_ORIGIN}/",
            "User-Agent": _WEB_USER_AGENT,
        }

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=False,
                trust_env=False,
            ) as http:
                async with http.stream(
                    "GET",
                    url,
                    params=params,
                    headers=headers,
                ) as response:
                    self._raise_for_status(response.status_code)
                    content_type = response.headers.get("content-type", "")
                    media_type = content_type.partition(";")[0].strip().lower()
                    if media_type not in {"application/json", "text/json"}:
                        raise NarakaProtocolError(
                            "Naraka API returned an invalid content type"
                        )
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > _MAX_RESPONSE_BYTES:
                            raise NarakaProtocolError("Naraka API response is too large")
        except NarakaError:
            raise
        except (httpx.TimeoutException, httpx.RequestError):
            raise NarakaUnavailableError("Naraka API is unavailable") from None

        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            raise NarakaProtocolError("Naraka API returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise NarakaProtocolError("Naraka API returned an invalid payload")
        return self._parse_payload(payload)

    @staticmethod
    def _raise_for_status(status_code: int) -> None:
        """把 HTTP 状态映射为稳定的第三方 API 异常。"""

        if status_code in {401, 403}:
            raise NarakaAuthenticationError("Naraka credentials were rejected")
        if status_code in {408, 425, 429} or status_code >= 500:
            raise NarakaUnavailableError("Naraka API is temporarily unavailable")
        if 300 <= status_code < 400:
            raise NarakaProtocolError("Naraka API returned a redirect")
        if status_code < 200 or status_code >= 300:
            raise NarakaRejectedError("Naraka API rejected the request")

    @staticmethod
    def _parse_payload(payload: dict[str, object]) -> dict[str, object]:
        """解析小黑盒固定响应信封并拒绝异常状态。"""

        status = payload.get("status")
        if not isinstance(status, str):
            raise NarakaProtocolError("Naraka API response has no status")
        normalized_status = status.casefold()
        message = payload.get("msg")
        safe_message = message if isinstance(message, str) else ""
        if normalized_status != "ok":
            if normalized_status in {"login", "auth", "unauthorized"} or "登录" in safe_message:
                raise NarakaAuthenticationError("Naraka credentials have expired")
            if normalized_status in {"not_found", "notfound"} or _looks_not_found(
                safe_message
            ):
                raise NarakaNotFoundError("Naraka role was not found")
            raise NarakaRejectedError("Naraka API rejected the query")

        raw_result = payload.get("result")
        if not isinstance(raw_result, dict):
            raise NarakaProtocolError("Naraka API response has no result")
        result_message = raw_result.get("message")
        if isinstance(result_message, str) and _looks_not_found(result_message):
            raise NarakaNotFoundError("Naraka data was not found")
        return raw_result

    async def aclose(self) -> None:
        """客户端按请求创建并关闭连接；保留统一生命周期接口。"""
