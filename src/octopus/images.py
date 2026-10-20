"""Decode untrusted image bytes and send only freshly encoded pixels to Codex."""
from __future__ import annotations

import base64
import io
import threading
import warnings

from PIL import Image, ImageOps


MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 12_000_000
MAX_IMAGE_DIMENSION = 8192
_FORMATS = ("PNG", "JPEG", "WEBP", "GIF")
_DECODE_LOCK = threading.Lock()


class ImageInputError(ValueError):
    """A safe, fixed message suitable for replying to the image sender."""


class _LimitedOutput(io.BytesIO):
    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > MAX_IMAGE_BYTES:
            raise ImageInputError("图片转换后超过 5 MB，请缩小图片后重试。")
        return super().write(data)


def _check_image(image: Image.Image) -> None:
    width, height = image.size
    if (
        width < 1 or height < 1
        or width * height > MAX_IMAGE_PIXELS
        or max(width, height) > MAX_IMAGE_DIMENSION
    ):
        raise ImageInputError("图片尺寸过大，请缩小到 1200 万像素以内，且单边不超过 8192 像素。")
    if getattr(image, "is_animated", False):
        raise ImageInputError("暂不支持动态图片，请发送静态截图。")


def prepare_image(data: bytes) -> str:
    """Return a bounded JPEG/PNG data URL without original metadata or files."""
    if not isinstance(data, bytes) or not data:
        raise ImageInputError("图片内容无效，请重新发送 PNG、JPEG、WebP 或静态 GIF 图片。")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageInputError("图片超过 5 MB，请缩小图片后重试。")
    # Hold the lock in the worker thread: cancelling an asyncio.to_thread caller
    # cannot release it while Pillow is still decoding or encoding an image.
    with _DECODE_LOCK:
        return _prepare_image(data)


def _prepare_image(data: bytes) -> str:
    try:
        # Check the structure separately, because verify() invalidates some decoders.
        # Do not change Pillow's global pixel limit or truncated-image setting.
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=_FORMATS) as probe:
                _check_image(probe)
                probe.verify()
            with Image.open(io.BytesIO(data), formats=_FORMATS) as source:
                _check_image(source)
                source.load()
                output_format = "JPEG" if source.format == "JPEG" else "PNG"
                with ImageOps.exif_transpose(source) as oriented:
                    mode = "RGBA" if (
                        "A" in oriented.getbands() or "transparency" in oriented.info
                    ) else "RGB"
                    with oriented.convert(mode) as converted:
                        # Image.copy/convert can inherit EXIF, comments and ICC data.
                        # A fresh canvas copies only pixels.
                        with Image.new(mode, converted.size) as clean:
                            clean.paste(converted)
                            with _LimitedOutput() as output:
                                options = {"quality": 90} if output_format == "JPEG" else {}
                                clean.save(output, format=output_format, **options)
                                payload = base64.b64encode(output.getvalue()).decode("ascii")
                media_type = "image/jpeg" if output_format == "JPEG" else "image/png"
                return f"data:{media_type};base64,{payload}"
    except ImageInputError:
        raise
    except Exception:
        # Decoder errors can contain attacker-controlled data; never send them back.
        raise ImageInputError("图片无法读取，请重新发送 PNG、JPEG、WebP 或静态 GIF 图片。") from None
