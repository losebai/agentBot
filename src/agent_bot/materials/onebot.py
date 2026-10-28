"""仅为授权 QQ 文件消息和通知登记素材，图片交由实际触发的对话接收。"""

from nonebot import logger, on_message, on_notice
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    GroupUploadNoticeEvent,
    MessageEvent,
    MessageSegment,
    NoticeEvent,
    PrivateMessageEvent,
)
from nonebot.adapters.onebot.v11.exception import ActionFailed, ApiNotAvailable, NetworkError
from nonebot.rule import Rule, is_type

from agent_bot.ai.errors import AiError
from agent_bot.media.contracts import MediaOwner
from agent_bot.state import build_session_scope, external_session_id


def install_material_receivers(
    *, receiver_provider, config_provider, policy_provider, secret_provider,
    is_authorized, session_id_provider, runtime_lease, wait_ready,
):
    """消息复用现有授权与会话编码，文件通知按同一访问策略验证真实发送者。"""
    priority = config_provider().chat_priority - 1
    messages = on_message(
        rule=is_type(PrivateMessageEvent, GroupMessageEvent) & Rule(is_authorized),
        priority=priority, block=False,
    )
    async def file_notice(event: NoticeEvent) -> bool:
        """私聊离线文件是 NapCat 扩展，适配器保留为通用 notice 事件。"""
        return event.notice_type in {"group_upload", "offline_file"}

    notices = on_notice(rule=Rule(file_notice), priority=priority, block=False)

    @messages.handle()
    async def receive_message(bot: Bot, event: MessageEvent):
        """普通文件保留独立接收能力，未触发对话的图片不能自动进入素材库。"""
        segments = [item for item in event.get_message() if item.type == "file"]
        if not segments:
            return
        async with runtime_lease():
            try:
                if len(segments) > 16:
                    raise ValueError("一次最多接收十六个素材")
                owner = MediaOwner(entry="qq", session_id=session_id_provider(bot, event))
                for segment in segments:
                    await receiver_provider().register(
                        bot, owner, segment,
                        group_id=event.group_id if isinstance(event, GroupMessageEvent) else None,
                    )
            except (
                AiError, OSError, ValueError, ActionFailed, ApiNotAvailable, NetworkError,
            ) as error:
                logger.warning("收到附件但素材保存失败：{}",
                               getattr(error, "code", type(error).__name__))

    @notices.handle()
    async def receive_file(bot: Bot, event: NoticeEvent):
        """OneBot 普通文件可能只上报 notice；不把未授权用户文件写入素材库。"""
        await wait_ready()
        payload = event.model_dump()
        group_id = payload.get("group_id") if isinstance(event, GroupUploadNoticeEvent) else None
        user_id = payload.get("user_id")
        file = payload.get("file")
        if not user_id or not isinstance(file, dict):
            logger.warning("收到无效文件通知，未保存素材")
            return
        if not policy_provider().allows(user_id=str(user_id), group_id=group_id):
            return
        config = config_provider()
        scope = build_session_scope(
            bot_id=bot.self_id, user_id=str(user_id), group_id=group_id,
            group_shared=config.chat_group_shared_session,
        )
        owner = MediaOwner(entry="qq", session_id=external_session_id(scope, secret_provider()))
        data = {"file_id": file.get("id"), "name": file.get("name"), "size": file.get("size")}
        if isinstance(event, GroupUploadNoticeEvent):
            data["busid"] = file.get("busid")
        else:
            data["url"] = file.get("url")
        async with runtime_lease():
            try:
                await receiver_provider().register(
                    bot, owner, MessageSegment("file", data), group_id=group_id,
                )
            except (
                AiError, OSError, ValueError, ActionFailed, ApiNotAvailable, NetworkError,
            ) as error:
                logger.warning("收到文件通知但素材保存失败：{}",
                               getattr(error, "code", type(error).__name__))

    return messages, notices
