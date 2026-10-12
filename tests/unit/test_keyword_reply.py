import pytest

from agent_bot.keyword_reply import find_keyword_reply, normalize_keyword


def test_find_keyword_reply_matches_nfkc_casefolded_substring() -> None:
    rules = {"\uff28\uff45\uff4c\uff4c\uff4f": "你好！"}

    assert find_keyword_reply("有人说 hello world", rules) == "你好！"


def test_find_keyword_reply_prefers_longest_matching_keyword() -> None:
    rules = {
        "永劫": "短规则",
        "永劫无间": "长规则",
    }

    assert find_keyword_reply("今晚玩永劫无间吗", rules) == "长规则"


@pytest.mark.parametrize("text", ["", "完全不匹配", "x" * 20_001 + "关键词"])
def test_find_keyword_reply_returns_none_for_empty_unmatched_or_out_of_scan_range(
    text: str,
) -> None:
    assert find_keyword_reply(text, {"关键词": "回复"}) is None


def test_normalize_keyword_makes_compatibility_forms_equivalent() -> None:
    assert normalize_keyword(" \uff21\uff22\uff23 ") == normalize_keyword("abc")
