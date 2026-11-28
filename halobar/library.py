"""Local pixel asset library stored under ``%LOCALAPPDATA%\\halobar\\assets``.

Stores pre-encoded device payloads (``.bin``) alongside crisp 1bpp preview PNGs
and lightweight JSON metadata so users can switch between favourite static
images, custom billboards and multi-frame GIF animations in one click without
re-importing or re-tuning parameters.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import bitmap, settings

log = logging.getLogger(__name__)

MAX_LIBRARY_ITEMS = 60


@dataclass
class LibraryItem:
    id: str
    name: str
    frames: int
    animated: bool
    category: int
    option: int
    bytes: int
    delays: list[int]
    kind: str = "image"          # image | billboard | preset
    created_at: float = 0.0

    def to_public(self, preview_b64: str = "") -> dict:
        data = asdict(self)
        data["preview"] = f"data:image/png;base64,{preview_b64}" if preview_b64 else ""
        return data


class AssetLibrary:
    """Thread-safe persistent library of encoded 256x32 pixel assets."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else settings.asset_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        self._index_path = self.root / "index.json"
        self._lock = threading.RLock()
        self._items: list[LibraryItem] = []
        self._load()

    # ------------------------------------------------------------------ storage
    def _load(self) -> None:
        with self._lock:
            if not self._index_path.exists():
                self._items = []
                return
            try:
                raw = json.loads(self._index_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                log.warning("asset library index unreadable (%s); resetting", exc)
                self._items = []
                return
            items: list[LibraryItem] = []
            for entry in raw if isinstance(raw, list) else []:
                try:
                    item = LibraryItem(
                        id=str(entry["id"]),
                        name=str(entry["name"]),
                        frames=int(entry["frames"]),
                        animated=bool(entry["animated"]),
                        category=int(entry["category"]),
                        option=int(entry["option"]),
                        bytes=int(entry["bytes"]),
                        delays=[int(d) for d in entry.get("delays", [])],
                        kind=str(entry.get("kind", "image")),
                        created_at=float(entry.get("created_at", 0.0)),
                    )
                    if self._bin_path(item.id).exists():
                        items.append(item)
                except (KeyError, TypeError, ValueError):
                    continue
            self._items = items

    def _save_index(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._index_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps([asdict(item) for item in self._items], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._index_path)

    def _bin_path(self, item_id: str) -> Path:
        return self.root / f"{item_id}.bin"

    def _preview_path(self, item_id: str) -> Path:
        return self.root / f"{item_id}.png"

    # ------------------------------------------------------------------- public
    def list_items(self) -> list[dict]:
        with self._lock:
            result = []
            for item in self._items:
                preview_b64 = ""
                p = self._preview_path(item.id)
                if p.exists():
                    try:
                        preview_b64 = base64.b64encode(p.read_bytes()).decode("ascii")
                    except OSError:
                        pass
                result.append(item.to_public(preview_b64))
            return result

    def save_asset(self, asset: bitmap.Asset, stream: bytes, *,
                   name: str | None = None, kind: str = "image") -> dict:
        clean_name = (name or asset.name or "未命名素材").strip()[:48] or "未命名素材"
        item_id = secrets.token_hex(6)
        preview_bytes = bitmap.preview_png(asset.frames[0], scale=2)
        item = LibraryItem(
            id=item_id,
            name=clean_name,
            frames=len(asset.frames),
            animated=asset.animated,
            category=asset.category,
            option=asset.option,
            bytes=len(stream),
            delays=list(asset.delays),
            kind=kind,
            created_at=time.time(),
        )
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            self._bin_path(item_id).write_bytes(stream)
            self._preview_path(item_id).write_bytes(preview_bytes)
            self._items.insert(0, item)
            while len(self._items) > MAX_LIBRARY_ITEMS:
                dropped = self._items.pop()
                self._remove_files(dropped.id)
            self._save_index()
        return item.to_public(base64.b64encode(preview_bytes).decode("ascii"))

    def load_payload(self, item_id: str) -> tuple[LibraryItem, bytes]:
        with self._lock:
            item = next((x for x in self._items if x.id == item_id), None)
            if item is None:
                raise KeyError("素材不存在")
            path = self._bin_path(item.id)
            if not path.exists():
                raise KeyError("素材文件已丢失")
            return item, path.read_bytes()

    def rename(self, item_id: str, new_name: str) -> dict:
        clean = (new_name or "").strip()[:48]
        if not clean:
            raise ValueError("名称不能为空")
        with self._lock:
            item = next((x for x in self._items if x.id == item_id), None)
            if item is None:
                raise KeyError("素材不存在")
            item.name = clean
            self._save_index()
            p = self._preview_path(item.id)
            preview_b64 = base64.b64encode(p.read_bytes()).decode("ascii") if p.exists() else ""
            return item.to_public(preview_b64)

    def delete(self, item_id: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [x for x in self._items if x.id != item_id]
            if len(self._items) == before:
                return False
            self._remove_files(item_id)
            self._save_index()
            return True

    def _remove_files(self, item_id: str) -> None:
        for path in (self._bin_path(item_id), self._preview_path(item_id)):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


# --------------------------------------------------------------- built-in presets
def builtin_presets() -> list[dict]:
    """Metadata for zero-dependency procedural pixel presets."""
    return [
        {"id": "eq-wave", "name": "声波律动", "desc": "6 帧平滑频谱动画", "frames": 6, "animated": True},
        {"id": "cyber-pulse", "name": "赛博脉冲", "desc": "4 帧双向扫描光带", "frames": 4, "animated": True},
        {"id": "starfield", "name": "像素星轨", "desc": "6 帧微光星空动画", "frames": 6, "animated": True},
        {"id": "halo-badge", "name": "HALO 铭牌", "desc": "静态双线圆角招牌", "frames": 1, "animated": False},
    ]


def build_preset(preset_id: str) -> bitmap.Asset:
    """Procedurally generate a crisp 256x32 1bpp preset Asset."""
    from PIL import Image, ImageDraw
    from . import billboard

    w, h = bitmap.WIDTH, bitmap.HEIGHT
    if preset_id == "halo-badge":
        asset = billboard.render(
            "HALO PIXELBAR",
            font_style="bold",
            font_size=20,
            bold_stroke=1,
            tracking=1,
            border="corners",
            motion="static",
        )
        asset.name = "HALO 铭牌"
        return asset

    if preset_id == "eq-wave":
        frames = []
        for f in range(6):
            img = Image.new("1", (w, h), 0)
            draw = ImageDraw.Draw(img)
            for i in range(30):
                amp = 0.5 + 0.5 * math.sin(i * 0.42 + f * (math.tau / 6))
                bh = max(4, int(round(4 + 22 * amp)))
                x = 10 + i * 8
                y0 = (h - bh) // 2
                draw.rectangle((x, y0, x + 4, y0 + bh), fill=1)
            frames.append(img)
        return bitmap.Asset(frames=frames, delays=[140] * 6, name="声波律动", source_frames=6)

    if preset_id == "cyber-pulse":
        frames = []
        for f in range(4):
            img = Image.new("1", (w, h), 0)
            draw = ImageDraw.Draw(img)
            draw.rounded_rectangle((1, 1, w - 2, h - 2), radius=4, outline=1, width=1)
            cx = 128
            span = 26 + f * 30
            draw.rectangle((cx - span, 14, cx + span, 17), fill=1)
            for offset in (-span - 10, span + 6):
                if 6 < cx + offset < w - 10:
                    draw.rectangle((cx + offset, 10, cx + offset + 3, 21), fill=1)
            frames.append(img)
        return bitmap.Asset(frames=frames, delays=[160] * 4, name="赛博脉冲", source_frames=4)

    if preset_id == "starfield":
        stars = [
            (18, 6), (44, 22), (71, 10), (96, 25), (124, 8), (149, 19),
            (176, 7), (204, 23), (229, 12), (242, 26), (31, 14), (112, 16),
            (163, 26), (190, 14), (218, 6), (60, 27), (138, 13), (84, 5),
        ]
        frames = []
        for f in range(6):
            img = Image.new("1", (w, h), 0)
            draw = ImageDraw.Draw(img)
            for idx, (sx, sy) in enumerate(stars):
                phase = (f + idx) % 6
                if phase in (0, 1):
                    draw.point((sx, sy), fill=1)
                elif phase in (2, 3):
                    draw.line((sx - 2, sy, sx + 2, sy), fill=1)
                    draw.line((sx, sy - 2, sx, sy + 2), fill=1)
                elif phase == 4:
                    draw.line((sx - 3, sy, sx + 3, sy), fill=1)
                    draw.line((sx, sy - 3, sx, sy + 3), fill=1)
                    draw.point((sx - 1, sy - 1), fill=1)
                    draw.point((sx + 1, sy + 1), fill=1)
            frames.append(img)
        return bitmap.Asset(frames=frames, delays=[180] * 6, name="像素星轨", source_frames=6)

    raise KeyError(f"未知内置预设：{preset_id}")
