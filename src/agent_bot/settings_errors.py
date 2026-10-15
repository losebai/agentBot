"""启动文件与运行配置共享的错误类型，不导入后端或初始化日志。"""


class SettingsError(RuntimeError):
    """受控配置操作错误基类。"""


class SettingsRevisionConflictError(SettingsError):
    """配置快照在保存前已被其他进程更新。"""


class SettingsValidationError(SettingsError):
    """配置快照内容不符合当前受控模型。"""
