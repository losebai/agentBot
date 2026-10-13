"""NapCatQQ / OneBot V11 插件入口。"""

from __future__ import annotations

from typing import Any

from agent_bot.plugin import application as _application

__plugin_meta__ = _application.__plugin_meta__


def __getattr__(name: str) -> Any:
    """把入口兼容属性延迟委托给唯一的应用装配模块。"""

    return getattr(_application, name)


def __dir__() -> list[str]:
    """合并入口与应用装配模块的可发现属性。"""

    return sorted(set(globals()) | set(dir(_application)))
