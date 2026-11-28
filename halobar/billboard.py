"""Custom 256x32 pixel billboard renderer (bold/art text, borders, mini animations).

Unlike real-time lyrics (which write UTF-8 to the device ROM font slot to avoid
flash wear), static and animated billboards render arbitrary Windows TrueType
fonts, bold stroke weights, borders and 4-frame accents into 1bpp bitmaps that
are uploaded once to scene 3/6 (static) or 6/52 (animated).
"""

from __future__ import annotations

import math
import os
from pathlib import Path

from . import bitmap

# Preferred Windows font stacks per style; gracefully falls back to available fonts.
_FONT_STACKS: dict[str, list[str]] = {
    "bold": ["msyhbd.ttc", "simhei.ttf", "segoeuib.ttf", "arialbd.ttf"],
    "sans": ["msyh.ttc", "segoeui.ttf", "arial.ttf"],
    "mono": ["CascadiaMono.ttf", "consolab.ttf", "consola.ttf", "lucon.ttf", "msyhbd.ttc"],
    "impact": ["impact.ttf", "arialbd.ttf", "msyhbd.ttc", "simhei.ttf"],
    "serif": ["simsun.ttc", "msyhbd.ttc", "georgia.ttf"],
}

BORDERS = ("none", "frame", "corners", "pill", "bars")
MOTION_MODES = ("static", "pulse", "bars", "cursor")
ALIGNMENTS = ("left", "center", "right")


def _font_dirs() -> list[Path]:
    dirs: list[Path] = []
    win = os.environ.get("WINDIR") or r"C:\Windows"
    dirs.append(Path(win) / "Fonts")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    return [d for d in dirs if d.exists()]


def _load_font(style: str, size: int):
    from PIL import ImageFont

    candidates = _FONT_STACKS.get(style, _FONT_STACKS["bold"]) + _FONT_STACKS["bold"]
    seen: set[str] = set()
    for folder in _font_dirs():
        for name in candidates:
            low = name.lower()
            if low in seen:
                continue
            path = folder / name
            if path.exists():
                seen.add(low)
                try:
                    return ImageFont.truetype(str(path), size=size)
                except OSError:
                    continue
    return ImageFont.load_default()


def _text_width(draw, text: str, font, tracking: int = 0) -> tuple[int, int]:
    if not text:
        return 0, 0
    if tracking == 0:
        bbox = draw.textbbox((0, 0), text, font=font)
        return max(0, bbox[2] - bbox[0]), max(0, bbox[3] - bbox[1])
    total = 0
    max_h = 0
    for idx, ch in enumerate(text):
        bbox = draw.textbbox((0, 0), ch, font=font)
        total += max(1, bbox[2] - bbox[0])
        if idx < len(text) - 1:
            total += tracking
        max_h = max(max_h, bbox[3] - bbox[1])
    return total, max_h


def _draw_tracked_text(draw, x: int, y: int, text: str, font, *,
                       fill: int = 255, tracking: int = 0, stroke: int = 0) -> None:
    if tracking == 0:
        draw.text((x, y), text, font=font, fill=fill, stroke_width=stroke, stroke_fill=fill)
        return
    cursor = x
    for ch in text:
        bbox = draw.textbbox((0, 0), ch, font=font)
        draw.text((cursor - bbox[0], y), ch, font=font, fill=fill,
                  stroke_width=stroke, stroke_fill=fill)
        cursor += max(1, bbox[2] - bbox[0]) + tracking


def _draw_border(draw, border: str, *, phase: int = 0, inverted: bool = False) -> None:
    w, h = bitmap.WIDTH, bitmap.HEIGHT
    fg = 0 if inverted else 255
    if border == "frame":
        draw.rounded_rectangle((1, 1, w - 2, h - 2), radius=4, outline=fg, width=1)
    elif border == "corners":
        arm = 8
        # Four tactical corner brackets
        for pts in (
            [(1, arm), (1, 1), (arm, 1)],
            [(w - 2 - arm, 1), (w - 2, 1), (w - 2, arm)],
            [(1, h - 2 - arm), (1, h - 2), (arm, h - 2)],
            [(w - 2 - arm, h - 2), (w - 2, h - 2), (w - 2, h - 2 - arm)],
        ):
            draw.line(pts, fill=fg, width=2)
    elif border == "pill":
        draw.rounded_rectangle((2, 2, w - 3, h - 3), radius=13, outline=fg, width=2)
    elif border == "bars":
        # Decorative equalizer columns on both flanks
        for side in (0, 1):
            for col in range(3):
                bar_h = 6 + int(round(12 * (0.5 + 0.5 * math.sin(phase * 1.57 + col * 1.1 + side))))
                x = 6 + col * 5 if side == 0 else w - 9 - col * 5
                y0 = (h - bar_h) // 2
                draw.rectangle((x, y0, x + 2, y0 + bar_h), fill=fg)


def _render_frame(
    text: str,
    *,
    subtext: str = "",
    font_style: str = "bold",
    font_size: int = 22,
    bold_stroke: int = 0,
    tracking: int = 0,
    align: str = "center",
    border: str = "none",
    invert: bool = False,
    motion: str = "static",
    phase: int = 0,
):
    from PIL import Image, ImageDraw

    w, h = bitmap.WIDTH, bitmap.HEIGHT
    bg = 255 if invert else 0
    fg = 0 if invert else 255
    canvas = Image.new("L", (w, h), bg)
    draw = ImageDraw.Draw(canvas)

    # Side padding keeps text clear of decorative borders/bars.
    pad_x = 24 if border == "bars" else (12 if border != "none" else 6)
    inner_w = w - pad_x * 2

    display_text = text
    if motion == "cursor" and phase % 2 == 0:
        display_text = f"{text}_"

    if subtext:
        main_size = max(11, min(15, int(round(font_size * 0.68))))
        sub_size = max(9, min(11, int(round(font_size * 0.48))))
        f_main = _load_font(font_style, main_size)
        f_sub = _load_font("sans", sub_size)
        mw, mh = _text_width(draw, display_text, f_main, tracking)
        while mw > inner_w and main_size > 10:
            main_size -= 1
            f_main = _load_font(font_style, main_size)
            mw, mh = _text_width(draw, display_text, f_main, tracking)
        sw, sh = _text_width(draw, subtext, f_sub, 0)
        while sw > inner_w and sub_size > 8:
            sub_size -= 1
            f_sub = _load_font("sans", sub_size)
            sw, sh = _text_width(draw, subtext, f_sub, 0)

        m_bbox = draw.textbbox((0, 0), display_text or "A", font=f_main)
        s_bbox = draw.textbbox((0, 0), subtext or "A", font=f_sub)
        gap = 3 if (mh + sh + 3) <= (h - 4) else 2
        total_h = mh + gap + sh
        top_y = max(2, (h - total_h) // 2)
        y_main = top_y - m_bbox[1]
        y_sub = top_y + mh + gap - s_bbox[1]

        def _x_for(tw: int) -> int:
            if align == "left":
                return pad_x
            if align == "right":
                return max(pad_x, w - pad_x - tw)
            return max(2, (w - tw) // 2)

        _draw_tracked_text(draw, _x_for(mw), y_main, display_text, f_main,
                           fill=fg, tracking=tracking, stroke=min(1, bold_stroke))
        _draw_tracked_text(draw, _x_for(sw), y_sub, subtext, f_sub, fill=fg, tracking=0, stroke=0)
    else:
        size = max(12, min(30, int(font_size)))
        font = _load_font(font_style, size)
        # Auto-shrink slightly if text overflows inner width.
        tw, th = _text_width(draw, display_text, font, tracking)
        while tw > inner_w and size > 12:
            size -= 1
            font = _load_font(font_style, size)
            tw, th = _text_width(draw, display_text, font, tracking)
        bbox = draw.textbbox((0, 0), display_text or "A", font=font)
        top_offset = bbox[1]
        if align == "left":
            x = pad_x
        elif align == "right":
            x = max(pad_x, w - pad_x - tw)
        else:
            x = max(2, (w - tw) // 2)
        y = int(round((h - th) / 2 - top_offset))
        _draw_tracked_text(draw, x, y, display_text, font,
                           fill=fg, tracking=tracking, stroke=bold_stroke)

    # Border / animated decoration overlay
    if motion == "pulse":
        if phase % 2 == 0:
            _draw_border(draw, border if border != "none" else "frame", phase=phase, inverted=invert)
    elif motion == "bars":
        _draw_border(draw, "bars", phase=phase, inverted=invert)
        if border not in ("none", "bars"):
            _draw_border(draw, border, phase=phase, inverted=invert)
    else:
        _draw_border(draw, border, phase=phase, inverted=invert)

    mono = Image.new("1", (w, h))
    mono.putdata([1 if px >= 128 else 0 for px in canvas.getdata()])
    return mono


def render(
    text: str,
    *,
    subtext: str = "",
    font_style: str = "bold",
    font_size: int = 22,
    bold_stroke: int = 0,
    tracking: int = 0,
    align: str = "center",
    border: str = "none",
    invert: bool = False,
    motion: str = "static",
    delay_ms: int = 320,
) -> bitmap.Asset:
    """Render a custom text billboard into a 1-frame static or 4-frame animated Asset."""
    clean_text = (text or "").strip()
    clean_sub = (subtext or "").strip()
    if not clean_text and not clean_sub:
        raise bitmap.AssetError("请输入至少一行招牌文字")
    if font_style not in _FONT_STACKS:
        font_style = "bold"
    if align not in ALIGNMENTS:
        align = "center"
    if border not in BORDERS:
        border = "none"
    if motion not in MOTION_MODES:
        motion = "static"

    stroke = max(0, min(2, int(bold_stroke)))
    track = max(-2, min(8, int(tracking)))
    delay = max(bitmap.MIN_DELAY_MS, min(2000, int(delay_ms)))
    frame_count = 1 if motion == "static" else 4

    frames = [
        _render_frame(
            clean_text or clean_sub,
            subtext=clean_sub if clean_text else "",
            font_style=font_style,
            font_size=int(font_size),
            bold_stroke=stroke,
            tracking=track,
            align=align,
            border=border,
            invert=bool(invert),
            motion=motion,
            phase=phase,
        )
        for phase in range(frame_count)
    ]
    delays = [delay] * frame_count
    title = (clean_text or clean_sub)[:24]
    return bitmap.Asset(frames=frames, delays=delays, name=f"招牌·{title}", source_frames=frame_count)
