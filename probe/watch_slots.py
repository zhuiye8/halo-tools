"""Poll the device text slots (custom / lyric) and screen state to observe writes from other apps.

Usage: python watch_slots.py [seconds] [interval_ms]

Useful to confirm whether EDIFIER TempoHub (or anything else) is driving the lyric slot
(source 0x0E) on this device.
"""
import sys
import time

from hid_probe import Device, find_pixelbar


def find_target():
    for path, caps in find_pixelbar():
        if caps.UsagePage == 0xFF14 and caps.InputReportByteLength == 64:
            return path
    return None


def main():
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    interval = float(sys.argv[2]) / 1000.0 if len(sys.argv) > 2 else 0.2

    path = find_target()
    if not path:
        print("device not found on USB")
        return 1

    dev = Device(path)
    started = time.monotonic()
    last = {}
    try:
        while time.monotonic() - started < duration:
            now = time.monotonic() - started
            for source, label in ((0, "custom"), (0x0E, "lyric ")):
                reply = dev.request(0xE7, bytes([source]))
                text = reply[4:4 + reply[3]].decode("utf-8", "replace") if reply else None
                if last.get(label) != text:
                    last[label] = text
                    print(f"{now:6.2f}s {label} -> {text!r}")
            state = dev.request(0xEE)
            if state and last.get("state") != state:
                last["state"] = state
                print(f"{now:6.2f}s state   -> {state.hex(' ')}")
            time.sleep(interval)
    finally:
        dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())