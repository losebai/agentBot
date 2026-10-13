"""按启动配置选择免登录或恒定时间的管理员密码校验。"""

import secrets

from fastapi import HTTPException


def require_token(authorization: str | None, expected: str) -> None:
    """空密码直接允许管理访问；设置密码后拒绝缺失或错误的认证头。"""
    if not expected:
        return
    scheme, separator, supplied = (authorization or "").partition(" ")
    if (
        scheme.casefold() != "bearer"
        or not separator
        or not secrets.compare_digest(supplied.encode(), expected.encode())
    ):
        raise HTTPException(401, "管理密码无效", headers={"WWW-Authenticate": "Bearer"})
