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
    """Draw the app icon: a rounded pink→violet tile with five pixel bars."""
    supersample = 4
    side = size * supersample
    image = Image.new("RGBA", (side, side), (0, 0, 0, 0))

    # vertical gradient background
    top = (255, 138, 178)
    bottom = (146, 128, 255)
    pixels = image.load()
    for y in range(side):
        t = y / max(1, side - 1)
        colour = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        for x in range(side):
            pixels[x, y] = (*colour, 255)

    # rounded corners
    mask = Image.new("L", (side, side), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, side - 1, side - 1], radius=int(side * 0.26), fill=255)
    image.putalpha(mask)

    # five equaliser bars, bottom aligned
    draw = ImageDraw.Draw(image)
    count = 5
    margin = side * 0.19
    gap = side * 0.055
    bar_width = (side - 2 * margin - (count - 1) * gap) / count
    base = side * 0.79
    heights = (0.34, 0.58, 0.78, 0.52, 0.30)
    for index, ratio in enumerate(heights):
        left = margin + index * (bar_width + gap)
        top_y = base - side * ratio
        draw.rounded_rectangle(
            [left, top_y, left + bar_width, base],
            radius=bar_width / 2.3, fill=(255, 255, 255, 236))

    return image.resize((size, size), Image.LANCZOS)


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

        def create_shortcut(icon=None, item=None):
            from .shortcut import create_desktop_shortcut

            try:
                link = create_desktop_shortcut()
                log.info("shortcut created at %s", link)
            except OSError as exc:
                log.warning("shortcut creation failed: %s", exc)

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
                default=True,
            ),
            pystray.MenuItem("创建桌面快捷方式", create_shortcut),
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