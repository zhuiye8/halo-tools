"""Upload a custom 256x32 bitmap to Halo PixelBar and verify it, then restore the UI scene.

Protocol verified against tflanfeng/OpenHaloPixelBar and woozyc/pixelbardiy notes:
  0xEE (state) -> 0x17 [0,0,<len:4>] -> device replies [0,1,<chunk:2>]
  -> 0x17 [0,2,<idx:2>,<data>] (ack [0,3] between chunks) -> 0x17 [0,4] -> wait [0,5]
  -> 0xEF [1, R,G,B, combo, 1, category, option, 255]
Static frames use category 3 / option 6; animations use category 6 / option 52.
"""
import sys
import time

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
from hid_probe import Device, find_pixelbar  # noqa: E402
from write_test import send_uimodel  # noqa: E402

W, H = 256, 32


def pixel_bytes(img):
    mono = img.convert("1")
    out = bytearray()
    for page in range(H // 8):
        for x in range(W):
            b = 0
            for bit in range(8):
                if mono.getpixel((x, page * 8 + bit)):
                    b |= 1 << bit
            out.append(b)
    return bytes(out)


def encode_static(img):
    px = pixel_bytes(img)
    return b"".join(
        bytes([3]) + i.to_bytes(2, "big") + b"\xff\xff" + px[i * 128:(i + 1) * 128]
        for i in range(8)
    )


def encode_animation(frames, delays, category=6):
    records = []
    for fi, (img, delay) in enumerate(zip(frames, delays)):
        px = pixel_bytes(img)
        for block in range(8):
            idx = fi * 8 + block
            records.append(
                bytes([category]) + idx.to_bytes(2, "big") + delay.to_bytes(2, "big")
                + px[block * 128:(block + 1) * 128]
            )
    return b"".join(records)


def receive(dev, command, timeout_ms=5000):
    deadline = time.monotonic() + timeout_ms / 1000.0
    import ctypes
    while time.monotonic() < deadline:
        buf = ctypes.create_string_buffer(64)
        n = dev._transfer(buf, True, 200)
        if n == 0:
            continue
        d = buf.raw[:n]
        if len(d) < 7 or d[:3] != b"\x2F\xBB\xEC":
            continue
        length = int.from_bytes(d[4:6], "big")
        if length > 57 or len(d) < 7 + length:
            continue
        if (sum(d[1:6 + length]) & 0xFF) != d[6 + length]:
            continue
        if d[3] == command:
            return d[6:6 + length]
    return None


def drain(dev):
    import ctypes
    for _ in range(64):
        buf = ctypes.create_string_buffer(64)
        if dev._transfer(buf, True, 5) == 0:
            break


def read_frame(dev, timeout_ms=3000):
    deadline = time.monotonic() + timeout_ms / 1000.0
    import ctypes
    while time.monotonic() < deadline:
        buf = ctypes.create_string_buffer(64)
        n = dev._transfer(buf, True, 200)
        if n == 0:
            continue
        d = buf.raw[:n]
        if len(d) < 7 or d[:3] != b"\x2F\xBB\xEC":
            continue
        length = int.from_bytes(d[4:6], "big")
        if length > 57 or len(d) < 7 + length:
            continue
        if (sum(d[1:6 + length]) & 0xFF) != d[6 + length]:
            continue
        if d[3] == 0x17:
            return d[6:6 + length]
    return None


def upload(dev, data, category, option, progress=None):
    state = dev.request(0xEE)
    if not state or len(state) < 9:
        raise RuntimeError("state query failed")
    drain(dev)
    dev.send(0x17, bytes([0, 0]) + len(data).to_bytes(4, "big"))
    ready = None
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        f = read_frame(dev)
        if f and f[:2] == b"\x00\x01":
            ready = f
            break
    if not ready:
        raise RuntimeError("upload request rejected")
    chunk = int.from_bytes(ready[2:4], "big")
    if not 1 <= chunk <= 53:
        raise RuntimeError(f"unexpected chunk size {chunk}")
    print(f"    device chunk size = {chunk}, total {len(data)} bytes")
    t0 = time.monotonic()
    index = 0
    for offset in range(0, len(data), chunk):
        dev.send(0x17, bytes([0, 2]) + index.to_bytes(2, "big") + data[offset:offset + chunk])
        index += 1
        if offset + chunk < len(data):
            ack = None
            end = time.monotonic() + 3
            while time.monotonic() < end:
                f = read_frame(dev)
                if not f:
                    continue
                if f[:2] == b"\x00\x01":
                    continue
                ack = f
                break
            if not ack or ack[:2] != b"\x00\x03":
                raise RuntimeError(f"chunk rejected at {offset}: {ack.hex(' ') if ack else None}")
        if progress:
            progress(min(offset + chunk, len(data)) / len(data))
    dev.send(0x17, bytes([0, 4]))
    final = False
    end = time.monotonic() + 8
    while time.monotonic() < end:
        f = read_frame(dev, 1000)
        if not f:
            break
        if f[:2] == b"\x00\x05":
            final = True
            break
    if not final:
        raise RuntimeError("no final upload confirmation")
    upload_ms = (time.monotonic() - t0) * 1000

    selection = bytes([1]) + state[1:5] + bytes([1, category, option, 255])
    dev.send(0xEF, selection)
    time.sleep(0.4)
    current = dev.request(0xEE)
    ok = bool(current and current[5:8] == bytes([1, category, option]))
    return ok, upload_ms, current, state


def make_text_image(text, fnt_path, size, invert=False):
    img = Image.new("1", (W, H), 0)
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(fnt_path, size)
    bbox = d.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    d.text(((W - tw) // 2 - bbox[0], (H - th) // 2 - bbox[1]), text, font=font, fill=1)
    if invert:
        img = img.point(lambda v: 0 if v else 255, mode="1") if False else Image.eval(img, lambda v: 255 - v)
    d.rectangle([0, 0, W - 1, H - 1], outline=1)
    return img


def main():
    path = None
    for p, caps in find_pixelbar():
        if caps.UsagePage == 0xFF14 and caps.InputReportByteLength == 64:
            path = p
    dev = Device(path)
    try:
        original = dev.request(0xEE)
        print("original state:", original.hex(" "))

        font = r"C:\Windows\Fonts\msyh.ttc"
        img = make_text_image("HALO 自定义点阵", font, 20)
        img.save("out_custom_static.png")
        ok, ms, cur, _ = upload(dev, encode_static(img), 3, 6)
        print(f"static upload: ok={ok} {ms:.0f} ms, state={cur.hex(' ')}")

        frames, delays = [], []
        for i in range(8):
            frames.append(make_text_image("自定义动画 " + ">" * i + " " * (8 - i), font, 20))
            delays.append(150)
        ok, ms, cur, _ = upload(dev, encode_animation(frames, delays), 6, 52)
        print(f"animation upload: ok={ok} {ms:.0f} ms, state={cur.hex(' ')}")

        time.sleep(0.5)
        send_uimodel(dev, (original[1], original[2], original[3]), original[6])
        time.sleep(0.8)
        print("restored state:", dev.request(0xEE).hex(" "))
    finally:
        dev.close()


if __name__ == "__main__":
    main()