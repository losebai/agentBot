"""永劫无间战绩的确定性事实序列化，不直接作为用户回复发送。"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from agent_bot.services.naraka import (
    NarakaMatch,
    NarakaMatchDetail,
    NarakaMetric,
    NarakaPlayerSearchResult,
    NarakaResult,
    Scalar,
    _safe_text,
    is_naraka_match_id,
)

_SEASON_DISPLAY_NAMES = {"qianji": "千机"}
_DISPLAY_TIMEZONE = timezone(timedelta(hours=8))
_MAX_DISPLAY_NICKNAME_CHARS = 32


def _display(value: Scalar | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return f"{value:g}"
    text = _safe_text(value)
    return text or None


def _positive_delta(value: Scalar | None) -> str | None:
    rendered = _display(value)
    if rendered is None:
        return None
    try:
        numeric = float(rendered)
    except ValueError:
        return rendered
    return f"+{rendered}" if numeric > 0 else rendered


def _display_season(value: str | None) -> str | None:
    rendered = _display(value)
    if rendered is None:
        return None
    return _SEASON_DISPLAY_NAMES.get(rendered.casefold(), rendered)


def _finite_number(value: Scalar | None) -> float | None:
    rendered = _display(value)
    if rendered is None:
        return None
    try:
        numeric = float(rendered)
    except ValueError:
        return None
    return numeric if math.isfinite(numeric) else None


def _compact_number(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _display_match_time(value: Scalar | None) -> str | None:
    rendered = _display(value)
    if rendered is None:
        return None
    numeric = _finite_number(value)
    if numeric is None or not numeric.is_integer() or not 946_684_800 <= numeric <= 4_102_444_800:
        return rendered
    try:
        return datetime.fromtimestamp(int(numeric), tz=_DISPLAY_TIMEZONE).strftime(
            "%Y-%m-%d %H:%M"
        )
    except (OSError, OverflowError, ValueError):
        return rendered


def _format_metric(metric: NarakaMetric) -> tuple[str, str] | None:
    label = _safe_text(metric.desc)
    value = _display(metric.value)
    if not label or value is None:
        return None
    grade = _display(metric.grade)
    suffix = f" ({grade})" if grade else ""
    return label.casefold(), f"{label} {value}{suffix}"


def _format_recent_match_summary(matches: list[NarakaMatch]) -> str | None:
    if not matches:
        return None
    parts: list[str] = []
    ranks = [value for match in matches if (value := _finite_number(match.rank)) is not None]
    if ranks:
        parts.append(f"平均名次 {_compact_number(sum(ranks) / len(ranks))}")
    kills = [
        value for match in matches if (value := _finite_number(match.kill_times)) is not None
    ]
    if kills:
        parts.append(f"总击杀 {_compact_number(sum(kills))}")
    damages = [
        value for match in matches if (value := _finite_number(match.damage)) is not None
    ]
    if damages:
        parts.append(f"场均伤害 {_compact_number(sum(damages) / len(damages))}")
    deltas = [
        value
        for match in matches
        if (value := _finite_number(match.rating_delta)) is not None
    ]
    if deltas:
        delta = _positive_delta(_compact_number(sum(deltas)))
        if delta:
            parts.append(f"积分 {delta}")
    return " | ".join(parts) or None


def format_naraka_summary(result: NarakaResult) -> str:
    """把上游白名单字段格式化为适合 QQ 的纯文本摘要。"""

    player_name = _safe_text(result.player_info.name) or "未知玩家"
    lines = ["永劫无间战绩", f"玩家：{player_name}"]

    context: list[str] = []
    if server := _display(result.server_desc):
        context.append(f"服务器：{server}")
    if season := _display_season(result.season):
        context.append(f"赛季：{season}")
    if context:
        lines.append(" | ".join(context))

    progression: list[str] = []
    if rating := _display(result.player_info.rating):
        progression.append(f"段位积分：{rating}")
    level_value = result.player_info.level or result.player_info.lv
    if level := _display(level_value):
        progression.append(f"等级：{level}")
    if progression:
        lines.append(" | ".join(progression))

    scores: list[str] = []
    if composite := _display(result.score_info.composite_score):
        grade = _display(result.score_info.score_grade)
        scores.append(f"综合评分：{composite}{f' ({grade})' if grade else ''}")
    if average_rank := _display(result.recent_avg_rank):
        scores.append(f"近期平均名次：{average_rank}")
    if scores:
        lines.append(" | ".join(scores))

    primary_labels = ("总场次", "夺冠率", "前五率", "KD", "场均伤害", "场均击杀")
    formatted_metrics = [
        formatted for metric in result.overview if (formatted := _format_metric(metric))
    ]
    metrics_by_name = dict(formatted_metrics)
    metrics = [
        metrics_by_name[label.casefold()]
        for label in primary_labels
        if label.casefold() in metrics_by_name
    ]
    if metrics:
        lines.append("数据：" + " | ".join(metrics))

    primary_names = {label.casefold() for label in primary_labels}
    extra_metrics = [text for name, text in formatted_metrics if name not in primary_names][:8]
    if extra_metrics:
        lines.append("其他数据：" + " | ".join(extra_metrics))

    if not result.matches:
        lines.append("最近暂无对局数据")
        return "\n".join(lines)

    recent_matches = result.matches[:5]
    if summary := _format_recent_match_summary(recent_matches):
        lines.append(f"最近{len(recent_matches)}场汇总：{summary}")
    lines.append(f"最近对局({len(recent_matches)}场)：")
    for index, match in enumerate(recent_matches, start=1):
        details: list[str] = []
        if match_time := _display_match_time(match.time):
            details.append(match_time)
        if map_name := _display(match.map_name):
            details.append(map_name)
        if rank := _display(match.rank):
            details.append(f"第{rank}名")
        if kills := _display(match.kill_times):
            details.append(f"击杀 {kills}")
        if damage := _display(match.damage):
            details.append(f"伤害 {damage}")
        if delta := _positive_delta(match.rating_delta):
            details.append(f"积分 {delta}")
        if match.match_id and is_naraka_match_id(match.match_id):
            details.append(f"比赛ID {match.match_id}")
        lines.append(f"{index}. " + " | ".join(details or ["暂无详情"]))
    return "\n".join(lines)


def format_naraka_match_detail(result: NarakaMatchDetail) -> str:
    """把单局详情中的白名单字段格式化为适合 QQ 的纯文本摘要。"""

    player_name = _safe_text(result.name) or "未知玩家"
    lines = ["永劫无间单局详情", f"玩家：{player_name}"]

    context: list[str] = []
    if map_name := _display(result.map_name):
        context.append(f"地图：{map_name}")
    if rank := _display(result.rank):
        context.append(f"第{rank}名")
    if play_num := _display(result.play_num):
        context.append(f"队伍人数：{play_num}")
    if context:
        lines.append(" | ".join(context))

    progression: list[str] = []
    if level := _display(result.level):
        progression.append(f"段位：{level}")
    if rating := _display(result.rating):
        progression.append(f"积分：{rating}")
    if grade := _display(result.grade):
        progression.append(f"评分：{grade}")
    if delta := _positive_delta(result.rating_delta):
        progression.append(f"积分 {delta}")
    if progression:
        lines.append(" | ".join(progression))

    metric_values = {
        _safe_text(metric.desc).casefold(): _display(metric.value)
        for metric in result.data
        if metric.desc
    }
    metrics: list[str] = []
    for label in ("总伤害", "总恢复", "击杀", "振刀", "存活时长", "救援"):
        value = metric_values.get(label.casefold())
        if value:
            metrics.append(f"{label} {value}")
    if metrics:
        lines.append("数据：" + " | ".join(metrics))

    weapons: list[str] = []
    for weapon in result.weapon_list[:3]:
        name = _safe_text(weapon.name) or "未知武器"
        weapon_details: list[str] = []
        if kills := _display(weapon.kill_times):
            weapon_details.append(f"击杀 {kills}")
        if damage := _display(weapon.damage):
            weapon_details.append(f"伤害 {damage}")
        suffix = f"({'，'.join(weapon_details)})" if weapon_details else ""
        weapons.append(f"{name}{suffix}")
    if weapons:
        lines.append("武器：" + "；".join(weapons))

    tags = [_safe_text(tag.name) for tag in result.tags[:5] if _safe_text(tag.name)]
    if tags:
        lines.append("成就：" + "、".join(tags))
    return "\n".join(lines)


def format_naraka_player_search(result: NarakaPlayerSearchResult) -> str:
    """把昵称搜索结果中的白名单字段格式化为适合 QQ 的纯文本列表。"""

    query = _safe_text(result.query, limit=_MAX_DISPLAY_NICKNAME_CHARS) or "未知昵称"
    lines = ["永劫无间角色搜索", f"关键词：{query}"]
    if not result.players:
        lines.append("没有找到匹配角色")
        return "\n".join(lines)

    lines.append(f"找到 {len(result.players)} 个角色：")
    for index, player in enumerate(result.players[:20], start=1):
        name = _safe_text(player.name) or "未知玩家"
        details = [f"游戏ID {player.game_id}"]
        if server := _display(player.server):
            details.append(f"服务器 {server}")
        if rank := _display(player.rank):
            details.append(rank)
        if score := _display(player.total_score):
            details.append(f"积分 {score}")
        if matches := _display(player.match_count):
            details.append(f"场次 {matches}")
        lines.append(f"{index}. {name} | " + " | ".join(details))
    return "\n".join(lines)
