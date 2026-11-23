"""Probe the device's dedicated native lyric text slot (source 0x0E) and animation presets."""
import sys
import time

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
from hid_probe import Device, find_pixelbar  # noqa: E402
from write_test import ack_text, send_uimodel  # noqa: E402


def send_layout(dev, rgb, payload_tail):
    payload = bytes([1, rgb[0], rgb[1], rgb[2]]) + bytes(payload_tail)
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
        print("0xEE state    :", state.hex(" "), f"({len(state)} bytes)")
        rgb = (state[1], state[2], state[3])

        for source, label in ((0, "custom"), (0x0E, "lyric ")):
            r = dev.request(0xE7, bytes([source]))
            print(f"0xE7 src={source:#04x} {label}: {r.hex(' ') if r else None}")
            if r and len(r) >= 4:
                print(f"   max={r[1]} pkg={r[2]} len={r[3]} text={r[4:4+r[3]].decode('utf-8','replace')!r}")

        # animation presets on the lyric overlay slot
        for preset in range(0, 5):
            send_layout(dev, rgb, [0x00, 0x00, 0x01, 0x00, preset])
            time.sleep(0.15)
            st = dev.request(0xEE)
            print(f"preset {preset}: state={st.hex(' ')}")

        text = "夜空中最亮的星"
        raw = text.encode("utf-8")
        dev.send(0xE8, bytes([0x0E, len(raw)]) + raw)
        print("lyric write ack:", ack_text(dev))
        time.sleep(0.3)
        r = dev.request(0xE7, bytes([0x0E]))
        print("lyric readback:", r.hex(" ") if r else None)
        print("state now     :", dev.request(0xEE).hex(" "))

        # restore: clear lyric slot, disable lyric overlay, restore UI model
        dev.send(0xE8, bytes([0x0E, 0]))
        ack_text(dev)
        send_layout(dev, rgb, [0x00, 0x00, 0x00, 0xFF, 0xFF])
        time.sleep(0.4)
        send_uimodel(dev, rgb, state[6])
        time.sleep(0.6)
        print("restored state:", dev.request(0xEE).hex(" "))
    finally:
        dev.close()


if __name__ == "__main__":
    main()