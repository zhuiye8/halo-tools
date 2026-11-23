"""Web console tests: auth, status, commands and settings (no hardware)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import urlparse

from halobar.server import ConsoleServer
from halobar.service import LyricsService, ServiceConfig


class FakeScreen:
    firmware = "1.1.1-fake"

    def show_text(self, text, **kwargs):
        return text

    def show_scene(self, category, option=0, template=0xFF):
        pass

    def snapshot(self):
        return {}

    def restore(self, snapshot):
        pass

    def close(self):
        pass


def make_service() -> LyricsService:
    async def reader(preferred=None, use_sixk=True):
        return None

    return LyricsService(
        ServiceConfig(poll_seconds=0.05),
        screen_factory=lambda: FakeScreen(),
        reader=reader,
        lyrics_client=type("L", (), {"lyrics_for": lambda self, *a, **k: __import__(
            "halobar.lyrics", fromlist=["Lyrics"]).Lyrics()} )(),
    )


def fetch(url: str, method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_console_requires_token_and_serves_status():
    service = make_service()
    console = ConsoleServer(service, port=0)
    url = console.start()
    try:
        parsed = urlparse(url)
        base = f"http://127.0.0.1:{console.port}"
        token = parsed.query.split("token=")[1]

        status, payload = fetch(f"{base}/api/status")
        assert status == 401

        status, payload = fetch(f"{base}/api/status?token={token}")
        assert status == 200
        assert "state" in payload and "settings" in payload

        page = urllib.request.urlopen(f"{base}/", timeout=5).read().decode()
        assert token in page and "halobar" in page and "正在播放" in page
    finally:
        console.stop()


def test_asset_upload_and_display():
    import base64
    import io

    pytest = __import__("pytest")
    pytest.importorskip("PIL")
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (64, 32), (255, 255, 255)).save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode()

    service = make_service()
    console = ConsoleServer(service, port=0)
    url = console.start()
    try:
        base = f"http://127.0.0.1:{console.port}"
        token = urlparse(url).query.split("token=")[1]

        status, payload = fetch(f"{base}/api/asset/upload?token={token}", "POST", {
            "filename": "white.png", "data": encoded, "fit": "contain",
            "threshold": 128, "invert": False, "dither": False, "as_standby": True,
        })
        assert status == 200, payload
        assert payload["frames"] == 1
        assert payload["category"] == 3
        assert payload["preview"].startswith("data:image/png;base64,")
        assert service.status().upload_state == "queued"

        status, payload = fetch(f"{base}/api/asset/upload?token={token}", "POST",
                                {"filename": "x.png", "data": "!!!notbase64!!!"})
        assert status == 400

        status, payload = fetch(f"{base}/api/asset/display?token={token}", "POST", {})
        assert status == 400 or status == 200
    finally:
        console.stop()


def test_console_commands_and_settings():
    service = make_service()
    console = ConsoleServer(service, port=0)
    url = console.start()
    try:
        base = f"http://127.0.0.1:{console.port}"
        token = urlparse(url).query.split("token=")[1]

        status, payload = fetch(f"{base}/api/command?token={token}", "POST", {"action": "pause"})
        assert status == 200 and payload["ok"]
        assert service.status().state == "paused"

        status, payload = fetch(f"{base}/api/command?token={token}", "POST",
                                {"action": "nudge", "value": -1.5})
        assert status == 200

        status, payload = fetch(f"{base}/api/settings?token={token}", "POST", {
            "align": "left", "animation": 3, "scroll_long_lines": False, "standby_after": 45,
        })
        assert status == 200, payload
        assert service.config.align.value == "left"
        assert service.config.animation == 3
        assert service.config.scroll_long_lines is False
        assert service.config.standby_after == 45

        status, payload = fetch(f"{base}/api/settings?token={token}", "POST",
                                {"animation": 9})
        assert status == 400

        status, payload = fetch(f"{base}/api/command?token={token}", "POST", {"action": "nope"})
        assert status == 400

        status, payload = fetch(f"{base}/api/debug/threads?token={token}")
        assert status == 200
        assert "threads" in payload and "handles" in payload and "media" in payload
    finally:
        console.stop()


def test_asset_upload_accepts_payload_larger_than_64kb():
    import base64
    import io
    import random

    pytest = __import__("pytest")
    pytest.importorskip("PIL")
    from PIL import Image

    # Random pixels produce an incompressible PNG whose base64 easily exceeds 64 KB.
    rng = random.Random(42)
    raw_pixels = bytes(rng.getrandbits(8) for _ in range(256 * 256 * 3))
    buffer = io.BytesIO()
    Image.frombytes("RGB", (256, 256), raw_pixels).save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode()
    assert len(encoded) > 100 * 1024

    service = make_service()
    console = ConsoleServer(service, port=0)
    url = console.start()
    try:
        base = f"http://127.0.0.1:{console.port}"
        token = urlparse(url).query.split("token=")[1]
        status, payload = fetch(f"{base}/api/asset/upload?token={token}", "POST", {
            "filename": "large.png", "data": encoded, "fit": "contain",
            "threshold": 128, "invert": False, "dither": False, "as_standby": True,
        })
        assert status == 200, payload
        assert payload["ok"] is True
        assert payload["frames"] == 1
    finally:
        console.stop()