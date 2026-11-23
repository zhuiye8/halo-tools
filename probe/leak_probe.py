"""Watch thread/memory growth of the playback readers (SMTC and 6K Labs).

Usage: python probe/leak_probe.py [iterations] [interval]
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wt
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halobar import media, sixk  # noqa: E402

kernel32 = ctypes.WinDLL("kernel32")


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def rss_mb() -> float:
    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    kernel32.GetCurrentProcess()
    psapi = ctypes.WinDLL("psapi")
    psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters),
                               counters.cb)
    return counters.WorkingSetSize / 1024 / 1024


async def loop(iterations: int, interval: float) -> None:
    for index in range(iterations):
        try:
            await media.read_playback()
        except Exception as exc:  # noqa: BLE001
            if index % 20 == 0:
                print("  read failed:", exc)
        await asyncio.sleep(interval)
        if index % 30 == 29:
            print(f"  iter {index + 1:4d}  rss {rss_mb():7.1f} MB  threads {threading.active_count()}")


def main() -> int:
    iterations = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    interval = float(sys.argv[2]) if len(sys.argv) > 2 else 0.1
    print(f"start rss {rss_mb():.1f} MB  threads {threading.active_count()}")
    print("sixk reachable:", sixk.fetch(timeout=1) is not None)
    asyncio.run(loop(iterations, interval))
    print(f"end   rss {rss_mb():.1f} MB  threads {threading.active_count()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())