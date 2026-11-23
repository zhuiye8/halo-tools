"""Sample a previously discovered memory address group to confirm it behaves like a
playback cursor (advances ~1:1 with wall clock and never goes backwards).

Runs the same discovery pass as mem_position_scan.py, then polls the best
candidate for a few seconds.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

import numpy as np

from mem_position_scan import (
    MEMORY_BASIC_INFORMATION,
    kernel32,
    read_doubles,
    regions,
    cloudmusic_pids,
    PROCESS_QUERY_INFORMATION,
    PROCESS_VM_READ,
)


def discover(handle: int, interval: float = 1.0) -> list[int]:
    areas = regions(handle)
    first = []
    for area in areas:
        values = read_doubles(handle, area)
        if values is None or values.size == 0:
            continue
        mask = (values > 0.0) & (values < 900.0)
        if mask.any():
            first.append((area, np.nonzero(mask)[0], values[mask]))
    time.sleep(interval)
    survivors = {}
    for area, offsets, before in first:
        values = read_doubles(handle, area)
        if values is None:
            continue
        after = values[offsets]
        with np.errstate(invalid="ignore"):
            diff = after - before
        mask = (np.abs(diff - interval) < 0.3 * max(1.0, interval)) & (after < 900.0)
        for offset, delta in zip(offsets[mask], diff[mask]):
            survivors[area.base + int(offset) * 8] = float(delta)
    print(f"candidates: {len(survivors)}")
    return list(survivors)[:8]


def sample(handle: int, address: int, seconds: int = 8) -> None:
    buffer = ctypes.create_string_buffer(8)
    got = ctypes.c_size_t(0)
    start = time.monotonic()
    print(f"sampling 0x{address:012X}")
    while time.monotonic() - start < seconds:
        if kernel32.ReadProcessMemory(handle, ctypes.c_void_p(address), buffer, 8,
                                      ctypes.byref(got)):
            value = float(np.frombuffer(buffer.raw, dtype=np.float64)[0])
            print(f"  t={time.monotonic() - start:5.2f}s  value={value:9.3f}")
        else:
            print("  read failed")
        time.sleep(1.0)


def main() -> None:
    pid = cloudmusic_pids()[0]
    handle = kernel32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    try:
        addresses = discover(handle)
        if addresses:
            sample(handle, addresses[0])
    finally:
        kernel32.CloseHandle(handle)


if __name__ == "__main__":
    main()