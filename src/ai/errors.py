"""Safe AI failure categories, independent of provider response bodies."""


class AiError(RuntimeError):
    """Carry a stable public category without including upstream or input content."""

    def __init__(self, code: str, *, status_code: int = 502) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
