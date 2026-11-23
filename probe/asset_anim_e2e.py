"""Upload a small 3-frame GIF through the console API and verify the scene switch."""

from __future__ import annotations

import base64
import io
import json
import sys
import time
import urllib.request

from PIL import Image, ImageDraw, ImageFont

PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8765
TOKEN = sys.argv[1] if len(sys.argv) > 1 else ""


def request(path: str, payload: dict | None = None) -> dict:
    url = f"http://127.0.0.1:{PORT}{path}?token={TOKEN}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def make_gif() -> bytes:
    frames = []
    font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 18)
    for index in range(3):
        image = Image.new("RGB", (256, 32), (0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.text((20 + index * 40, 6), "动" * (index + 1), font=font, fill=(255, 255, 255))
        draw.rectangle([0, 0, 255, 31], outline=(255, 255, 255))
        frames.append(image)
    buffer = io.BytesIO()
    frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:],
                   duration=200, loop=0)
    return buffer.getvalue()


def main() -> int:
    raw = make_gif()
    result = request("/api/asset/upload", {
        "filename": "anim.gif",
        "data": base64.b64encode(raw).decode(),
        "fit": "contain", "threshold": 128, "invert": False, "dither": False,
        "as_standby": True,
    })
    print("upload:", {key: value for key, value in result.items() if key != "preview"})
    for _ in range(80):
        status = request("/api/status")
        if status["upload_state"] in ("done", "error"):
            print("state:", status["upload_state"], "asset:", status["asset_name"],
                  status["asset_category"], status["asset_option"])
            return 0 if status["upload_state"] == "done" else 1
        time.sleep(0.5)
    print("upload did not finish")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())