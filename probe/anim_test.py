"""Focused animation upload test (2 frames, then 8 frames) with timing, then restore."""
import sys
import time

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
from hid_probe import Device, find_pixelbar  # noqa: E402
from write_test import send_uimodel  # noqa: E402
from upload_test import encode_animation, make_text_image, upload  # noqa: E402


def main():
    path = [p for p, c in find_pixelbar() if c.UsagePage == 0xFF14 and c.InputReportByteLength == 64][0]
    dev = Device(path)
    font = r"C:\Windows\Fonts\msyh.ttc"
    try:
        original = dev.request(0xEE)
        print("start state:", original.hex(" "))

        for count, size in ((2, 20), (8, 18)):
            frames = [make_text_image("动画 " + "●" * (i + 1), font, size) for i in range(count)]
            delays = [200] * count
            t0 = time.monotonic()
            ok, ms, cur, _ = upload(dev, encode_animation(frames, delays), 6, 52)
            print(f"{count} frames -> ok={ok} upload={ms:.0f}ms wall={1000*(time.monotonic()-t0):.0f}ms state={cur.hex(' ')}")
            time.sleep(1)

        send_uimodel(dev, (original[1], original[2], original[3]), 8)
        time.sleep(1.0)
        print("restored:", dev.request(0xEE).hex(" "))
    finally:
        dev.close()


if __name__ == "__main__":
    main()