"""一次聊天请求内收集 AI 返回的图片，不把图片字节混入正文或历史。"""

from __future__ import annotations

import base64
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar

from agent_bot.media_artifacts import ImageArtifact


class ToolImageCollection:
    """按内容去重并限制一次请求的图片总量。"""

    def __init__(self) -> None:
        """每次生成使用独立集合。"""
        self.items: tuple[ImageArtifact, ...] = ()

    def add(self, images: Sequence[ImageArtifact]) -> None:
        """完整验证后原子更新，超限结果不投递部分附件。"""
        merged = list(self.items)
        for image in images:
            if image not in merged:
                merged.append(image)
        if len(merged) > 4 or sum(
            len(base64.b64decode(image.data, validate=True)) for image in merged
        ) > 10_485_760:
            raise ValueError("tool images exceed request limits")
        self.items = tuple(merged)


_active: ContextVar[ToolImageCollection | None] = ContextVar("tool_images", default=None)


@contextmanager
def capture_tool_images() -> Iterator[ToolImageCollection]:
    """隔离并发消息、失败请求和后续重试的附件。"""
    collection = ToolImageCollection()
    token = _active.set(collection)
    try:
        yield collection
    finally:
        _active.reset(token)


def record_tool_images(images: Sequence[ImageArtifact]) -> None:
    """将已完成响应的附件交给当前队列生成过程。"""
    collection = _active.get()
    if collection is not None:
        collection.add(images)


def has_tool_images() -> bool:
    """纯图片结果可完成生成，不强制模型再返回占位正文。"""
    collection = _active.get()
    return collection is not None and bool(collection.items)


def current_tool_images() -> tuple[ImageArtifact, ...]:
    """读取当前请求中已验证的图片，不建立跨请求共享缓存。"""
    collection = _active.get()
    return collection.items if collection is not None else ()
