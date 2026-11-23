"""System tray front-end.

The tray is intentionally thin: it renders :class:`halobar.service.ServiceStatus`
and forwards a handful of commands.  All real work happens in the service.
"""

from __future__ import annotations

import logging
import threading
import webbrowser

from .service import LyricsService, ServiceStatus

log = logging.getLogger(__name__)

try:  # pragma: no cover - optional at import time
    import pystray
    from PIL import Image, ImageDraw

    TRAY_AVAILABLE = True
except ImportError:  # pragma: no cover
    pystray = None  # type: ignore[assignment]
    TRAY_AVAILABLE = False


def make_icon_image(size: int = 64):
    """Draw a small pixel-bar glyph (no external asset needed)."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([4, 18, size - 4, size - 18], radius=8, fill=(28, 30, 38, 255))
    pattern = [0b1010, 0b0101, 0b1110, 0b0101, 0b1010]
    cell = (size - 16) // 10
    for row, bits in enumerate(pattern):
        for col in range(10):
            if bits >> (col % 4) & 1:
                x = 8 + col * cell
                y = 24 + row * cell
                draw.rectangle([x, y, x + cell - 1, y + cell - 1], fill=(240, 180, 200, 255))
    return image


def _menu_text(status: ServiceStatus) -> str:
    if not status.connected:
        return "设备未连接"
    if status.track:
        return f"{status.track} - {status.artist}"[:40]
    return "等待播放"


class TrayApp:
    """pystray wrapper around a running service."""

    def __init__(self, service: LyricsService, console_url: str = "") -> None:
        if not TRAY_AVAILABLE:
            raise RuntimeError("pystray/Pillow are not installed")
        self.service = service
        self.console_url = console_url
        self.icon = pystray.Icon(
            "halobar",
            make_icon_image(),
            "halobar",
            menu=self._build_menu(),
        )
        self._paused = False

    def _build_menu(self):
        service = self.service

        def toggle(icon=None, item=None):
            state = service.status().state
            if state in ("paused", "restored"):
                service.resume()
            else:
                service.pause()
            self.refresh()

        def restore(icon=None, item=None):
            service.request_restore()
            self.refresh()

        def open_console(icon=None, item=None):
            if self.console_url:
                webbrowser.open(self.console_url)

        def quit_app(icon=None, item=None):
            service.stop()
            if icon is not None:
                icon.stop()

        def toggle_label(item) -> str:
            state = service.status().state
            return "继续同步" if state in ("paused", "restored") else "暂停同步"

        return pystray.Menu(
            pystray.MenuItem(lambda item: _menu_text(service.status()), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(toggle_label, toggle),
            pystray.MenuItem("恢复屏幕（停止跟随）", restore),
            pystray.MenuItem(
                "打开控制台",
                open_console,
                enabled=bool(self.console_url),
                default=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", quit_app),
        )

    def refresh(self) -> None:
        try:
            status = self.service.status()
            self.icon.title = f"halobar · {_menu_text(status)}"
            self.icon.update_menu()
        except Exception:  # noqa: BLE001 - tray refresh must never crash
            log.debug("tray refresh failed", exc_info=True)

    def run(self) -> None:
        self.service.start()
        threading.Thread(target=self._tick, name="halobar-tray-refresh", daemon=True).start()
        try:
            self.icon.run()
        finally:
            self.service.stop()

    def _tick(self) -> None:
        import time

        while True:
            self.refresh()
            time.sleep(2.0)


def run_tray(service: LyricsService, console_url: str = "") -> None:
    TrayApp(service, console_url).run()