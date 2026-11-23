"""Controlled write test for Halo PixelBar native text + layout, then restore.

Sends only reversible display commands:
  E8  native text (single packet)
  EF  layout / native UI model selection
Captures 0xEE state first and restores it at the end.
"""
import ctypes
import sys
import time

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
from hid_probe import Device, find_pixelbar  # noqa: E402


def ack_text(dev, timeout_ms=3000):
    """E8 has a dedicated 2F CC EC E8 00 01 01 ack frame."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        buf = ctypes.create_string_buffer(64)
        n = dev._transfer(buf, True, 200)
        if n == 0:
            continue
        d = buf.raw[:n]
        if d[:6] == b"\x2f\xcc\xec\xe8\x00\x01":
            sum_ = sum(d[1:7]) & 0xFF
            return ("ok" if d[7] == sum_ else "bad-checksum", d[6], d[:12].hex(" "))
    return ("timeout", None, "")


def send_layout(dev, rgb, category=2, sub=0, option=0):
    payload = bytes([1, rgb[0], rgb[1], rgb[2], 0x00, category,
                     0x00 if sub == 0 else 0x01, option, 0xFF])
    frame = bytes([0x2E, 0xAA, 0xEC, 0xEF, 0x00, len(payload)]) + payload
    acc = 124
    for b in payload:
        acc += b + 2
    frame += bytes([acc % 256, 0x00])
    return dev._transfer(frame.ljust(64, b"\x00"), False, 3000)


def send_uimodel(dev, rgb, model):
    payload = bytes([2, rgb[0], rgb[1], rgb[2], 0x00, 0x01, model, 0xFF, 0xFF])
    frame = bytes([0x2E, 0xAA, 0xEC, 0xEF, 0x00, len(payload)]) + payload
    acc = 124
    for b in payload:
        acc += b + 2
    frame += bytes([acc % 256, 0x00])
    return dev._transfer(frame.ljust(64, b"\x00"), False, 3000)


def main():
    path = None
    for p, caps in find_pixelbar():
        if caps.UsagePage == 0xFF14 and caps.InputReportByteLength == 64:
            path = p
    dev = Device(path)
    try:
        state = dev.request(0xEE)
        print("initial 0xEE :", state.hex(" "))
        rgb = (state[1], state[2], state[3])
        category, option = state[5], state[6]
        print(f"  rgb={rgb} category={category} option={option}")

        tests = [
            ("16 ascii", "ABCDEFGHIJKLMNOP"),
            ("16 cjk", "一二三四五六七八九十一二三四五六"),
            ("18 cjk", "一二三四五六七八九十一二三四五六七八"),
            ("20 cjk", "一二三四五六七八九十一二三四五六七八九十"),
            ("long line", "我曾经跨过山和大海也穿过人山人海"),
        ]
        for label, text in tests:
            raw = text.encode("utf-8")
            print(f"\n--- {label}: {len(text)} chars / {len(raw)} bytes ---")
            before = dev.request(0xE7, b"\x00")
            payload = bytes([0, len(raw)]) + raw
            total = 6 + len(payload) + 1
            if total > 64:
                print(f"  skipped: single packet needs {total} bytes > 64")
                continue
            dev.send(0xE8, payload)
            print("  ack:", ack_text(dev))
            after = dev.request(0xE7, b"\x00")
            print("  before:", before[4:].decode("utf-8", "replace") if before else None)
            print("  after :", after[4:].decode("utf-8", "replace") if after else None)

        print("\n--- layout: static center ---")
        send_layout(dev, rgb, category=2, sub=0, option=1)
        time.sleep(0.3)
        print("  state:", dev.request(0xEE).hex(" "))

        print("--- layout: scroll right-to-left ---")
        send_layout(dev, rgb, category=2, sub=1, option=1)
        time.sleep(0.3)
        print("  state:", dev.request(0xEE).hex(" "))

        print("\n--- restore original UI model ---")
        send_uimodel(dev, rgb, option)
        time.sleep(0.5)
        print("  state:", dev.request(0xEE).hex(" "))
    finally:
        dev.close()


if __name__ == "__main__":
    main()