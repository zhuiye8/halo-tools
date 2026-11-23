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
    auto_check_updates: bool = True
    update_proxy: str = "auto"
    standby_after: float = 20.0
    standby_scene: int | None = 8
    standby_scene_option: int = 0

    last_asset: dict = field(default_factory=dict)

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