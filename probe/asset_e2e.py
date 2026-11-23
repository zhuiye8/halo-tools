"""End-to-end check of the asset upload path through the running console.

Usage: python probe/asset_e2e.py <console_token> [port]
Requires the service running with the console enabled; writes to the device.
"""

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
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode())


def make_image() -> bytes:
    image = Image.new("1", (256, 32), 0)
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 20)
    text = "halobar 素材"
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(((256 - (box[2] - box[0])) // 2 - box[0], (32 - (box[3] - box[1])) // 2 - box[1]),
              text, font=font, fill=1)
    draw.rectangle([0, 0, 255, 31], outline=1)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def main() -> int:
    raw = make_image()
    result = request("/api/asset/upload", {
        "filename": "e2e.png",
        "data": base64.b64encode(raw).decode(),
        "fit": "contain", "threshold": 128, "invert": False, "dither": False,
        "as_standby": True, "pause_after": True,
    })
    print("upload response:", {k: v for k, v in result.items() if k != "preview"})
    for _ in range(60):
        status = request("/api/status")
        if status["upload_state"] in ("done", "error"):
            print("upload_state:", status["upload_state"], "progress:",
                  round(status["upload_progress"], 2), "asset:",
                  status["asset_name"], status["asset_category"], status["asset_option"])
            if status["upload_state"] == "error":
                print("error:", status["last_error"])
                return 1
            break
        time.sleep(0.5)
    else:
        print("upload did not finish in time")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())