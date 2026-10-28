"""B 站内部凭据和模型可见参数的严格边界。"""

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class Account(BaseModel):
    """凭据只供服务端 HTTP 请求使用，默认序列化和 repr 均排除。"""

    uid: str = Field(pattern=r'^[1-9][0-9]{0,19}$')
    name: str = Field(max_length=200)
    cookies: SecretStr = Field(exclude=True, repr=False)

    def cookie_values(self) -> dict[str, str]:
        """仅在请求边界显式解包允许的 Cookie。"""
        return json.loads(self.cookies.get_secret_value())


class Arguments(BaseModel):
    """模型参数不能夹带账号、Cookie、路径或服务端身份。"""

    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, strict=True)


class StatusArguments(Arguments):
    """检测工具没有可由模型指定的账号参数。"""


class MyVideosArguments(Arguments):
    """分页读取当前登录账号的已发布投稿，不能指定其他账号或私有稿件状态。"""

    page: int = Field(default=1, ge=1, le=10000, description='页码，从 1 开始')
    page_size: int = Field(default=10, ge=1, le=20, description='每页最多 20 个视频')


class VideoArguments(Arguments):
    """仅接受 BV 或 AV 标识，不接受任意网络地址。"""

    video_id: str = Field(pattern=r'^(?:BV[0-9A-Za-z]{10}|av[1-9][0-9]{0,19})$')


class CommentsArguments(VideoArguments):
    """分页必须有界，根评论可用于读取楼中楼。"""

    page: int = Field(default=1, ge=1, le=10000)
    page_size: int = Field(default=10, ge=1, le=20)
    root: int | None = Field(default=None, ge=1)


class ReplyArguments(VideoArguments):
    """根评论与目标评论显式提供，避免误发为顶层评论。"""

    root: int = Field(ge=1)
    parent: int = Field(ge=1)
    message: str = Field(min_length=1, max_length=1000)


class DynamicPublishArguments(Arguments):
    """图文动态只接受本会话的 PNG/JPEG 资产，图片顺序与传入顺序一致。"""

    text: str = Field(min_length=1, max_length=2000, description='动态正文，最多 2000 字')
    image_asset_ids: list[Annotated[str, Field(pattern=r'^[a-f0-9]{32}$')]] = Field(
        min_length=1, max_length=9, description='按发布顺序排列的图片资产 ID，最多 9 张',
    )

    @field_validator('image_asset_ids')
    @classmethod
    def unique_images(cls, value: list[str]) -> list[str]:
        """拒绝重复资产，防止同一图片被意外多次插入动态。"""
        if len(set(value)) != len(value):
            raise ValueError('duplicate image assets')
        return value


class PublishArguments(Arguments):
    """投稿使用当前会话资产，原创与转载不允许静默替换。"""

    video_asset_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    cover_asset_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(default='', max_length=2000)
    tid: int = Field(ge=1)
    tags: list[str] = Field(min_length=1, max_length=12)
    copyright: Literal[1, 2] = 1
    source: str = Field(default='', max_length=200)

    @field_validator('tags')
    @classmethod
    def valid_tags(cls, value: list[str]) -> list[str]:
        """标签作为平台逗号分隔字段，拒绝空项与分隔符注入。"""
        if any(not item.strip() or len(item) > 20 or ',' in item for item in value):
            raise ValueError('invalid tags')
        return [item.strip() for item in value]

    @model_validator(mode='after')
    def require_source(self) -> 'PublishArguments':
        """转载缺来源应在任何上传之前明确失败。"""
        if self.copyright == 2 and not self.source:
            raise ValueError('repost source required')
        return self


TOOL_MODELS = {
    'bilibili_login_status': StatusArguments,
    'bilibili_get_my_videos': MyVideosArguments,
    'bilibili_get_comments': CommentsArguments,
    'bilibili_reply_comment': ReplyArguments,
    'bilibili_publish_video': PublishArguments,
    'bilibili_publish_dynamic': DynamicPublishArguments,
}
WRITE_TOOLS = frozenset({
    'bilibili_reply_comment', 'bilibili_publish_video', 'bilibili_publish_dynamic',
})
BILIBILI_TOOLS = frozenset(TOOL_MODELS)
