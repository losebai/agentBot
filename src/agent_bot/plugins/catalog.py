"""显式登记内置插件，配置校验不会执行工厂或加载消息入口。"""

from agent_bot import __version__
from agent_bot.plugins.contracts import ApplicationPlugin, BuiltinPlugin, PluginMetadata

QQBOT_METADATA = PluginMetadata(
    "qqbot",
    "QQBot",
    __version__,
    "通过 NapCatQQ / OneBot V11 提供 QQ 消息与工具能力",
)


def _qqbot_factory() -> ApplicationPlugin:
    """只有启用 QQBot 时才导入它的生命周期实现。"""
    from agent_bot.plugins.qqbot.plugin import QQBotPlugin

    return QQBotPlugin()


BILIBILI_METADATA = PluginMetadata(
    'bilibili', 'B站', __version__, '扫码登录、视频投稿、按需回复评论和登录检测',
)


def _bilibili_factory() -> ApplicationPlugin:
    """只有明确启用 B 站时才导入专属生命周期。"""
    from agent_bot.plugins.bilibili.plugin import BilibiliPlugin

    return BilibiliPlugin()


COMFY_METADATA = PluginMetadata(
    "comfy", "ComfyUI", __version__, "本地 ComfyUI 连接、工作流管理与图片视频生成",
)


def _comfy_factory() -> ApplicationPlugin:
    """只有启用 Comfy 时才导入它的客户端与生命周期。"""
    from agent_bot.plugins.comfy.plugin import ComfyPlugin

    return ComfyPlugin()


STICKERS_METADATA = PluginMetadata(
    "stickers", "表情包", __version__, "上传表情包、自定义标签、独立视觉识别和聊天语境检索发送",
)


def _stickers_factory() -> ApplicationPlugin:
    """只有显式启用表情插件才加载独立图库及视觉服务。"""
    from agent_bot.plugins.stickers.plugin import StickersPlugin

    return StickersPlugin()


BUILTIN_PLUGINS = (
    # Comfy 先启动，QQ 才能借用完整服务；退出顺序相反。
    BuiltinPlugin(COMFY_METADATA, _comfy_factory),
    BuiltinPlugin(QQBOT_METADATA, _qqbot_factory),
    BuiltinPlugin(BILIBILI_METADATA, _bilibili_factory),
    BuiltinPlugin(STICKERS_METADATA, _stickers_factory),
)
BUILTIN_PLUGIN_IDS = frozenset(plugin.metadata.id for plugin in BUILTIN_PLUGINS)
