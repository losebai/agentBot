"""媒体部署目录和不可被客户端放宽的资源限制。"""

import os
from pathlib import Path

from dotenv import dotenv_values
from pydantic import Field, field_validator

from agent_bot.ai.contracts import AiModel

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024
MAX_MODEL_IMAGE_BYTES = 2 * 1024 * 1024
MAX_MODEL_IMAGE_PIXELS = 16 * 1024 * 1024
MAX_IMAGES = 8
STORAGE_QUOTA_BYTES = 1024 * 1024 * 1024
RETENTION_SECONDS = 24 * 60 * 60
CLEANUP_INTERVAL_SECONDS = 60
ORPHAN_RETENTION_SECONDS = 60 * 60
MAX_GENERATIONS = 2
MAX_GENERATION_SECONDS = 120
MAX_UPLOADS = 2
UPLOAD_SECONDS = 30


class MediaConfig(AiModel):
    """只解析启动配置，目录创建与独占检查由资源所有者执行。"""

    directory: Path = Field(default_factory=lambda: Path.cwd() / "data/media")

    @field_validator("directory", mode="before")
    @classmethod
    def valid_directory(cls, value):
        """拒绝空目录和控制字符，保留原路径以检查目录联接。"""
        if not str(value).strip() or any(ord(c) < 32 for c in str(value)):
            raise ValueError("invalid media directory")
        return Path(value).absolute()

    @classmethod
    def from_environment(cls):
        """环境优先于不插值的配置文件，不读取或输出其他密钥。"""
        values = dotenv_values(".env", interpolate=False)
        directory = os.environ.get(
            "AGENT_BOT_MEDIA_DIRECTORY", values.get("AGENT_BOT_MEDIA_DIRECTORY")
        )
        return cls(directory=directory) if directory is not None else cls()
