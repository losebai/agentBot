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


BUILTIN_PLUGINS = (BuiltinPlugin(QQBOT_METADATA, _qqbot_factory),)
BUILTIN_PLUGIN_IDS = frozenset(plugin.metadata.id for plugin in BUILTIN_PLUGINS)
