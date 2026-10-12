"""统一图片解码、静态限制、隐私清理与模型预算。"""

from collections.abc import Sequence
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

from agent_bot.database_pool import finish_database_write
from agent_bot.media.config import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES,
    MAX_MODEL_IMAGE_BYTES,
    MAX_MODEL_IMAGE_PIXELS,
    MAX_TOTAL_IMAGE_BYTES,
)
from agent_bot.media.contracts import ModelImage, ValidatedImage
from agent_bot.media.errors import MediaError
from agent_bot.multimodal import InvalidImageAttachmentError, _normalize_model_image

MIME_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}


class ImagePreparer:
    """保留原图供工作流使用，另建无元数据的模型副本。"""

    def prepare(self, data: bytes, content_type: str | None) -> ValidatedImage:
        """校验真实容器和像素后再编码，禁止仅信任 MIME 或文件扩展名。"""
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise MediaError("MEDIA_TOO_LARGE", status_code=413)
        try:
            with Image.open(BytesIO(data)) as source:
                mime = MIME_TYPES.get(source.format)
                if mime is None or (
                    content_type and content_type.split(";")[0].strip().lower() != mime
                ):
                    raise MediaError("MEDIA_INVALID_IMAGE", status_code=422)
                if source.width * source.height > MAX_MODEL_IMAGE_PIXELS:
                    raise MediaError("MEDIA_TOO_LARGE", status_code=413)
                if getattr(source, "is_animated", False) or getattr(source, "n_frames", 1) != 1:
                    raise MediaError("MEDIA_ANIMATION_UNSUPPORTED", status_code=422)
                source.verify()
            with Image.open(BytesIO(data)) as source, ImageOps.exif_transpose(source) as oriented:
                oriented.load()
                normalized = oriented.convert("RGBA" if "A" in oriented.getbands() else "RGB")
                try:
                    normalized.info.clear()
                    output = BytesIO()
                    normalized.save(output, format="PNG")
                finally:
                    normalized.close()
            model_mime, model_data = _normalize_model_image(
                output.getvalue(), media_type="image/png", max_bytes=MAX_MODEL_IMAGE_BYTES
            )
            return ValidatedImage(
                media_type=mime,
                data=data,
                model_image=ModelImage(
                    media_type=model_mime, data=model_data, source_size_bytes=len(data)
                ),
            )
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            OSError,
            UnidentifiedImageError,
            InvalidImageAttachmentError,
            ValueError,
        ) as error:
            raise MediaError("MEDIA_INVALID_IMAGE", status_code=422) from error

    async def prepare_async(self, data: bytes, content_type: str | None) -> ValidatedImage:
        """在有界工作线程计算；取消后等待线程结束，避免提前释放输入资源。"""
        return await finish_database_write(lambda: self.prepare(data, content_type))

    def prepare_many(
        self, images: Sequence[ValidatedImage], *, maximum_count: int
    ) -> tuple[ModelImage, ...]:
        """按原始字节累计预算，不静默丢图或按压缩结果放宽输入限制。"""
        if len(images) > min(maximum_count, MAX_IMAGES) or maximum_count < 1:
            raise MediaError("MEDIA_TOO_MANY_IMAGES", status_code=422)
        if sum(len(image.data) for image in images) > MAX_TOTAL_IMAGE_BYTES:
            raise MediaError("MEDIA_TOO_LARGE", status_code=413)
        return tuple(image.model_image for image in images)
