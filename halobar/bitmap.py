"""Image → 256x32 monochrome bitmap conversion and upload payload encoding.

The device stores 1bpp frames: 4 pages of 8 rows, page-major, one byte per column,
LSB = top row of the page.  A frame is 1024 bytes; an upload record adds a 5-byte
header (scene category, big-endian record index, big-endian delay).

Static uploads use category 3; animations use category 6 with up to 8 frames.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

WIDTH, HEIGHT = 256, 32
MAX_FRAMES = 8
MIN_DELAY_MS = 100
MAX_DELAY_MS = 65535
MAX_BYTES = 32 * 1024 * 1024
MAX_PIXELS = 40_000_000

STATIC_CATEGORY = 3
STATIC_OPTION = 6
ANIMATION_CATEGORY = 6
ANIMATION_OPTION = 52


class AssetError(ValueError):
    """Raised for unsupported or oversized input."""


@dataclass
class Asset:
    frames: list[object] = field(default_factory=list)   # PIL images in mode "1"
    delays: list[int] = field(default_factory=list)
    name: str = ""

    @property
    def animated(self) -> bool:
        return len(self.frames) > 1

    @property
    def category(self) -> int:
        return ANIMATION_CATEGORY if self.animated else STATIC_CATEGORY

    @property
    def option(self) -> int:
        return ANIMATION_OPTION if self.animated else STATIC_OPTION


def _pil():
    try:
        from PIL import Image, ImageDraw, ImageOps  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError("素材功能需要 Pillow：pip install pillow") from exc
    from PIL import Image, ImageOps

    return Image, ImageOps


def load(data: bytes, name: str = "") -> "Asset":
    """Decode PNG/JPG/BMP/WebP/GIF into raw RGBA frames (animated GIF supported)."""
    if len(data) > MAX_BYTES:
        raise AssetError("文件不能超过 32 MB")
    Image, ImageOps = _pil()
    try:
        source = Image.open(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - Pillow raises a zoo of errors
        raise AssetError(f"无法解析图片：{exc}") from exc

    frames = []
    delays = []
    total_pixels = 0
    try:
        count = getattr(source, "n_frames", 1)
        if count > MAX_FRAMES:
            raise AssetError(f"最多支持 {MAX_FRAMES} 帧（当前 {count} 帧）")
        for index in range(count):
            source.seek(index)
            frame = ImageOps.exif_transpose(source).convert("RGBA")
            total_pixels += frame.width * frame.height
            if total_pixels > MAX_PIXELS:
                raise AssetError("所有帧合计像素超过 4000 万")
            frames.append(frame)
            delay = int(source.info.get("duration", MIN_DELAY_MS) or MIN_DELAY_MS)
            delays.append(max(MIN_DELAY_MS, min(MAX_DELAY_MS, delay)))
    except AssetError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AssetError(f"无法读取图片帧：{exc}") from exc
    finally:
        source.close()

    if not frames:
        raise AssetError("图片没有可用帧")
    return Asset(frames=frames, delays=delays, name=name)


def prepare(frame, *, fit: str = "contain", threshold: int = 128,
            invert: bool = False, dither: bool = False, zoom: float = 1.0,
            offset_x: float = 0.0, offset_y: float = 0.0,
            brightness: int = 0, contrast: float = 1.0):
    """Convert one RGBA frame into a 256x32 1bpp image.

    ``zoom`` > 1 magnifies the picture, ``offset_x``/``offset_y`` pan it (fractions
    of the source size, -1..1), ``brightness`` shifts levels (-100..100) and
    ``contrast`` scales them (0.2..3).  The same transform is mirrored in the
    console's JavaScript preview so what you see is what gets uploaded.
    """
    Image, ImageOps = _pil()
    background = Image.new("RGBA", frame.size, (0, 0, 0, 255))
    gray = Image.alpha_composite(background, frame).convert("L")

    # fit the source into the 256x32 canvas, then apply zoom/pan on the canvas
    if fit == "cover":
        fitted = ImageOps.fit(gray, (WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    elif fit == "stretch":
        fitted = gray.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    else:  # contain
        small = ImageOps.contain(gray, (WIDTH, HEIGHT), Image.Resampling.LANCZOS)
        fitted = Image.new("L", (WIDTH, HEIGHT), 0)
        fitted.paste(small, ((WIDTH - small.width) // 2, (HEIGHT - small.height) // 2))

    if zoom != 1.0 or offset_x or offset_y:
        scaled_w = max(1, int(round(WIDTH * zoom)))
        scaled_h = max(1, int(round(HEIGHT * zoom)))
        scaled = fitted.resize((scaled_w, scaled_h), Image.Resampling.LANCZOS)
        canvas = Image.new("L", (WIDTH, HEIGHT), 0)
        x = int(round((WIDTH - scaled_w) / 2 + offset_x * WIDTH))
        y = int(round((HEIGHT - scaled_h) / 2 + offset_y * HEIGHT))
        canvas.paste(scaled, (x, y))
        fitted = canvas

    pixels = list(fitted.getdata())
    if brightness or contrast != 1.0:
        pixels = [
            max(0, min(255, int(round((value - 128) * contrast + 128 + brightness))))
            for value in pixels
        ]
    if invert:
        pixels = [255 - value for value in pixels]

    if dither:
        out = _floyd_steinberg(pixels, threshold)
    else:
        out = [255 if value >= threshold else 0 for value in pixels]

    image = Image.new("1", (WIDTH, HEIGHT))
    image.putdata([1 if value else 0 for value in out])
    return image


def _floyd_steinberg(pixels: list[int], threshold: int) -> list[int]:
    values = [float(value) for value in pixels]
    result = [0] * len(values)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            index = y * WIDTH + x
            old = values[index]
            new = 255.0 if old >= threshold else 0.0
            result[index] = int(new)
            error = old - new
            if x + 1 < WIDTH:
                values[index + 1] += error * 7 / 16
            if y + 1 < HEIGHT:
                if x > 0:
                    values[index + WIDTH - 1] += error * 3 / 16
                values[index + WIDTH] += error * 5 / 16
                if x + 1 < WIDTH:
                    values[index + WIDTH + 1] += error / 16
    return result


def pixel_bytes(image) -> bytes:
    """Pack a mode-1 256x32 image into 1024 device bytes."""
    if image.size != (WIDTH, HEIGHT):
        raise AssetError(f"图片必须是 {WIDTH}x{HEIGHT}")
    mono = image.convert("1")
    out = bytearray()
    for page in range(HEIGHT // 8):
        for x in range(WIDTH):
            value = 0
            for bit in range(8):
                if mono.getpixel((x, page * 8 + bit)):
                    value |= 1 << bit
            out.append(value)
    return bytes(out)


def encode_static(image) -> bytes:
    """8 records for a still frame (delay 0xFFFF)."""
    pixels = pixel_bytes(image)
    records = []
    for index in range(8):
        records.append(bytes([STATIC_CATEGORY])
                       + index.to_bytes(2, "big")
                       + b"\xff\xff"
                       + pixels[index * 128:(index + 1) * 128])
    return b"".join(records)


def encode_animation(frames: list, delays: list[int]) -> bytes:
    if not 2 <= len(frames) <= MAX_FRAMES:
        raise AssetError("动画需要 2–8 帧")
    if len(delays) != len(frames):
        raise AssetError("帧数与时长数量不一致")
    records = []
    for frame_index, (frame, delay) in enumerate(zip(frames, delays)):
        if not MIN_DELAY_MS <= delay <= MAX_DELAY_MS:
            raise AssetError(f"每帧时长必须在 {MIN_DELAY_MS}–{MAX_DELAY_MS} ms")
        pixels = pixel_bytes(frame)
        for block in range(8):
            record_index = frame_index * 8 + block
            records.append(bytes([ANIMATION_CATEGORY])
                           + record_index.to_bytes(2, "big")
                           + int(delay).to_bytes(2, "big")
                           + pixels[block * 128:(block + 1) * 128])
    return b"".join(records)


def encode(asset: "Asset") -> bytes:
    if asset.animated:
        return encode_animation(asset.frames, asset.delays)
    return encode_static(asset.frames[0])


def preview_png(image, scale: int = 3) -> bytes:
    """Upscaled PNG preview for the console (nearest neighbour keeps pixels crisp)."""
    Image, _ = _pil()
    scaled = image.convert("L").resize((WIDTH * scale, HEIGHT * scale), Image.Resampling.NEAREST)
    buffer = io.BytesIO()
    scaled.save(buffer, format="PNG")
    return buffer.getvalue()


def apply_options(asset: "Asset", **options) -> "Asset":
    frames = [prepare(frame, **options) for frame in asset.frames]
    return Asset(frames=frames, delays=list(asset.delays), name=asset.name)