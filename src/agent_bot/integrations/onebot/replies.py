"""统一校验并投递由 LLM 生成的 OneBot 用户回复。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, NoReturn

from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from nonebot.matcher import Matcher

from agent_bot.config import RuntimeConfig
from agent_bot.model_guard import MODEL_CONFIGURATION_ERROR_MESSAGE
from agent_bot.services.replies import GeneratedReply, LlmReplyComposer, ReplyIntent
from agent_bot.state import split_reply


@dataclass(frozen=True, slots=True)
class ReplyDeliveryReceipt:
    """同一次完整投递的实际可见文字和最后一段 OneBot 响应。"""

    text: str
    response: dict[str, Any] | None


class OneBotReplyGateway:
    """统一消息投递；普通正文须来自模型，配置错误仅允许固定的受限通知。"""

    def __init__(self, config_provider: Callable[[], RuntimeConfig]) -> None:
        """保存运行时配置提供器，以便普通回复使用最新分段限制。"""

        self._config_provider = config_provider

    async def finish_configuration_error(
        self, matcher: Matcher, *, kind: Literal["model", "catalog", "agent"] = "model",
    ) -> NoReturn:
        """模型无法用于解释配置错误时，直接发送固定提示且不伪造生成回复来源。"""
        messages = {
            "model": MODEL_CONFIGURATION_ERROR_MESSAGE,
            "catalog": "模型目录暂时不可用，请检查数据库或稍后重试。",
            "agent": "AI 接入配置不可用，请检查 Agent、工具与模型配置。",
        }
        await matcher.send(Message(messages[kind]))
        await matcher.finish()
        raise AssertionError("matcher.finish must stop execution")

    async def send_configuration_error(
        self, bot: Bot, *, scene: Literal["private", "group"], target_id: str,
    ) -> None:
        """已有任务遇到模型失效时，只向任务原目标发送固定配置错误。"""
        await self._send_bot_message(
            bot, scene=scene, target_id=target_id,
            message=Message(MODEL_CONFIGURATION_ERROR_MESSAGE),
        )

    async def send_matcher(
        self,
        matcher: Matcher,
        reply: GeneratedReply,
        *,
        prefix: MessageSegment | None = None,
    ) -> None:
        """按当前长度配置向 Matcher 依次发送生成回复。"""

        self._require_generated(reply)
        self._require_at_prefix(prefix)
        for index, part in enumerate(self._parts(reply)):
            message = self._text_message(part, prefix if index == 0 else None)
            await matcher.send(message)

    async def finish_matcher(
        self,
        matcher: Matcher,
        reply: GeneratedReply | None,
        *,
        prefix: MessageSegment | None = None,
    ) -> NoReturn:
        """可选发送生成回复，然后无附加文字地结束 Matcher。"""

        if reply is not None:
            await self.send_matcher(matcher, reply, prefix=prefix)
        await matcher.finish()
        raise AssertionError("matcher.finish must stop execution")

    async def send_bot(
        self,
        bot: Bot,
        *,
        scene: Literal["private", "group"],
        target_id: str,
        reply: GeneratedReply,
        prefix: MessageSegment | None = None,
    ) -> ReplyDeliveryReceipt:
        """按单次配置快照投递全部分段并返回实际可见文字和最后一段响应。"""

        self._require_generated(reply)
        self._require_at_prefix(prefix)
        response: dict[str, Any] | None = None
        delivered: list[str] = []
        for index, part in enumerate(self._parts(reply)):
            response = await self._send_bot_message(
                bot,
                scene=scene,
                target_id=target_id,
                message=self._text_message(part, prefix if index == 0 else None),
            )
            delivered.append(f" {part}" if index == 0 and prefix is not None else part)
        return ReplyDeliveryReceipt(text="".join(delivered), response=response)

    async def send_mention(
        self,
        bot: Bot,
        *,
        target_id: str,
        member_id: int,
        reply: GeneratedReply,
    ) -> dict[str, Any] | None:
        """在传输边界构造真实 at，并沿统一发送路径返回最后一段响应。"""

        receipt = await self.send_bot(
            bot,
            scene="group",
            target_id=target_id,
            reply=reply,
            prefix=MessageSegment.at(member_id),
        )
        return receipt.response

    async def send_bot_exact_part(
        self,
        bot: Bot,
        *,
        scene: Literal["private", "group"],
        target_id: str,
        reply: GeneratedReply,
        prefix: MessageSegment | None = None,
    ) -> dict[str, Any]:
        """发送一个不再切分的生成回复分段并返回 OneBot 原始响应。"""

        self._require_generated(reply)
        self._require_at_prefix(prefix)
        return await self._send_bot_message(
            bot, scene=scene, target_id=target_id,
            message=self._text_message(reply.text, prefix),
        )

    async def send_tool_reply(
        self, bot: Bot, *, scene: Literal["private", "group"], target_id: str,
        reply: GeneratedReply, prefix: MessageSegment | None = None,
        image: MessageSegment | None = None,
    ) -> dict[str, Any]:
        """把工具增强对话的最终正文、原生 At 和可选图片合并为一次平台发送。"""
        self._require_generated(reply)
        self._require_at_prefix(prefix)
        message = self._text_message(reply.text, prefix)
        if image is not None:
            if image.type != "image":
                raise ValueError("tool reply attachment must be an image")
            message.append(image)
        return await self._send_bot_message(
            bot, scene=scene, target_id=target_id, message=message,
        )

    async def send_media_matcher(
        self,
        matcher: Matcher,
        segment: MessageSegment,
        *,
        prefix: MessageSegment | None = None,
    ) -> None:
        """向 Matcher 发送一个结构化媒体段和可选的真实 At 前缀。"""

        await matcher.send(self._media_message(segment, prefix))

    async def send_media_bot(
        self,
        bot: Bot,
        *,
        scene: Literal["private", "group"],
        target_id: str,
        segment: MessageSegment,
        prefix: MessageSegment | None = None,
    ) -> dict[str, Any]:
        """按已校验的会话目标投递结构化媒体，禁止附带任意文字段。"""

        return await self._send_bot_message(
            bot, scene=scene, target_id=target_id,
            message=self._media_message(segment, prefix),
        )

    async def _send_bot_message(
        self,
        bot: Bot,
        *,
        scene: Literal["private", "group"],
        target_id: str,
        message: Message,
    ) -> dict[str, Any]:
        """仅在网关内部发送已经通过文字来源或媒体类型校验的消息。"""

        if scene == "group":
            return await bot.send_group_msg(group_id=int(target_id), message=message)
        if scene == "private":
            return await bot.send_private_msg(user_id=int(target_id), message=message)
        raise ValueError("unsupported OneBot scene")

    def _media_message(
        self,
        segment: MessageSegment,
        prefix: MessageSegment | None,
    ) -> Message:
        """校验媒体类型与真实 At，保留 At 在媒体之前的消息段顺序。"""

        if not isinstance(segment, MessageSegment) or segment.type not in {
            "image", "record", "video",
        }:
            raise ValueError("unsupported media segment")
        self._require_at_prefix(prefix)
        return Message([prefix, segment]) if prefix is not None else Message(segment)

    def _parts(self, reply: GeneratedReply) -> list[str]:
        """先裁剪模型原文再分段，避免注入不属于模型回复的固定标记。"""

        config = self._config_provider()
        return split_reply(
            reply.text[:config.chat_max_reply_chars],
            chunk_chars=config.chat_chunk_chars,
            max_reply_chars=config.chat_max_reply_chars,
        )

    def _require_generated(self, reply: GeneratedReply) -> None:
        """拒绝没有 GeneratedReply 来源证明的文字。"""

        if not isinstance(reply, GeneratedReply):
            raise TypeError("reply must be GeneratedReply")

    def _require_at_prefix(self, prefix: MessageSegment | None) -> None:
        """仅允许真实 OneBot at 段作为生成文字前缀。"""

        if prefix is not None and (
            not isinstance(prefix, MessageSegment) or prefix.type != "at"
        ):
            raise TypeError("prefix must be MessageSegment.at")

    def _text_message(
        self,
        part: str,
        prefix: MessageSegment | None,
    ) -> Message:
        """构造一个纯生成文字消息或带 at 前缀的有序消息。"""

        if prefix is None:
            return Message(MessageSegment.text(part))
        return Message([prefix, MessageSegment.text(f" {part}")])


class OneBotReplyService:
    """编排回复意图生成和 OneBot Matcher 投递。"""

    def __init__(
        self,
        composer: LlmReplyComposer,
        gateway: OneBotReplyGateway,
    ) -> None:
        """保存回复生成器和统一发送网关。"""

        self._composer = composer
        self.gateway = gateway

    async def compose(self, intent: ReplyIntent) -> GeneratedReply | None:
        """把已确定的回复意图交给 LLM 生成器。"""

        return await self._composer.compose(intent)

    async def send_matcher(
        self,
        matcher: Matcher,
        intent: ReplyIntent,
        *,
        prefix: MessageSegment | None = None,
    ) -> bool:
        """生成并发送回复；生成失败时保持静默并返回假。"""

        reply = await self.compose(intent)
        if reply is None:
            return False
        await self.gateway.send_matcher(matcher, reply, prefix=prefix)
        return True

    async def finish_matcher(
        self,
        matcher: Matcher,
        intent: ReplyIntent,
        *,
        prefix: MessageSegment | None = None,
    ) -> NoReturn:
        """生成可选回复后结束 Matcher，不构造任何兜底文字。"""

        reply = await self.compose(intent)
        await self.gateway.finish_matcher(matcher, reply, prefix=prefix)
