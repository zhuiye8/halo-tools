"""Pin down the real native-text byte limit, then restore the original UI state."""
import sys
import time

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
from hid_probe import Device, find_pixelbar  # noqa: E402
from write_test import ack_text, send_uimodel  # noqa: E402


def try_text(dev, text):
    raw = text.encode("utf-8")
    payload = bytes([0, len(raw)]) + raw
    total = 7 + len(payload)
    if total > 64:
        return None, f"packet would be {total} bytes (>64)"
    dev.send(0xE8, payload)
    ack = ack_text(dev)
    after = dev.request(0xE7, b"\x00")
    stored = after[4:] if after else b""
    return (ack, stored), None


def main():
    path = None
    for p, caps in find_pixelbar():
        if caps.UsagePage == 0xFF14 and caps.InputReportByteLength == 64:
            path = p
    dev = Device(path)
    try:
        state = dev.request(0xEE)
        rgb = (state[1], state[2], state[3])
        model = state[6]

        cases = [
            ("16 ascii", "A" * 16),
            ("24 ascii", "B" * 24),
            ("40 ascii", "C" * 40),
            ("50 ascii", "D" * 50),
            ("17 cjk", "一二三四五六七八九十一二三四五六七"),
            ("16 cjk+1", "一二三四五六七八九十一二三四五六八"),
        ]
        for label, text in cases:
            res, err = try_text(dev, text)
            if err:
                print(f"{label:10s} -> {err}")
                continue
            (ack, stored) = res
            print(f"{label:10s} sent={len(text.encode())}B ack={ack[0]} reply={ack[2]}")
            print(f"           stored_len={len(stored)}B stored={stored.decode('utf-8', 'replace')!r}"
                  f" valid_utf8={stored == text.encode()}")
            print(f"           raw={stored.hex(' ')}")
            time.sleep(0.4)

        send_uimodel(dev, rgb, model)
        time.sleep(0.5)
        print("restored:", dev.request(0xEE).hex(" "))
    finally:
        dev.close()


if __name__ == "__main__":
    main()