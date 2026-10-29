"""在独立解码预算内保留表情包原件，并生成静态视觉预览。"""

import warnings
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, UnidentifiedImageError

from agent_bot.media.errors import MediaError

MAX_BYTES = 10 * 1024 * 1024
MAX_PREVIEW_BYTES = 2 * 1024 * 1024
MAX_FRAME_PIXELS = 16_000_000
MAX_DECODED_PIXELS = 80_000_000
MAX_FRAMES = 300
FORMATS = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp', 'GIF': 'image/gif'}


@dataclass(frozen=True)
class PreparedStickerImage:
    """原始数据用于投递，PNG 代表帧只提供给识图模型。"""

    data: bytes
    mime_type: str
    preview: bytes
    width: int
    height: int
    frame_count: int
    animated: bool


def prepare_sticker_image(data: bytes, content_type: str | None = None) -> PreparedStickerImage:
    """逐帧验证真实容器和预算并保留动画，不改变普通媒体图片规则。

    content_type 保留调用兼容；浏览器声明可能来自错误后缀，类型以解码结果为准。
    """
    if not data:
        raise MediaError('STICKER_INVALID_IMAGE', status_code=422)
    if len(data) > MAX_BYTES:
        raise MediaError('STICKER_TOO_LARGE', status_code=413)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                mime = FORMATS.get(image.format)
                if mime is None:
                    raise MediaError('STICKER_UNSUPPORTED_FORMAT', status_code=422)
                width, height = image.size
                if width * height > MAX_FRAME_PIXELS:
                    raise MediaError('STICKER_PIXEL_BUDGET_EXCEEDED', status_code=422)
                frames = getattr(image, 'n_frames', 1)
                if frames > MAX_FRAMES:
                    raise MediaError('STICKER_TOO_MANY_FRAMES', status_code=422)
                selected = {0, frames // 2, frames - 1}
                thumbnails = []
                pixels = 0
                for index in range(frames):
                    image.seek(index)
                    frame_pixels = image.width * image.height
                    pixels += frame_pixels
                    if frame_pixels > MAX_FRAME_PIXELS or pixels > MAX_DECODED_PIXELS:
                        raise MediaError('STICKER_PIXEL_BUDGET_EXCEEDED', status_code=422)
                    image.load()
                    if index in selected:
                        frame = image.convert('RGBA')
                        frame.thumbnail((512, 512), Image.Resampling.LANCZOS)
                        thumbnails.append(frame)
                preview_image = Image.new('RGB',
                                          (sum(f.width for f in thumbnails),
                                           max(f.height for f in thumbnails)), 'white')
                left = 0
                for frame in thumbnails:
                    preview_image.paste(frame, (left, 0), frame)
                    left += frame.width
                # PNG 必须满足模型输入上限；缩小画布不会改变保存和发送的原件。
                while True:
                    output = BytesIO()
                    preview_image.save(output, format='PNG')
                    preview = output.getvalue()
                    if len(preview) <= MAX_PREVIEW_BYTES:
                        break
                    preview_image = preview_image.resize(
                        (max(1, preview_image.width // 2), max(1, preview_image.height // 2)),
                        Image.Resampling.LANCZOS,
                    )
                return PreparedStickerImage(data, mime, preview, width, height, frames, frames > 1)
    except MediaError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise MediaError('STICKER_PIXEL_BUDGET_EXCEEDED', status_code=422) from error
    except (UnidentifiedImageError, OSError, ValueError, EOFError) as error:
        raise MediaError('STICKER_INVALID_IMAGE', status_code=422) from error
