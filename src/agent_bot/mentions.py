"""安全解析群聊 @ 命令并构造 OneBot V11 消息段。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from nonebot.adapters.onebot.v11 import Message, MessageSegment

_QQ_ID_PATTERN = re.compile(r"^[1-9][0-9]{4,11}$")
_MAX_MENTION_TEXT_CHARS = 500
_MAX_GROUP_MEMBERS = 2000
_MAX_MEMBER_NAME_CHARS = 64
_MAX_NAMED_DIRECTIVE_CHARS = _MAX_MEMBER_NAME_CHARS + _MAX_MENTION_TEXT_CHARS + 16
_DEFAULT_MENTION_TEXT = "有人找你。"
_AUTO_MENTION_SCAN_CHARS = 20
_SENTENCE_END_PATTERN = re.compile(r"[。！？.!?…\r\n]")
_TEXT_DIRECTIVE_MARKER = re.compile(
    r"@|艾特|(?<![A-Za-z0-9])at(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_NUMERIC_DIRECTIVE_TARGET = re.compile(r"^([1-9][0-9]{4,11})(?=$|[\s,，:：])")
_NATIVE_MENTION_ACTION = re.compile(
    r"(?:帮我|请你|麻烦你?|替我|帮忙|艾特|(?<![A-Za-z0-9])at)\s*$",
    re.IGNORECASE,
)


class InvalidMentionCommandError(ValueError):
    """@ 命令缺少目标或包含不安全的消息段。"""


class InvalidMentionDirectiveError(ValueError):
    """自然语言 @ 指令包含不安全或无法执行的目标。"""


class MentionTargetNotFoundError(LookupError):
    """群成员中没有匹配自然语言目标。"""


class AmbiguousMentionTargetError(LookupError):
    """群成员中有多个同名目标。"""


class InvalidGroupMemberDataError(ValueError):
    """OneBot 群成员列表返回格式或数量不安全。"""


@dataclass(frozen=True, slots=True, repr=False)
class MentionCommand:
    target_user_id: int
    text: str


@dataclass(frozen=True, slots=True, repr=False)
class NamedMentionRequest:
    """需要使用当前群成员列表解析的昵称 @ 请求。"""

    target_and_text: str


def _parse_target(value: object) -> int:
    rendered = str(value).strip()
    if not _QQ_ID_PATTERN.fullmatch(rendered):
        raise InvalidMentionCommandError("invalid QQ target")
    return int(rendered)


def _normalize_text(value: str) -> str:
    if any(
        unicodedata.category(character).startswith("C") and not character.isspace()
        for character in value
    ):
        raise InvalidMentionCommandError("mention text contains control characters")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > _MAX_MENTION_TEXT_CHARS:
        raise InvalidMentionCommandError("mention text is empty or too long")
    return normalized


def _normalize_directive_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = re.sub(r"^一下\s*[,，:：]?\s*", "", normalized)
    normalized = normalized.lstrip(" ,，:：")
    if not normalized:
        return _DEFAULT_MENTION_TEXT
    try:
        return _normalize_text(normalized)
    except InvalidMentionCommandError as exc:
        raise InvalidMentionDirectiveError("invalid mention directive text") from exc


def _text_before(segments: list[MessageSegment], index: int) -> str:
    return "".join(
        str(segment.data.get("text", ""))
        for segment in segments[:index]
        if segment.type == "text"
    )


def _text_after(segments: list[MessageSegment], index: int) -> str:
    return "".join(
        str(segment.data.get("text", ""))
        for segment in segments[index + 1 :]
        if segment.type == "text"
    )


def _is_in_auto_mention_window(text_before_marker: str) -> bool:
    normalized = unicodedata.normalize("NFKC", text_before_marker)
    return (
        len(normalized) < _AUTO_MENTION_SCAN_CHARS
        and _SENTENCE_END_PATTERN.search(normalized) is None
    )


def _is_native_mention_directive(text_before_marker: str) -> bool:
    """仅把目标前带明确动作词的原生 @ 识别为代发指令。"""

    normalized = unicodedata.normalize("NFKC", text_before_marker)
    return _is_in_auto_mention_window(normalized) and bool(
        _NATIVE_MENTION_ACTION.search(normalized)
    )


def _target_after_text_marker(value: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", value)
    sentence_end = _SENTENCE_END_PATTERN.search(normalized)
    window_end = min(
        len(normalized),
        _AUTO_MENTION_SCAN_CHARS,
        sentence_end.start() if sentence_end is not None else len(normalized),
    )
    marker = _TEXT_DIRECTIVE_MARKER.search(normalized)
    if marker is None or marker.start() >= window_end:
        return None
    target_and_text = re.sub(r"^\s*一下\s*", "", normalized[marker.end() :], count=1)
    return target_and_text.lstrip(" ,，:：")


def parse_conversational_mention(
    message: Message,
    *,
    bot_user_id: int | str,
) -> MentionCommand | NamedMentionRequest | None:
    """识别明确的群聊 @ 动作；普通讨论中的 @ 不会触发。"""

    segments = list(message)
    if any(segment.type not in {"text", "at"} for segment in segments):
        return None

    bot_id = str(bot_user_id)
    target_indexes = [
        index
        for index, segment in enumerate(segments)
        if segment.type == "at" and str(segment.data.get("qq", "")).strip() != bot_id
    ]
    if target_indexes:
        first_target_index = target_indexes[0]
        if not _is_native_mention_directive(
            _text_before(segments, first_target_index)
        ):
            return None
        if len(target_indexes) != 1:
            raise InvalidMentionDirectiveError("mention directive accepts one target")
        try:
            target_user_id = _parse_target(
                segments[first_target_index].data.get("qq", "")
            )
        except InvalidMentionCommandError as exc:
            raise InvalidMentionDirectiveError("invalid native mention target") from exc
        return MentionCommand(
            target_user_id=target_user_id,
            text=_normalize_directive_text(_text_after(segments, first_target_index)),
        )

    plain_text = "".join(
        str(segment.data.get("text", ""))
        for segment in segments
        if segment.type == "text"
    )
    target_after_marker = _target_after_text_marker(plain_text)
    if target_after_marker is None:
        return None
    target_and_text = " ".join(target_after_marker.split())
    if not target_and_text or len(target_and_text) > _MAX_NAMED_DIRECTIVE_CHARS:
        raise InvalidMentionDirectiveError("named mention directive is empty or too long")
    if any(
        unicodedata.category(character).startswith("C") and not character.isspace()
        for character in target_and_text
    ):
        raise InvalidMentionDirectiveError("named mention directive contains control characters")

    numeric_target = _NUMERIC_DIRECTIVE_TARGET.match(target_and_text)
    if numeric_target is not None:
        target_user_id = int(numeric_target.group(1))
        if str(target_user_id) == bot_id:
            raise InvalidMentionDirectiveError("cannot mention the bot itself")
        return MentionCommand(
            target_user_id=target_user_id,
            text=_normalize_directive_text(target_and_text[numeric_target.end() :]),
        )
    return NamedMentionRequest(target_and_text=target_and_text)


def render_semantic_mentions(message: Message, members: object) -> Message:
    """把普通聊天中的原生 @ 段替换为群成员名，保留其余消息段。"""

    if not isinstance(members, Sequence) or isinstance(members, (str, bytes, bytearray)):
        raise InvalidGroupMemberDataError("group member list is invalid")
    if len(members) > _MAX_GROUP_MEMBERS:
        raise InvalidGroupMemberDataError("group member list exceeds limit")

    member_names: dict[str, str] = {}
    for member in members:
        if not isinstance(member, Mapping):
            continue
        try:
            user_id = str(_parse_target(member.get("user_id", "")))
        except InvalidMentionCommandError:
            continue
        name = _normalize_member_name(member.get("card")) or _normalize_member_name(
            member.get("nickname")
        )
        if name is not None:
            member_names[user_id] = name

    rendered = Message()
    for segment in message:
        if segment.type != "at":
            rendered.append(segment)
            continue
        target = str(segment.data.get("qq", "")).strip()
        name = member_names.get(target)
        if name is None:
            raise MentionTargetNotFoundError("mentioned group member was not found")
        rendered.append(MessageSegment.text(name))
    return rendered


def resolve_named_mention(
    request: NamedMentionRequest,
    members: object,
    *,
    bot_user_id: int | str,
) -> MentionCommand:
    """按群名片/昵称最长前缀唯一匹配目标，不进行猜测。"""

    if not isinstance(members, Sequence) or isinstance(members, (str, bytes, bytearray)):
        raise InvalidGroupMemberDataError("group member list is invalid")
    if len(members) > _MAX_GROUP_MEMBERS:
        raise InvalidGroupMemberDataError("group member list exceeds limit")

    target_and_text = request.target_and_text
    folded_target = target_and_text.casefold()
    matches: dict[int, int] = {}
    for raw_member in members:
        if not isinstance(raw_member, Mapping):
            continue
        try:
            user_id = _parse_target(raw_member.get("user_id", ""))
        except InvalidMentionCommandError:
            continue
        if str(user_id) == str(bot_user_id):
            continue
        for field in ("card", "nickname"):
            alias = _normalize_member_name(raw_member.get(field))
            if alias is not None and folded_target.startswith(alias.casefold()):
                matches[user_id] = max(matches.get(user_id, 0), len(alias))

    if not matches:
        raise MentionTargetNotFoundError("group member was not found")
    longest = max(matches.values())
    target_ids = [user_id for user_id, length in matches.items() if length == longest]
    if len(target_ids) != 1:
        raise AmbiguousMentionTargetError("group member name is ambiguous")
    return MentionCommand(
        target_user_id=target_ids[0],
        text=_normalize_directive_text(target_and_text[longest:]),
    )


def resolve_tool_mention(
    target: str,
    text: str,
    members: object,
    *,
    bot_user_id: int | str,
) -> MentionCommand:
    """Resolve an MCP mention target strictly against the current group membership."""

    normalized_target = unicodedata.normalize("NFKC", target).strip()
    if (
        not normalized_target
        or len(normalized_target) > _MAX_MEMBER_NAME_CHARS
        or any(unicodedata.category(character).startswith("C") for character in normalized_target)
    ):
        raise InvalidMentionDirectiveError("invalid tool mention target")
    try:
        normalized_text = _normalize_text(text)
    except InvalidMentionCommandError as exc:
        raise InvalidMentionDirectiveError("invalid tool mention text") from exc

    if _QQ_ID_PATTERN.fullmatch(normalized_target):
        target_user_id = int(normalized_target)
        if target_user_id == int(bot_user_id):
            raise InvalidMentionDirectiveError("cannot mention bot")
        if not isinstance(members, Sequence) or isinstance(members, (str, bytes, bytearray)):
            raise InvalidGroupMemberDataError("group member list is invalid")
        if len(members) > _MAX_GROUP_MEMBERS:
            raise InvalidGroupMemberDataError("group member list exceeds limit")
        is_member = any(
            isinstance(member, Mapping)
            and str(member.get("user_id", "")).strip() == normalized_target
            for member in members
        )
        if not is_member:
            raise MentionTargetNotFoundError("group member was not found")
        return MentionCommand(target_user_id=target_user_id, text=normalized_text)

    if not isinstance(members, Sequence) or isinstance(members, (str, bytes, bytearray)):
        raise InvalidGroupMemberDataError("group member list is invalid")
    if len(members) > _MAX_GROUP_MEMBERS:
        raise InvalidGroupMemberDataError("group member list exceeds limit")
    folded_target = normalized_target.casefold()
    target_ids: set[int] = set()
    for raw_member in members:
        if not isinstance(raw_member, Mapping):
            continue
        try:
            user_id = _parse_target(raw_member.get("user_id", ""))
        except InvalidMentionCommandError:
            continue
        if user_id == int(bot_user_id):
            continue
        for field in ("card", "nickname"):
            alias = _normalize_member_name(raw_member.get(field))
            if alias is not None and alias.casefold() == folded_target:
                target_ids.add(user_id)
    if not target_ids:
        raise MentionTargetNotFoundError("group member was not found")
    if len(target_ids) != 1:
        raise AmbiguousMentionTargetError("group member name is ambiguous")
    return MentionCommand(target_user_id=target_ids.pop(), text=normalized_text)


def _normalize_member_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if (
        not normalized
        or len(normalized) > _MAX_MEMBER_NAME_CHARS
        or any(unicodedata.category(character).startswith("C") for character in normalized)
    ):
        return None
    return normalized


def parse_mention_command(argument: Message) -> MentionCommand:
    """接受数字 QQ 或单个原生 at 段，拒绝 all、多目标和非文本内容。"""

    segments = list(argument)
    if any(segment.type not in {"text", "at"} for segment in segments):
        raise InvalidMentionCommandError("mention command only accepts text")

    at_indexes = [index for index, segment in enumerate(segments) if segment.type == "at"]
    if len(at_indexes) > 1:
        raise InvalidMentionCommandError("mention command accepts one target")

    if at_indexes:
        at_index = at_indexes[0]
        before = "".join(
            str(segment.data.get("text", ""))
            for segment in segments[:at_index]
            if segment.type == "text"
        )
        if before.strip():
            raise InvalidMentionCommandError("native at target must appear first")
        target_user_id = _parse_target(segments[at_index].data.get("qq", ""))
        raw_text = "".join(
            str(segment.data.get("text", ""))
            for segment in segments[at_index + 1 :]
            if segment.type == "text"
        )
    else:
        plain_text = "".join(
            str(segment.data.get("text", ""))
            for segment in segments
            if segment.type == "text"
        ).strip()
        parts = plain_text.split(maxsplit=1)
        if len(parts) != 2:
            raise InvalidMentionCommandError("mention command requires target and text")
        target_user_id = _parse_target(parts[0])
        raw_text = parts[1]

    return MentionCommand(
        target_user_id=target_user_id,
        text=_normalize_text(raw_text),
    )


def build_mention_message(command: MentionCommand) -> Message:
    """构造 NapCat `/send_msg` 可识别的 at + text 消息段数组。"""

    return Message(
        [
            MessageSegment.at(command.target_user_id),
            MessageSegment.text(f" {command.text}"),
        ]
    )
