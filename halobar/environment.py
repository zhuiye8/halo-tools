"""Environment detection: NetEase install, BetterNCM, 6K Labs endpoint, TempoHub.

The console uses this to tell the user exactly what is missing instead of showing a
vague "no progress" state.

Important: probing involves HTTP timeouts and a ``tasklist`` subprocess, so it runs
in a background thread and the request path only ever reads a cached snapshot.
Doing this work inside the request handler (as an earlier version did) could block
a request forever and let the thread-per-request server pile up thousands of
threads.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass, field
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
    links: dict = field(default_factory=lambda: dict(LINKS))

    @property
    def ready(self) -> bool:
        return self.sixk

    def as_dict(self) -> dict:
        data = asdict(self)
        data["links"] = dict(LINKS)
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


def sixk_available(timeout: float = 0.5) -> bool:
    try:
        with urllib.request.urlopen(_SIXK_ENDPOINT, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        return isinstance(payload, dict) and "player" in payload
    except (OSError, ValueError):
        return False


def tempo_hub_running(timeout: float = 3.0) -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/fi", "imagename eq EDIFIER TempoHub.exe", "/fo", "csv", "/nh"],
            capture_output=True, text=True, creationflags=_CREATE_NO_WINDOW, timeout=timeout)
        return "EDIFIER TempoHub" in (result.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return False


def probe() -> Environment:
    """Blocking probe; only ever called from the monitor's background thread."""
    exe = netease_path()
    return Environment(
        netease_path=str(exe) if exe else "",
        netease_exe=bool(exe),
        betterncm=bool(exe and (exe.parent / "msimg32.dll").exists())
        or _BETTERNCM_DATA.exists(),
        sixk=sixk_available(),
        tempo_hub=tempo_hub_running(),
        checked_at=time.time(),
    )


class EnvironmentMonitor:
    """Cached, non-blocking environment probe.

    ``get()`` returns the last snapshot immediately; ``start()`` refreshes it in the
    background (immediately, then every ``ttl`` seconds).
    """

    def __init__(self, ttl: float = 30.0) -> None:
        self.ttl = ttl
        self._snapshot = Environment()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="halobar-env", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(1.0)

    def refresh_now(self) -> None:
        try:
            self._snapshot = probe()
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            log.debug("environment probe failed: %s", exc)

    def _run(self) -> None:
        self.refresh_now()
        while not self._stop.wait(max(1.0, self.ttl)):
            self.refresh_now()

    def get(self) -> Environment:
        return self._snapshot