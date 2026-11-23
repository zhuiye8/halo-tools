"""Environment detection: NetEase install, BetterNCM, 6K Labs endpoint, TempoHub.

The console uses this to tell the user exactly what is missing instead of showing a
vague "no progress" state.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_CREATE_NO_WINDOW = 0x08000000
_SIXK_ENDPOINT = "http://127.0.0.1:9863/query"
_BETTERNCM_DATA = Path("C:/betterncm")

LINKS = {
    "netease": "https://music.163.com/#/download",
    "betterncm": "https://github.com/std-microblock/BetterNCM-Installer/releases/latest",
    "inflink": "https://github.com/apoint123/inflink-rs/releases/latest",
    "sixk": "https://github.com/lgc2333/BetterNCM-6K-Labs/tree/dist",
}


@dataclass
class Environment:
    netease_path: str = ""
    netease_exe: bool = False
    betterncm: bool = False
    sixk: bool = False
    tempo_hub: bool = False
    checked_at: float = 0.0
    links: dict | None = None

    @property
    def ready(self) -> bool:
        return self.sixk

    def as_dict(self) -> dict:
        data = asdict(self)
        data["links"] = LINKS
        data["ready"] = self.ready
        return data


def _reg_query(default_path: Path | None = None) -> str:
    import winreg

    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(
                    root,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\cloudmusic.exe") as key:
                value, _ = winreg.QueryValueEx(key, "")
                if value:
                    return str(value)
        except OSError:
            continue
    return str(default_path) if default_path else ""


def netease_path() -> Path | None:
    exe = _reg_query()
    if exe:
        path = Path(exe)
        if path.exists():
            return path
    for candidate in (
            Path(r"C:\Program Files\Netease\CloudMusic\cloudmusic.exe"),
            Path(r"C:\Program Files (x86)\Netease\CloudMusic\cloudmusic.exe"),
            Path(r"C:\Apps\CloudMusic\cloudmusic.exe")):
        if candidate.exists():
            return candidate
    return None


def sixk_available(timeout: float = 0.6) -> bool:
    try:
        with urllib.request.urlopen(_SIXK_ENDPOINT, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        return isinstance(payload, dict) and "player" in payload
    except (OSError, ValueError):
        return False


def tempo_hub_running() -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/fi", "imagename eq EDIFIER TempoHub.exe", "/fo", "csv", "/nh"],
            capture_output=True, text=True, creationflags=_CREATE_NO_WINDOW, timeout=5)
        return "EDIFIER TempoHub" in (result.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return False


class EnvironmentMonitor:
    """Cached environment probe (the console polls status every ~700 ms)."""

    def __init__(self, ttl: float = 5.0) -> None:
        self.ttl = ttl
        self._lock = threading.Lock()
        self._cached: Environment | None = None

    def get(self) -> Environment:
        with self._lock:
            now = time.time()
            if self._cached and now - self._cached.checked_at < self.ttl:
                return self._cached
            exe = netease_path()
            env = Environment(
                netease_path=str(exe) if exe else "",
                netease_exe=bool(exe),
                betterncm=bool(exe and (exe.parent / "msimg32.dll").exists())
                or _BETTERNCM_DATA.exists(),
                sixk=sixk_available(),
                tempo_hub=tempo_hub_running(),
                checked_at=now,
            )
            self._cached = env
            return env