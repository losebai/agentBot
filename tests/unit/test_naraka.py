from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from agent_bot.integrations.naraka import (
    NarakaClient,
    WebHkeySigner,
    create_hkey,
)
from agent_bot.services.naraka import (
    NarakaAuthenticationError,
    NarakaBusyError,
    NarakaDataUpdatingError,
    NarakaMatchDetail,
    NarakaNotFoundError,
    NarakaPlayerSearchResult,
    NarakaProtocolError,
    NarakaQueryService,
    NarakaRateLimitedError,
    NarakaRejectedError,
    NarakaResult,
    NarakaUnavailableError,
)
from agent_bot.services.naraka_formatting import (
    format_naraka_match_detail,
    format_naraka_player_search,
    format_naraka_summary,
)

FIXED_TIME = 1_700_000_997
FIXED_NONCE = "0123456789ABCDEF0123456789ABCDEF"
FAKE_COOKIE = "pkey=fake-pkey; x_heybox_id=123456; x_pkey=fake-pkey; x_xhh_tokenid=fake-token"


def success_payload() -> dict[str, object]:
    return {
        "status": "ok",
        "msg": "",
        "result": {
            "server_desc": "网易国服",
            "season": "qianji",
            "battle_tid": "5000001",
            "role_id": "must-not-be-rendered",
            "friends": [{"heybox_id": "must-not-be-rendered"}],
            "player_info": {
                "name": "测试玩家",
                "rating": "2450",
                "level": 88,
                "game_id": "must-not-be-rendered",
            },
            "score_info": {
                "composite_score": "91.2",
                "score_grade": "S",
            },
            "recent_avg_rank": 3.5,
            "overview": [
                {"desc": "场均伤害", "value": 12345},
                {"desc": "总场次", "value": "27"},
                {"desc": "KD", "value": "4.2"},
                {"desc": "夺冠率", "value": "18%"},
            ],
            "matches": [
                {
                    "time": "08-08 20:00",
                    "map_name": "聚窟洲",
                    "rank": 1,
                    "kill_times": 8,
                    "damage": 15678,
                    "rating_delta": 36,
                    "match_id": "ukg1000012345678900001.1700000000",
                }
            ],
        },
    }


def match_detail_payload() -> dict[str, object]:
    return {
        "status": "ok",
        "msg": "",
        "result": {
            "name": "测试玩家",
            "rating": "4120",
            "battle_tid": "5000001",
            "level": "坠日Ⅳ",
            "grade": "S",
            "rank": 1,
            "rating_delta": "42",
            "time": 1_700_123_456,
            "map_name": "龙隐洞天",
            "play_num": 3,
            "weapon_list": [
                {"name": "拳刃", "kill_times": 5, "damage": 7691},
                {"name": "其他", "kill_times": 2, "damage": 1225},
            ],
            "data": [
                {"grade": "S", "value": "33.8k", "desc": "总伤害"},
                {"value": "9", "desc": "击杀"},
                {"value": "3", "desc": "振刀"},
                {"value": "24min", "desc": "存活时长"},
            ],
            "tags": [
                {"name": "阎王", "desc": "累计淘汰5名玩家"},
                {"name": "利刃", "desc": "伤害量达到12500"},
            ],
            "all_team": [{"heybox_id": "must-not-be-rendered"}],
            "bg_img": "https://attacker.invalid/must-not-be-rendered",
        },
    }


def player_search_payload() -> dict[str, object]:
    return {
        "status": "ok",
        "msg": "",
        "result": {
            "header": [
                {"text": "斗士", "type": "user_info"},
                {"text": "境界", "type": "icon_text"},
                {"text": "总积分", "type": "text"},
                {"text": "场次", "type": "text"},
            ],
            "player_list": [
                {
                    "column_list": [
                        {"text": "测试昵称一", "type": "user_info"},
                        {"text": "坠日Ⅳ", "type": "icon_text"},
                        {"text": "4321", "type": "text"},
                        {"text": "5469", "type": "text"},
                    ],
                    "ext": "163",
                    "game_id": "abcd000012345678901234",
                    "private_url": "https://attacker.invalid/must-not-be-rendered",
                },
                {
                    "column_list": [
                        {"text": "测试昵称二", "type": "user_info"},
                        {"text": "陨星Ⅰ", "type": "icon_text"},
                        {"text": "3442", "type": "text"},
                        {"text": "4411", "type": "text"},
                    ],
                    "ext": "163",
                    "game_id": "wxyz000000987654321163",
                },
            ],
        },
    }


def fixed_signer() -> WebHkeySigner:
    return WebHkeySigner(
        timestamp_factory=lambda: FIXED_TIME,
        nonce_factory=lambda: FIXED_NONCE,
    )


def make_client(
    handler,
    *,
    cookie: str = FAKE_COOKIE,
) -> NarakaClient:
    return NarakaClient(
        heybox_id="123456",
        cookie=cookie,
        server="163",
        season="qianji",
        battle_tid="5000001",
        timeout_seconds=3,
        signer=fixed_signer(),
        transport=httpx.MockTransport(handler),
    )


def test_web_hkey_uses_canonical_path_and_time_offset() -> None:
    assert (
        create_hkey(
            "/game/yjwj/home/data",
            timestamp=FIXED_TIME,
            nonce=FIXED_NONCE,
        )
        == "WPSTU14"
    )
    assert (
        create_hkey(
            "//game/yjwj/home/data/",
            timestamp=FIXED_TIME,
            nonce=FIXED_NONCE,
        )
        == "WPSTU14"
    )


def test_signer_generates_uppercase_nonce_and_current_timestamp() -> None:
    signature = fixed_signer().sign("/game/yjwj/home/data")

    assert signature.timestamp == FIXED_TIME
    assert signature.nonce == FIXED_NONCE
    assert signature.hkey == "WPSTU14"


@pytest.mark.asyncio
async def test_client_sends_expected_fixed_host_contract() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.method == "GET"
        assert request.url.scheme == "https"
        assert request.url.host == "api.xiaoheihe.cn"
        assert request.url.path == "/game/yjwj/home/data"
        assert dict(request.url.params) == {
            "app": "heybox",
            "heybox_id": "123456",
            "os_type": "web",
            "x_app": "heybox",
            "x_client_type": "web",
            "x_os_type": "ohos",
            "x_client_version": "1.3.390",
            "version": "999.0.4",
            "hkey": "WPSTU14",
            "_time": str(FIXED_TIME),
            "nonce": FIXED_NONCE,
            "server": "163",
            "role_id": "ukg1000017605500010163",
        }
        assert request.headers["cookie"] == FAKE_COOKIE
        assert request.headers["origin"] == "https://web.xiaoheihe.cn"
        assert request.headers["referer"] == "https://web.xiaoheihe.cn/"
        return httpx.Response(200, json=success_payload())

    result = await make_client(handler).query(role_id="17605500010163")

    assert calls == 1
    assert result.player_info.name == "测试玩家"
    assert result.player_info.level == 88


@pytest.mark.asyncio
async def test_client_preserves_complete_naraka_game_id() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["role_id"] == "ukg1000017605500010163"
        return httpx.Response(200, json=success_payload())

    result = await make_client(handler).query(role_id="ukg1000017605500010163")

    assert result.player_info.name == "测试玩家"


@pytest.mark.asyncio
async def test_client_rebuilds_match_id_from_queried_role_and_match_time() -> None:
    payload = success_payload()
    result_payload = payload["result"]
    assert isinstance(result_payload, dict)
    matches = result_payload["matches"]
    assert isinstance(matches, list)
    match = matches[0]
    assert isinstance(match, dict)
    match["time"] = "1786000123"
    match["match_id"] = "ukg1000017605500010163.1786282570"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["role_id"] == "ukg1000012511500280163"
        return httpx.Response(200, json=payload)

    result = await make_client(handler).query(role_id="12511500280163")

    assert result.matches[0].match_id == "ukg1000012511500280163.1786000123"


@pytest.mark.asyncio
async def test_client_sends_expected_match_detail_contract() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.scheme == "https"
        assert request.url.host == "api.xiaoheihe.cn"
        assert request.url.path == "/game/yjwj/match/detail"
        assert dict(request.url.params) == {
            "app": "heybox",
            "heybox_id": "123456",
            "os_type": "web",
            "x_app": "heybox",
            "x_client_type": "web",
            "x_os_type": "ohos",
            "x_client_version": "1.3.390",
            "version": "999.0.4",
            "hkey": "WPSTU14",
            "_time": str(FIXED_TIME),
            "nonce": FIXED_NONCE,
            "match_id": "7pra000013743400100163.1729693893",
        }
        assert request.headers["cookie"] == FAKE_COOKIE
        assert request.headers["origin"] == "https://web.xiaoheihe.cn"
        assert request.headers["referer"] == "https://web.xiaoheihe.cn/"
        return httpx.Response(200, json=match_detail_payload())

    result = await make_client(handler).query_match_detail(
        match_id="7pra000013743400100163.1729693893"
    )

    assert result.name == "测试玩家"
    assert result.rank == 1
    assert result.weapon_list[0].name == "拳刃"


@pytest.mark.asyncio
async def test_client_sends_expected_player_search_contract_and_preserves_game_ids() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.scheme == "https"
        assert request.url.host == "api.xiaoheihe.cn"
        assert request.url.path == "/game/player_search/do"
        assert dict(request.url.params) == {
            "app": "heybox",
            "heybox_id": "123456",
            "os_type": "web",
            "x_app": "heybox",
            "x_client_type": "web",
            "x_os_type": "ohos",
            "x_client_version": "1.3.390",
            "version": "999.0.4",
            "hkey": "Z0PD231",
            "_time": str(FIXED_TIME),
            "nonce": FIXED_NONCE,
            "client_type": "mobile",
            "game_type": "yjwj",
            "q": "测试 昵称",
            "offset": "0",
            "limit": "20",
        }
        assert request.headers["cookie"] == FAKE_COOKIE
        return httpx.Response(200, json=player_search_payload())

    result = await make_client(handler).search_players(nickname="  测试 昵称  ")

    assert result.query == "测试 昵称"
    assert [player.game_id for player in result.players] == [
        "abcd000012345678901234",
        "wxyz000000987654321163",
    ]
    assert result.players[0].name == "测试昵称一"
    assert result.players[0].rank == "坠日Ⅳ"
    assert result.players[0].total_score == "4321"
    assert result.players[0].match_count == "5469"


@pytest.mark.asyncio
async def test_client_sends_expected_player_search_report_contract() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.scheme == "https"
        assert request.url.host == "api.xiaoheihe.cn"
        assert request.url.path == "/game/player_search/report_search"
        assert dict(request.url.params) == {
            "app": "heybox",
            "heybox_id": "123456",
            "os_type": "web",
            "x_app": "heybox",
            "x_client_type": "web",
            "x_os_type": "ohos",
            "x_client_version": "1.3.390",
            "version": "999.0.4",
            "hkey": "U1DI721",
            "_time": str(FIXED_TIME),
            "nonce": FIXED_NONCE,
            "client_type": "mobile",
            "game_type": "yjwj",
            "game_id": "7pra000013743400100163",
            "nickname": "下水道de哥布林",
            "ext": "163",
        }
        assert request.headers["cookie"] == FAKE_COOKIE
        return httpx.Response(200, json={"status": "ok", "msg": "", "result": {}})

    await make_client(handler).report_player_search(
        game_id="7pra000013743400100163",
        nickname="下水道de哥布林",
        server="163",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("game_id", "nickname", "server"),
    [
        ("https://attacker.invalid/", "玩家", "163"),
        ("7pra000013743400100163", "玩家\n注入", "163"),
        ("7pra000013743400100163", "玩家", "../secret"),
    ],
)
async def test_client_rejects_invalid_player_search_report_before_request(
    game_id: str,
    nickname: str,
    server: str,
) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"status": "ok", "msg": "", "result": {}})

    with pytest.raises(ValueError):
        await make_client(handler).report_player_search(
            game_id=game_id,
            nickname=nickname,
            server=server,
        )

    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("nickname", ["", "   ", "坏\n昵称", "a" * 33])
async def test_client_rejects_invalid_player_search_before_request(nickname: str) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=player_search_payload())

    with pytest.raises(ValueError):
        await make_client(handler).search_players(nickname=nickname)

    assert calls == 0


@pytest.mark.asyncio
async def test_client_omits_malformed_search_rows_but_rejects_fully_invalid_contract() -> None:
    body = player_search_payload()
    result = body["result"]
    assert isinstance(result, dict)
    players = result["player_list"]
    assert isinstance(players, list)
    players[0]["game_id"] = "https://attacker.invalid/"

    async def partly_valid_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    search = await make_client(partly_valid_handler).search_players(nickname="测试")
    assert [player.name for player in search.players] == ["测试昵称二"]

    players[1]["game_id"] = "also-invalid"

    async def invalid_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(NarakaProtocolError):
        await make_client(invalid_handler).search_players(nickname="测试")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "match_id",
    ["", "ab", "https://attacker.invalid/", "../secret", "a" * 129],
)
async def test_client_rejects_invalid_match_id_before_request(match_id: str) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=match_detail_payload())

    with pytest.raises(ValueError):
        await make_client(handler).query_match_detail(match_id=match_id)

    assert calls == 0


@pytest.mark.asyncio
async def test_match_detail_maps_not_found_result() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "ok", "msg": "", "result": {"message": "未找到比赛"}},
        )

    with pytest.raises(NarakaNotFoundError):
        await make_client(handler).query_match_detail(
            match_id="ukg1000017605500010163.1786282570"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, NarakaAuthenticationError),
        (403, NarakaAuthenticationError),
        (429, NarakaUnavailableError),
        (503, NarakaUnavailableError),
        (302, NarakaProtocolError),
        (400, NarakaRejectedError),
    ],
)
async def test_client_classifies_http_status_without_following_redirects(
    status: int,
    error_type: type[Exception],
) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            headers={"location": "https://attacker.invalid/steal"},
            text=f"sensitive body {FAKE_COOKIE}",
        )

    with pytest.raises(error_type) as exc_info:
        await make_client(handler).query(role_id="7pra000013743400100163")

    assert calls == 1
    assert FAKE_COOKIE not in str(exc_info.value)
    assert "sensitive body" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_client_maps_login_business_status_to_authentication_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "login", "msg": f"expired {FAKE_COOKIE}", "result": {}},
        )

    with pytest.raises(NarakaAuthenticationError) as exc_info:
        await make_client(handler).query(role_id="7pra000013743400100163")

    assert FAKE_COOKIE not in str(exc_info.value)


@pytest.mark.asyncio
async def test_client_maps_unbound_result_to_not_found() -> None:
    body = success_payload()
    result = body["result"]
    assert isinstance(result, dict)
    result["bind_account"] = 0
    result["message"] = "未绑定角色"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(NarakaNotFoundError):
        await make_client(handler).query(role_id="7pra000013743400100163")


@pytest.mark.asyncio
async def test_client_maps_wait_update_result_to_data_updating() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "ok", "msg": "", "result": {"wait_update": True}},
        )

    with pytest.raises(NarakaDataUpdatingError):
        await make_client(handler).query(role_id="47166000270163")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b"not json", "application/json"),
        (b"{}", "text/html"),
        (b"[]", "application/json"),
    ],
)
async def test_client_rejects_invalid_protocol(content: bytes, content_type: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content, headers={"content-type": content_type})

    with pytest.raises(NarakaProtocolError):
        await make_client(handler).query(role_id="7pra000013743400100163")


@pytest.mark.asyncio
async def test_client_maps_network_error_without_secret_or_exception_chain() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"network leaked {FAKE_COOKIE} {request.url}",
            request=request,
        )

    with pytest.raises(NarakaUnavailableError) as exc_info:
        await make_client(handler).query(role_id="7pra000013743400100163")

    assert FAKE_COOKIE not in str(exc_info.value)
    assert "7pra000013743400100163" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_client_does_not_reuse_upstream_set_cookie_between_queries() -> None:
    received_cookies: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        received_cookies.append(request.headers["cookie"])
        return httpx.Response(
            200,
            json=success_payload(),
            headers={"set-cookie": "x_xhh_tokenid=poisoned; Path=/"},
        )

    client = make_client(handler)
    await client.query(role_id="7pra000013743400100163")
    await client.query(role_id="7pra000013743400100163")

    assert received_cookies == [FAKE_COOKIE, FAKE_COOKIE]


def test_client_repr_does_not_expose_cookie() -> None:
    client = make_client(lambda request: httpx.Response(200, json=success_payload()))

    assert FAKE_COOKIE not in repr(client)


def test_client_rejects_header_injection_without_exposing_cookie() -> None:
    poisoned = f"{FAKE_COOKIE}\r\nX-Leak: secret"

    with pytest.raises(ValueError) as exc_info:
        make_client(lambda request: httpx.Response(200), cookie=poisoned)

    assert poisoned not in str(exc_info.value)
    assert poisoned not in repr(exc_info.value)


def test_summary_uses_metric_names_and_omits_private_fields() -> None:
    result = NarakaResult.model_validate(success_payload()["result"])

    summary = format_naraka_summary(result)

    assert "玩家：测试玩家" in summary
    assert "赛季：千机" in summary
    assert "总场次 27" in summary
    assert "夺冠率 18%" in summary
    assert "KD 4.2" in summary
    assert "场均伤害 12345" in summary
    assert "聚窟洲" in summary
    assert "积分 +36" in summary
    assert "比赛ID ukg1000012345678900001.1700000000" in summary
    assert "must-not-be-rendered" not in summary


def test_summary_handles_missing_optional_lists_and_sanitizes_control_chars() -> None:
    result = NarakaResult.model_validate(
        {
            "server_desc": "测试服",
            "season": "season",
            "player_info": {"name": "玩家\n[CQ:at,qq=all]", "rating": 1},
            "overview": None,
            "matches": None,
            "score_info": None,
        }
    )

    summary = format_naraka_summary(result)

    assert "玩家 [CQ:at,qq=all]" in summary
    assert "赛季：season" in summary
    assert "\n[CQ" not in summary
    assert "最近暂无对局数据" in summary


def test_summary_formats_all_metrics_and_five_match_performance_in_detail() -> None:
    result = NarakaResult.model_validate(
        {
            "server_desc": "国服",
            "season": "qianji",
            "player_info": {"name": "示例玩家", "rating": "4200", "level": "蚀月Ⅰ"},
            "score_info": {"composite_score": "80.0", "score_grade": "A"},
            "recent_avg_rank": "8.0",
            "overview": [
                {"desc": "总场次", "value": "100"},
                {"desc": "夺冠率", "value": "10%"},
                {"desc": "前五率", "value": "30%"},
                {"desc": "KD", "value": "2.0"},
                {"desc": "场均伤害", "value": "12000"},
                {"desc": "场均击杀", "value": "3.0"},
                {"desc": "振刀率", "value": "12.5%", "grade": "A"},
            ],
            "matches": [
                {
                    "time": 1_700_000_997,
                    "map_name": "地图一",
                    "rank": 1,
                    "kill_times": 5,
                    "damage": 15000,
                    "rating_delta": 30,
                },
                {
                    "time": 1_700_000_900,
                    "map_name": "地图二",
                    "rank": 2,
                    "kill_times": 4,
                    "damage": 14000,
                    "rating_delta": 20,
                },
                {
                    "time": 1_700_000_800,
                    "map_name": "地图三",
                    "rank": 3,
                    "kill_times": 3,
                    "damage": 13000,
                    "rating_delta": 10,
                },
                {
                    "time": 1_700_000_700,
                    "map_name": "地图四",
                    "rank": 4,
                    "kill_times": 2,
                    "damage": 12000,
                    "rating_delta": -5,
                },
                {
                    "time": 1_700_000_600,
                    "map_name": "地图五",
                    "rank": 5,
                    "kill_times": 1,
                    "damage": 11000,
                    "rating_delta": -10,
                },
                {
                    "time": 1_700_000_500,
                    "map_name": "不应展示的第六场",
                    "rank": 6,
                    "kill_times": 0,
                    "damage": 10000,
                    "rating_delta": -20,
                },
            ],
        }
    )

    summary = format_naraka_summary(result)

    assert "赛季：千机" in summary
    assert "其他数据：振刀率 12.5% (A)" in summary
    assert "最近5场汇总：平均名次 3 | 总击杀 15 | 场均伤害 13000 | 积分 +45" in summary
    assert "2023-11-15 06:29" in summary
    assert "5. 2023-11-15" in summary
    assert "不应展示的第六场" not in summary


def test_match_detail_summary_uses_allowlisted_fields_and_omits_private_fields() -> None:
    result = NarakaMatchDetail.model_validate(match_detail_payload()["result"])

    summary = format_naraka_match_detail(result)

    assert "永劫无间单局详情" in summary
    assert "玩家：测试玩家" in summary
    assert "龙隐洞天" in summary
    assert "第1名" in summary
    assert "积分 +42" in summary
    assert "总伤害 33.8k" in summary
    assert "拳刃(击杀 5，伤害 7691)" in summary
    assert "阎王" in summary
    assert "must-not-be-rendered" not in summary
    assert "attacker.invalid" not in summary


def test_match_detail_model_handles_null_optional_lists() -> None:
    result = NarakaMatchDetail.model_validate(
        {"name": "玩家\n[CQ:at,qq=all]", "weapon_list": None, "data": None, "tags": None}
    )

    summary = format_naraka_match_detail(result)

    assert "玩家 [CQ:at,qq=all]" in summary
    assert "\n[CQ" not in summary


def test_player_search_summary_uses_allowlisted_fields_and_game_ids() -> None:
    result = NarakaPlayerSearchResult.model_validate(
        {
            "query": "测试\n[CQ:at,qq=all]",
            "players": [
                {
                    "name": "玩家\n[CQ:at,qq=all]",
                    "game_id": "7pra000013743400100163",
                    "server": "163",
                    "rank": "坠日Ⅳ",
                    "total_score": "4321",
                    "match_count": "5469",
                    "private_url": "https://attacker.invalid/must-not-be-rendered",
                }
            ],
        }
    )

    summary = format_naraka_player_search(result)

    assert "永劫无间角色搜索" in summary
    assert "关键词：测试 [CQ:at,qq=all]" in summary
    assert "玩家 [CQ:at,qq=all]" in summary
    assert "游戏ID 7pra000013743400100163" in summary
    assert "坠日Ⅳ" in summary
    assert "积分 4321" in summary
    assert "场次 5469" in summary
    assert "\n[CQ" not in summary
    assert "attacker.invalid" not in summary


def test_player_search_summary_handles_empty_result() -> None:
    result = NarakaPlayerSearchResult(query="无人昵称", players=[])

    assert "没有找到匹配角色" in format_naraka_player_search(result)


@pytest.mark.asyncio
async def test_query_service_validates_role_and_applies_cooldown() -> None:
    client = type("Client", (), {"query": AsyncMock(return_value="result")})()
    now = 100.0
    service = NarakaQueryService(
        client=client,
        cooldown_seconds=10,
        max_concurrent_requests=1,
        clock=lambda: now,
    )

    assert await service.query("session", "7pra000013743400100163") == "result"
    with pytest.raises(NarakaRateLimitedError) as exc_info:
        await service.query("session", "7pra000013743400100163")
    assert exc_info.value.retry_after == 10

    with pytest.raises(ValueError):
        await service.query("other", "https://attacker.invalid/")


@pytest.mark.asyncio
async def test_query_service_validates_match_id_and_shares_cooldown() -> None:
    client = type(
        "Client",
        (),
        {
            "query": AsyncMock(return_value="summary"),
            "query_match_detail": AsyncMock(return_value="detail"),
        },
    )()
    service = NarakaQueryService(
        client=client,
        cooldown_seconds=10,
        max_concurrent_requests=1,
        clock=lambda: 100.0,
    )

    match_id = "ukg1000017605500010163.1786282570"
    assert await service.query_match_detail("session", match_id) == "detail"
    client.query_match_detail.assert_awaited_once_with(match_id=match_id)
    with pytest.raises(NarakaRateLimitedError):
        await service.query("session", "7pra000013743400100163")
    with pytest.raises(ValueError):
        await service.query_match_detail("other", "https://attacker.invalid/")
    with pytest.raises(ValueError):
        await service.query_match_detail("other", "match.123")


@pytest.mark.asyncio
async def test_query_service_validates_player_search_and_shares_cooldown() -> None:
    client = type(
        "Client",
        (),
        {
            "query": AsyncMock(return_value="summary"),
            "search_players": AsyncMock(return_value="players"),
        },
    )()
    service = NarakaQueryService(
        client=client,
        cooldown_seconds=10,
        max_concurrent_requests=1,
        clock=lambda: 100.0,
    )

    assert await service.search_players("session", "  测试 昵称  ") == "players"
    client.search_players.assert_awaited_once_with(nickname="测试 昵称")
    with pytest.raises(NarakaRateLimitedError):
        await service.query("session", "7pra000013743400100163")
    with pytest.raises(ValueError):
        await service.search_players("other", "坏\n昵称")


@pytest.mark.asyncio
async def test_query_service_resolves_nickname_to_first_role_in_one_query() -> None:
    search_result = NarakaPlayerSearchResult.model_validate(
        {
            "query": "白小痴",
            "players": [
                {
                    "name": "第一名玩家",
                    "game_id": "7pra000013743400100163",
                    "server": "163",
                },
                {
                    "name": "第二名玩家",
                    "game_id": "wxyz000000987654321163",
                    "server": "163",
                },
            ],
        }
    )
    result = NarakaResult.model_validate({"player_info": {"name": "第一名玩家"}})
    client = type(
        "Client",
        (),
        {
            "search_players": AsyncMock(return_value=search_result),
            "report_player_search": AsyncMock(),
            "query": AsyncMock(return_value=result),
        },
    )()
    service = NarakaQueryService(
        client=client,
        cooldown_seconds=10,
        max_concurrent_requests=1,
        clock=lambda: 100.0,
    )

    assert await service.query_by_nickname("session", "  白小痴  ") == result
    client.search_players.assert_awaited_once_with(nickname="白小痴")
    client.report_player_search.assert_awaited_once_with(
        game_id="7pra000013743400100163",
        nickname="第一名玩家",
        server="163",
    )
    client.query.assert_awaited_once_with(
        role_id="7pra000013743400100163",
        server="163",
    )
    with pytest.raises(NarakaRateLimitedError):
        await service.query("session", "17605500010163")


@pytest.mark.asyncio
async def test_query_service_does_not_block_detail_when_search_report_is_rejected() -> None:
    search_result = NarakaPlayerSearchResult.model_validate(
        {
            "query": "下水道",
            "players": [
                {
                    "name": "下水道de哥布林",
                    "game_id": "7pra000013743400100163",
                    "server": "163",
                }
            ],
        }
    )
    result = NarakaResult.model_validate(
        {"player_info": {"name": "下水道de哥布林"}}
    )
    client = type(
        "Client",
        (),
        {
            "search_players": AsyncMock(return_value=search_result),
            "report_player_search": AsyncMock(
                side_effect=NarakaRejectedError("report rejected")
            ),
            "query": AsyncMock(return_value=result),
        },
    )()
    service = NarakaQueryService(
        client=client,
        cooldown_seconds=0,
        max_concurrent_requests=1,
    )

    assert await service.query_by_nickname("session", "下水道") == result
    client.query.assert_awaited_once_with(
        role_id="7pra000013743400100163",
        server="163",
    )


@pytest.mark.asyncio
async def test_query_service_does_not_query_when_nickname_has_no_results() -> None:
    search_result = NarakaPlayerSearchResult(query="白小痴", players=[])
    client = type(
        "Client",
        (),
        {
            "search_players": AsyncMock(return_value=search_result),
            "report_player_search": AsyncMock(),
            "query": AsyncMock(),
        },
    )()
    service = NarakaQueryService(
        client=client,
        cooldown_seconds=0,
        max_concurrent_requests=1,
    )

    with pytest.raises(NarakaNotFoundError):
        await service.query_by_nickname("session", "白小痴")
    client.report_player_search.assert_not_awaited()
    client.query.assert_not_awaited()


@pytest.mark.asyncio
async def test_query_service_rejects_in_flight_and_global_overload_without_queueing() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingClient:
        async def query(self, *, role_id: str):
            entered.set()
            await release.wait()
            return "result"

    service = NarakaQueryService(
        client=BlockingClient(),
        cooldown_seconds=0,
        max_concurrent_requests=1,
    )
    running = asyncio.create_task(
        service.query("session-1", "7pra000013743400100163")
    )
    await entered.wait()

    with pytest.raises(NarakaBusyError):
        await service.query("session-1", "7pra000013743400100163")
    with pytest.raises(NarakaBusyError):
        await service.query("session-2", "wxyz000000987654321163")

    release.set()
    assert await running == "result"
