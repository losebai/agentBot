"""本地模型目录预检的错误分类。"""


class ModelNotAvailableError(RuntimeError):
    """配置的模型未登记或未启用。"""


class ModelGuardUnavailableError(ModelNotAvailableError):
    """模型目录暂时无法查询，可由持久队列安全重试。"""
