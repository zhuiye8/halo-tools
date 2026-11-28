"""Bitmap conversion and upload payload encoding tests (Pillow required)."""

from __future__ import annotations

import io

import pytest

from halobar import bitmap

pytest.importorskip("PIL")


def png_bytes(size=(64, 32), color=(255, 255, 255), frames=1) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    if frames == 1:
        Image.new("RGB", size, color).save(buffer, format="PNG")
    else:
        # Distinct colours keep every GIF frame (identical frames get optimised away).
        images = [Image.new("RGB", size, ((20 * index) % 256, 255 - 10 * index, 128))
                  for index in range(frames)]
        images[0].save(buffer, format="GIF", save_all=True, append_images=images[1:],
                       duration=120, loop=0)
    return buffer.getvalue()


def test_static_encoding_layout():
    asset = bitmap.load(png_bytes(size=(256, 32)), "white.png")
    processed = bitmap.apply_options(asset)
    assert processed.animated is False
    assert processed.category == bitmap.STATIC_CATEGORY
    assert processed.option == bitmap.STATIC_OPTION

    stream = bitmap.encode(processed)
    assert len(stream) == 8 * 133
    assert stream[0] == bitmap.STATIC_CATEGORY
    assert stream[1:3] == b"\x00\x00"
    assert stream[3:5] == b"\xff\xff"
    # A fully white source must produce all-on bytes (threshold 128).
    assert stream[5:133] == b"\xff" * 128


def test_animation_encoding_and_delays():
    asset = bitmap.load(png_bytes(frames=2), "two.gif")
    assert len(asset.frames) == 2
    processed = bitmap.apply_options(asset)
    stream = bitmap.encode(processed)
    assert len(stream) == 2 * 8 * 133
    assert stream[0] == bitmap.ANIMATION_CATEGORY
    first_delay = int.from_bytes(stream[3:5], "big")
    assert 100 <= first_delay <= 65535


def test_pixel_bytes_is_1024_bytes():
    asset = bitmap.apply_options(bitmap.load(png_bytes(), "x.png"))
    assert len(bitmap.pixel_bytes(asset.frames[0])) == 1024


def test_preview_png_is_png():
    asset = bitmap.apply_options(bitmap.load(png_bytes(), "x.png"))
    preview = bitmap.preview_png(asset.frames[0], scale=2)
    assert preview[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(preview) > 100


def test_invert_and_threshold_change_pixels():
    asset = bitmap.load(png_bytes(color=(200, 200, 200)), "gray.png")
    normal = bitmap.apply_options(asset, threshold=128)
    inverted = bitmap.apply_options(asset, threshold=128, invert=True)
    assert bitmap.pixel_bytes(normal.frames[0]) != bitmap.pixel_bytes(inverted.frames[0])
    high = bitmap.apply_options(asset, threshold=250)
    assert bitmap.pixel_bytes(high.frames[0]) != bitmap.pixel_bytes(normal.frames[0])


def test_auto_samples_long_gif_and_strict_rejects():
    long_gif = png_bytes(frames=16)
    sampled = bitmap.load(long_gif, "long.gif")
    assert sampled.source_frames == 16
    assert sampled.sampled is True
    assert len(sampled.frames) == bitmap.MAX_FRAMES
    assert all(d >= 100 for d in sampled.delays)

    selected = bitmap.apply_options(sampled, selected_indices=[0, 2, 4], speed=2.0)
    assert len(selected.frames) == 3
    assert selected.animated is True

    with pytest.raises(bitmap.AssetError):
        bitmap.load(png_bytes(frames=9), "nine.gif", auto_sample=False)


def test_rejects_garbage():
    with pytest.raises(bitmap.AssetError):
        bitmap.load(b"not an image", "x.png")