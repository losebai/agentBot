"""把宿主上下文显式传入 NoneBot 消息入口，避免入口重建公共资源。"""

from agent_bot.plugins.contracts import PluginContext

_context: PluginContext | None = None


def bind_context(context: PluginContext) -> None:
    """每个驱动仅绑定一个上下文，同一实例重复绑定保持幂等。"""
    global _context
    if _context is not None and _context.driver is context.driver and _context is not context:
        raise RuntimeError("QQBot context already bound")
    _context = context


def current_context() -> PluginContext | None:
    """独立 NoneBot 兼容入口未绑定时明确返回空上下文。"""
    return _context
