"""Desktop shortcut creation (no installer, no admin rights)."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_CREATE_NO_WINDOW = 0x08000000


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


@dataclass
class ShortcutTarget:
    executable: Path
    arguments: str
    working_directory: Path


def _desktop_path() -> Path:
    """Resolve the real Desktop folder (honours OneDrive redirection)."""
    try:
        guid = _GUID()
        ctypes.windll.ole32.CLSIDFromString(
            ctypes.c_wchar_p("{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"), ctypes.byref(guid))
        path_ptr = ctypes.c_wchar_p()
        result = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(path_ptr))
        if result == 0 and path_ptr.value:
            return Path(path_ptr.value)
    except Exception:  # noqa: BLE001 - fall back to the plain location
        pass
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"


def resolve_target() -> ShortcutTarget:
    """Packaged builds point at halobar.exe; source checkouts use pythonw -m halobar."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        return ShortcutTarget(exe, "", exe.parent)
    project_root = Path(__file__).resolve().parent.parent
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    executable = pythonw if pythonw.exists() else Path(sys.executable)
    return ShortcutTarget(executable, "-m halobar run", project_root)


def create_desktop_shortcut(name: str = "halobar",
                            description: str = "halobar · 花再 PixelBar 歌词工具") -> Path:
    """Create (or overwrite) ``<Desktop>\\<name>.lnk``."""
    target = resolve_target()
    link = _desktop_path() / f"{name}.lnk"
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$sc = $ws.CreateShortcut('{link}'); "
        f"$sc.TargetPath = '{target.executable}'; "
        f"$sc.Arguments = '{target.arguments}'; "
        f"$sc.WorkingDirectory = '{target.working_directory}'; "
        f"$sc.IconLocation = '{target.executable},0'; "
        f"$sc.Description = '{description}'; "
        "$sc.Save()"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, creationflags=_CREATE_NO_WINDOW, timeout=30)
    if result.returncode != 0 or not link.exists():
        raise OSError((result.stderr or "shortcut creation failed").strip())
    log.info("desktop shortcut created: %s -> %s %s", link, target.executable, target.arguments)
    return link


def shortcut_exists(name: str = "halobar") -> bool:
    return (_desktop_path() / f"{name}.lnk").exists()