"""Feasibility probe: can we locate the playback position inside cloudmusic.exe?

Read-only.  Strategy: walk committed private/writable regions, decode them as
float64, and keep addresses whose value increases by ~1.0 per second while
staying inside a plausible song duration.  A real playback cursor survives
several passes; application uptime counters usually exceed the duration filter.

Usage:  python mem_position_scan.py [seconds_between_reads]
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import time
from dataclasses import dataclass

import numpy as np
import psutil

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE_READWRITE = 0x40


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.VirtualQueryEx.restype = ctypes.c_size_t
kernel32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p,
                                    ctypes.POINTER(MEMORY_BASIC_INFORMATION),
                                    ctypes.c_size_t]
kernel32.ReadProcessMemory.restype = wt.BOOL
kernel32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]


@dataclass(frozen=True)
class Region:
    base: int
    size: int


def cloudmusic_pids() -> list[int]:
    return [proc.pid for proc in psutil.process_iter(["pid", "name"])
            if (proc.info["name"] or "").lower() == "cloudmusic.exe"]


def regions(handle: int, limit_mb: int = 1024) -> list[Region]:
    found: list[Region] = []
    address = 0
    info = MEMORY_BASIC_INFORMATION()
    while kernel32.VirtualQueryEx(handle, ctypes.c_void_p(address), ctypes.byref(info),
                                  ctypes.sizeof(info)):
        base = info.BaseAddress or 0
        size = info.RegionSize or 0
        writable = info.Protect in (PAGE_READWRITE, PAGE_WRITECOPY, PAGE_EXECUTE_READWRITE)
        if (info.State == MEM_COMMIT and writable
                and not (info.Protect & PAGE_GUARD)
                and not (info.Protect & PAGE_NOACCESS)
                and 0 < size <= limit_mb * 1024 * 1024):
            found.append(Region(base, size))
        address = base + size
        if address <= 0:
            break
    return found


def read_doubles(handle: int, region: Region) -> np.ndarray | None:
    buffer = ctypes.create_string_buffer(region.size)
    got = ctypes.c_size_t(0)
    if not kernel32.ReadProcessMemory(handle, ctypes.c_void_p(region.base), buffer,
                                      region.size, ctypes.byref(got)):
        return None
    usable = (got.value // 8) * 8
    if usable <= 0:
        return None
    return np.frombuffer(buffer.raw[:usable], dtype=np.float64).copy()


def main() -> int:
    interval = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
    pids = cloudmusic_pids()
    if not pids:
        print("cloudmusic.exe is not running")
        return 1
    pid = pids[0]
    print(f"scanning pid {pid}")
    handle = kernel32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not handle:
        print(f"OpenProcess failed: {ctypes.get_last_error()}")
        return 1

    try:
        areas = regions(handle)
        total = sum(area.size for area in areas)
        print(f"regions: {len(areas)} ({total / 1024 / 1024:.0f} MB)")

        # First pass: remember per region only offsets that look like a position.
        first: list[tuple[Region, np.ndarray, np.ndarray]] = []
        for area in areas:
            values = read_doubles(handle, area)
            if values is None or values.size == 0:
                continue
            mask = (values > 0.0) & (values < 900.0)
            if not mask.any():
                continue
            first.append((area, np.nonzero(mask)[0].copy(), values[mask].copy()))
        print(f"regions with plausible values: {len(first)}")

        time.sleep(interval)

        survivors: dict[int, tuple[float, float, float]] = {}
        for area, offsets, before in first:
            values = read_doubles(handle, area)
            if values is None:
                continue
            after = values[offsets]
            diff = after - before
            mask = (np.abs(diff - interval) < 0.25 * max(1.0, interval)) & (after < 900.0)
            for offset, value_after, delta in zip(offsets[mask], after[mask], diff[mask]):
                survivors[area.base + int(offset) * 8] = (float(value_after - delta),
                                                          float(value_after), float(delta))
        print(f"first-pass candidates: {len(survivors)}")

        # Second pass: keep only addresses that keep advancing at ~1x.
        time.sleep(interval)
        confirmed: list[tuple[int, float, float, float]] = []
        for address, (_, _, _) in list(survivors.items()):
            buffer = ctypes.create_string_buffer(8)
            got = ctypes.c_size_t(0)
            if not kernel32.ReadProcessMemory(handle, ctypes.c_void_p(address), buffer, 8,
                                              ctypes.byref(got)):
                continue
            later = float(np.frombuffer(buffer.raw, dtype=np.float64)[0])
            start, middle, _ = survivors[address]
            if abs((later - middle) - interval) < 0.25 * max(1.0, interval) and later < 900.0:
                confirmed.append((address, start, middle, later))

        print(f"confirmed candidates: {len(confirmed)}")
        for address, start, middle, later in confirmed[:40]:
            print(f"  0x{address:012X}  {start:9.3f} -> {middle:9.3f} -> {later:9.3f}")
        return 0
    finally:
        kernel32.CloseHandle(handle)


if __name__ == "__main__":
    raise SystemExit(main())