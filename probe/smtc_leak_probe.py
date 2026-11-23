"""Press the SMTC reader in a tight loop and watch threads/memory.

Useful to prove (or rule out) WinRT as the source of the RPC-thread growth seen in
long-running packaged builds.

Usage: python probe/smtc_leak_probe.py [iterations] [interval_seconds]
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halobar import media  # noqa: E402

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)


class MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


psapi.GetProcessMemoryInfo.restype = wt.BOOL
psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(MEMORY_COUNTERS), wt.DWORD]
kernel32.GetCurrentProcess.restype = wt.HANDLE


def rss_mb() -> float:
    counters = MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters),
                               counters.cb)
    return counters.WorkingSetSize / 1024 / 1024


async def main() -> None:
    iterations = int(sys.argv[1]) if len(sys.argv) > 1 else 600
    interval = float(sys.argv[2]) if len(sys.argv) > 2 else 0.1
    print(f"start rss {rss_mb():.1f} MB threads {threading.active_count()} pid {os.getpid()}")
    for index in range(iterations):
        try:
            await media.read_playback(use_sixk=False)
        except Exception as exc:  # noqa: BLE001
            if index % 50 == 0:
                print("  read failed:", type(exc).__name__, exc)
        await asyncio.sleep(interval)
        if index % 50 == 49:
            print(f"  iter {index + 1:4d}  rss {rss_mb():7.1f} MB  threads {threading.active_count()}")
    print(f"end   rss {rss_mb():.1f} MB  threads {threading.active_count()}")


if __name__ == "__main__":
    asyncio.run(main())