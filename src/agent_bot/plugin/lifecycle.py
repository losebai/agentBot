"""兼容旧 QQ 入口的租约导出，生命周期实现只有公共核心一份。"""

from agent_bot.core.lifecycle import (
    RuntimeLeaseGate as RuntimeLeaseGate,
)
from agent_bot.core.lifecycle import (
    apply_runtime_settings as apply_runtime_settings,
)
from agent_bot.core.lifecycle import (
    settings_refresh_loop as settings_refresh_loop,
)
from agent_bot.core.lifecycle import (
    start_background_task as start_background_task,
)
from agent_bot.core.lifecycle import (
    stop_background_task as stop_background_task,
)
