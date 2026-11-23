"""Inspect the NetEase client (CEF) window through UI Automation.

Goal: does the player seek bar expose a slider with a readable ValuePattern?  If
yes we could read the real playback position without injecting anything into the
client.

Run:  python uia_probe.py
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import subprocess

import uiautomation as auto

user32 = ctypes.WinDLL("user32", use_last_error=True)
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
VALUE_PROPERTY_ID = 0x00000015


def cloudmusic_pids() -> set[int]:
    out = subprocess.check_output(
        ["tasklist", "/fi", "imagename eq cloudmusic.exe", "/fo", "csv"],
        text=True, encoding="gbk", errors="ignore")
    pids: set[int] = set()
    for line in out.splitlines()[1:]:
        parts = [part.strip('"') for part in line.split('","')]
        if len(parts) > 1:
            try:
                pids.add(int(parts[1]))
            except ValueError:
                pass
    return pids


def windows_of(pids: set[int]) -> list[tuple[int, str, str, bool]]:
    found: list[tuple[int, str, str, bool]] = []

    def callback(hwnd, _):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            found.append((hwnd, cls.value, title.value, bool(user32.IsWindowVisible(hwnd))))
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


def walk(control, max_nodes: int = 6000) -> None:
    stack = [(control, 0)]
    seen = 0
    hits = 0
    while stack and seen < max_nodes:
        node, depth = stack.pop()
        seen += 1
        try:
            kind = node.ControlTypeName
            if kind in ("SliderControl", "ProgressBarControl"):
                hits += 1
                value = node.GetPropertyValue(VALUE_PROPERTY_ID)
                print(f"  [{kind}] name={node.Name!r} value={value!r} id={node.AutomationId!r}")
                try:
                    pattern = node.GetPattern(auto.PatternId.ValuePattern)
                    print(f"      ValuePattern -> {pattern.Value!r}")
                except Exception as exc:  # noqa: BLE001
                    print(f"      no ValuePattern ({exc})")
            if depth < 45:
                for child in node.GetChildren():
                    stack.append((child, depth + 1))
        except Exception:
            continue
    print(f"  walked {seen} controls, {hits} slider/progress hits")


def main() -> None:
    auto.SetGlobalSearchTimeout(2)
    pids = cloudmusic_pids()
    print(f"cloudmusic pids: {pids}")
    windows = windows_of(pids)
    print(f"top-level windows: {len(windows)}")
    visible = [w for w in windows if w[3]]
    for hwnd, cls, title, is_visible in visible or windows:
        print(f"WINDOW hwnd={hwnd} class={cls!r} title={title!r} visible={is_visible}")
        try:
            element = auto.ControlFromHandle(hwnd)
        except Exception as exc:  # noqa: BLE001
            print(f"  ControlFromHandle failed: {exc}")
            continue
        walk(element)


if __name__ == "__main__":
    main()