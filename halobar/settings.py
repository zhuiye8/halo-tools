"""Persistent user settings and well-known data locations.

Everything the app writes lives under ``%LOCALAPPDATA%\\halobar`` so the program
directory can be a read-only, portable folder:

* ``settings.json``  - this file
* ``logs/``          - rotating log files
* ``cache/lyrics/``  - downloaded lyric payloads
* ``assets/``        - original images uploaded to the speaker
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)

APP_NAME = "halobar"
SETTINGS_VERSION = 1


def app_dir() -> Path:
    """Return the per-user data directory (created on demand)."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_path() -> Path:
    return app_dir() / "settings.json"


def log_dir() -> Path:
    path = app_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    path = app_dir() / "cache" / "lyrics"
    path.mkdir(parents=True, exist_ok=True)
    return path


def asset_dir() -> Path:
    path = app_dir() / "assets"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Settings:
    """User-visible configuration, persisted as JSON."""

    version: int = SETTINGS_VERSION
    token: str = ""
    console_port: int = 8765
    open_console_on_start: bool = True
    create_shortcut_done: bool = False

    align: str = "center"
    animation: int | None = None
    scroll_long_lines: bool = True
    line_mode: str = "page"
    lyric_lang: str = "original"       # original | translation | bilingual
    theme: str = "system"              # system | light | dark
    color: list[int] | None = None     # [R, G, B] or None to follow device
    color_calibration: bool = True     # sRGB -> linear PWM gamma correction for LEDs
    auto_check_updates: bool = True
    update_proxy: str = "auto"
    standby_after: float = 20.0
    standby_scene: int | None = 8
    standby_scene_option: int = 0

    last_asset: dict = field(default_factory=dict)
    track_offsets: dict[str, float] = field(default_factory=dict)
    track_song_ids: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        target = path or settings_path()
        if not target.exists():
            settings = cls()
            settings.ensure_token()
            settings.save(target)
            return settings
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("settings unreadable (%s); using defaults", exc)
            try:
                target.replace(target.with_suffix(".corrupt.json"))
            except OSError:
                pass
            settings = cls()
            settings.ensure_token()
            settings.save(target)
            return settings

        known = {item.name for item in fields(cls)}
        cleaned = {key: value for key, value in data.items() if key in known}
        settings = cls(**cleaned)
        settings.version = SETTINGS_VERSION
        settings.ensure_token()
        return settings

    # ------------------------------------------------------------------- saving
    def save(self, path: Path | None = None) -> None:
        target = path or settings_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        try:
            temporary.write_text(
                json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(target)
        except OSError as exc:
            log.warning("could not save settings: %s", exc)

    def ensure_token(self) -> str:
        if not self.token:
            self.token = secrets.token_urlsafe(16)
        return self.token

    def console_url(self, port: int | None = None) -> str:
        return f"http://127.0.0.1:{port or self.console_port}/?token={self.token}"

    def apply_align(self, value: str | None) -> None:
        if value in ("left", "center", "right", "justify"):
            self.align = value

    def apply_animation(self, value: int | None) -> None:
        if value is None or 0 <= int(value) <= 4:
            self.animation = None if value is None else int(value)

    def apply_lyric_lang(self, value: str | None) -> None:
        if value in ("original", "translation", "bilingual"):
            self.lyric_lang = value

    def apply_theme(self, value: str | None) -> None:
        if value in ("system", "light", "dark"):
            self.theme = value

    def apply_color(self, value: list[int] | tuple[int, int, int] | None) -> None:
        if value is None:
            self.color = None
            return
        if len(value) == 3:
            self.color = [max(0, min(255, int(c))) for c in value]

    def get_track_offset(self, track_key: str) -> float:
        return float(self.track_offsets.get(track_key, 0.0))

    def set_track_offset(self, track_key: str, offset: float) -> None:
        if not track_key:
            return
        rounded = round(float(offset), 2)
        if abs(rounded) < 0.05:
            self.track_offsets.pop(track_key, None)
        else:
            self.track_offsets[track_key] = rounded
            self._trim_dict(self.track_offsets, 200)

    def get_track_song_id(self, track_key: str) -> str:
        return str(self.track_song_ids.get(track_key, ""))

    def set_track_song_id(self, track_key: str, song_id: str) -> None:
        if not track_key:
            return
        if not song_id:
            self.track_song_ids.pop(track_key, None)
        else:
            self.track_song_ids[track_key] = str(song_id)
            self._trim_dict(self.track_song_ids, 200)

    @staticmethod
    def _trim_dict(mapping: dict, limit: int) -> None:
        while len(mapping) > limit:
            oldest = next(iter(mapping))
            mapping.pop(oldest, None)