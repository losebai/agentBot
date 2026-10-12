"""Constant-time verification of the independent management credential."""

import secrets

from fastapi import HTTPException


def require_token(authorization: str | None, expected: str) -> None:
    """Reject missing or incorrect Bearer headers without returning credential values."""
    scheme, separator, supplied = (authorization or "").partition(" ")
    if (
        scheme.casefold() != "bearer"
        or not separator
        or not secrets.compare_digest(supplied.encode(), expected.encode())
    ):
        raise HTTPException(401, "管理凭据无效", headers={"WWW-Authenticate": "Bearer"})
