"""Local web console for the halobar service.

A dependency-free ``http.server`` bound to 127.0.0.1 that exposes the service status
and a handful of commands.  The page is served by this module; every API call needs
the session token that is injected into the page (or passed as ``?token=``).

Endpoints
---------
GET  /                     -> console page
GET  /api/status           -> service status + settings
GET  /api/logs             -> recent log lines
POST /api/command          -> {"action": "pause"|"resume"|"restore"|"nudge", "value": 0.5}
POST /api/settings         -> {"align": "...", "animation": 2, "scroll_long_lines": true, ...}
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from collections import deque
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .service import LyricsService

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "web"


class RingBufferHandler(logging.Handler):
    """Keeps the latest log records for the console."""

    def __init__(self, capacity: int = 300) -> None:
        super().__init__()
        self.records: deque[str] = deque(maxlen=capacity)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                                            datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(self.format(record))
        except Exception:  # noqa: BLE001 - logging must never raise
            pass

    def snapshot(self) -> list[str]:
        self.acquire()
        try:
            return list(self.records)
        finally:
            self.release()


class _ConsoleHTTPServer(ThreadingHTTPServer):
    """Thread-per-request server that does not keep finished threads around.

    ``ThreadingHTTPServer`` defaults to ``block_on_close = True``, which makes the
    server retain every request thread object for its whole lifetime.  For a page
    that polls twice a second that list grows without bound, so it is disabled and
    request handlers get a socket timeout to avoid stuck threads.
    """

    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True


class ConsoleServer:
    def __init__(self, service: LyricsService, port: int = 8765, token: str | None = None,
                 settings=None, environment=None) -> None:
        self.service = service
        self.port = port
        self.token = token or secrets.token_urlsafe(16)
        self.settings = settings
        self.environment = environment
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.logs = RingBufferHandler()
        logging.getLogger("halobar").addHandler(self.logs)
        logging.getLogger("halobar").setLevel(logging.INFO)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> str:
        handler = _make_handler(self)
        last_error = None
        for offset in range(0, 10):
            try:
                self._httpd = _ConsoleHTTPServer(("127.0.0.1", self.port + offset), handler)
                break
            except OSError as exc:  # port busy -> try the next one
                last_error = exc
                continue
        if self._httpd is None:
            raise OSError(f"cannot bind the console port: {last_error}")
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="halobar-console", daemon=True)
        self._thread.start()
        if self.environment is not None and hasattr(self.environment, "start"):
            self.environment.start()
        url = f"http://127.0.0.1:{self.port}/?token={self.token}"
        log.info("console listening on %s", url)
        return url

    def stop(self) -> None:
        if self.environment is not None and hasattr(self.environment, "stop"):
            self.environment.stop()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(2.0)
        logging.getLogger("halobar").removeHandler(self.logs)

    # -------------------------------------------------------------------- helpers
    def status_payload(self) -> dict:
        from . import __version__, buildinfo

        status = asdict(self.service.status())
        config = self.service.config
        status["settings"] = {
            "align": config.align.value,
            "animation": config.animation,
            "scroll_long_lines": config.scroll_long_lines,
            "line_mode": config.line_mode,
            "page_units": config.page_units,
            "auto_check_updates": config.auto_check_updates,
            "update_proxy": config.update_proxy,
            "standby_after": config.standby_after,
            "standby_scene": config.standby_scene,
        }
        status["version"] = __version__
        status["build"] = buildinfo.load()
        status["firmware"] = status.get("firmware") or "-"
        status["environment"] = (
            self.environment.get().as_dict() if self.environment is not None else None)
        return status

    def handle_command(self, payload: dict) -> tuple[int, dict]:
        action = str(payload.get("action", "")).lower()
        if action == "pause":
            self.service.pause()
        elif action == "resume":
            self.service.resume()
        elif action == "restore":
            self.service.request_restore()
        elif action == "nudge":
            try:
                seconds = float(payload.get("value", 0))
            except (TypeError, ValueError):
                return HTTPStatus.BAD_REQUEST, {"error": "value must be a number"}
            self.service.nudge(max(-30.0, min(30.0, seconds)))
        elif action == "next_line":
            self.service.nudge_to_next_line()
        else:
            return HTTPStatus.BAD_REQUEST, {"error": f"unknown action {action!r}"}
        log.info("console command: %s %s", action, payload.get("value", ""))
        return HTTPStatus.OK, {"ok": True, "status": self.status_payload()}

    def handle_settings(self, payload: dict) -> tuple[int, dict]:
        try:
            self.service.update_config(
                align=payload.get("align"),
                animation=payload.get("animation"),
                scroll_long_lines=payload.get("scroll_long_lines"),
                line_mode=payload.get("line_mode"),
                page_units=payload.get("page_units"),
                standby_after=payload.get("standby_after"),
            )
        except (TypeError, ValueError) as exc:
            return HTTPStatus.BAD_REQUEST, {"error": str(exc)}
        if self.settings is not None:
            config = self.service.config
            self.settings.apply_align(config.align.value)
            self.settings.apply_animation(config.animation)
            self.settings.scroll_long_lines = config.scroll_long_lines
            self.settings.line_mode = config.line_mode
            self.settings.standby_after = config.standby_after
            self.settings.standby_scene = config.standby_scene
            self.settings.standby_scene_option = config.standby_scene_option
            if payload.get("auto_check_updates") is not None:
                self.settings.auto_check_updates = bool(payload.get("auto_check_updates"))
                self.service.config.auto_check_updates = self.settings.auto_check_updates
            if payload.get("update_proxy") in ("auto", "none"):
                self.settings.update_proxy = payload["update_proxy"]
                self.service.config.update_proxy = payload["update_proxy"]
            self.settings.save()
        return HTTPStatus.OK, {"ok": True, "status": self.status_payload()}

    def handle_shortcut(self) -> tuple[int, dict]:
        from .shortcut import create_desktop_shortcut

        try:
            link = create_desktop_shortcut()
        except OSError as exc:
            return HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)}
        if self.settings is not None:
            self.settings.create_shortcut_done = True
            self.settings.save()
        return HTTPStatus.OK, {"ok": True, "path": str(link)}

    # ------------------------------------------------------------------ update
    def handle_update_check(self) -> tuple[int, dict]:
        self.service.request_update_check()
        return HTTPStatus.OK, {"ok": True}

    def handle_update_apply(self) -> tuple[int, dict]:
        self.service.request_update_apply()
        return HTTPStatus.OK, {"ok": True}

    def thread_snapshot(self) -> dict:
        """Diagnostics: how many threads exist and where they are stuck."""
        import collections
        import sys
        import threading
        import traceback

        frames = sys._current_frames()
        groups: collections.Counter[str] = collections.Counter()
        for thread in threading.enumerate():
            frame = frames.get(thread.ident)
            where = f"{thread.name}: (no frame)"
            if frame is not None:
                stack = traceback.extract_stack(frame)
                if stack:
                    last = stack[-1]
                    where = f"{thread.name}: {Path(last.filename).name}:{last.lineno} {last.name}"
            groups[where] += 1
        return {
            "threads": threading.active_count(),
            "handles": _handle_count(),
            "top": groups.most_common(15),
            "media": self.service.media_diagnostics(),
        }

    # ------------------------------------------------------------------- assets
    def handle_asset_upload(self, payload: dict) -> tuple[int, dict]:
        import base64
        import binascii

        from . import bitmap
        from .service import UploadJob

        encoded = payload.get("data") or ""
        if not isinstance(encoded, str) or not encoded:
            return HTTPStatus.BAD_REQUEST, {"error": "missing file data"}
        if len(encoded) > 48 * 1024 * 1024:
            return HTTPStatus.BAD_REQUEST, {"error": "file too large"}
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return HTTPStatus.BAD_REQUEST, {"error": "invalid base64 data"}

        name = str(payload.get("filename") or "asset")
        options = {
            "fit": str(payload.get("fit") or "contain"),
            "threshold": int(payload.get("threshold") or 128),
            "invert": bool(payload.get("invert")),
            "dither": bool(payload.get("dither")),
            "zoom": float(payload.get("zoom") or 1.0),
            "offset_x": float(payload.get("offset_x") or 0.0),
            "offset_y": float(payload.get("offset_y") or 0.0),
            "brightness": int(payload.get("brightness") or 0),
            "contrast": float(payload.get("contrast") or 1.0),
        }
        if options["fit"] not in ("contain", "cover", "stretch"):
            return HTTPStatus.BAD_REQUEST, {"error": "fit must be contain/cover/stretch"}
        options["threshold"] = max(0, min(255, options["threshold"]))
        options["zoom"] = max(0.2, min(4.0, options["zoom"]))
        options["offset_x"] = max(-1.0, min(1.0, options["offset_x"]))
        options["offset_y"] = max(-1.0, min(1.0, options["offset_y"]))
        options["brightness"] = max(-100, min(100, options["brightness"]))
        options["contrast"] = max(0.2, min(3.0, options["contrast"]))
        try:
            asset = bitmap.load(raw, name)
            processed = bitmap.apply_options(asset, **options)
            stream = bitmap.encode(processed)
        except (bitmap.AssetError, RuntimeError) as exc:
            return HTTPStatus.BAD_REQUEST, {"error": str(exc)}

        job = UploadJob(
            data=stream,
            category=processed.category,
            option=processed.option,
            name=name,
            frames=len(processed.frames),
            as_standby=bool(payload.get("as_standby", True)),
            pause_after=bool(payload.get("pause_after", False)),
        )
        self.service.submit_upload(job)
        if self.settings is not None:
            import time as _time

            self.settings.last_asset = {
                "name": name,
                "frames": len(processed.frames),
                "category": processed.category,
                "option": processed.option,
                "time": _time.time(),
            }
            if job.as_standby:
                self.settings.standby_scene = processed.category
                self.settings.standby_scene_option = processed.option
            self.settings.save()
        preview = bitmap.preview_png(processed.frames[0])
        return HTTPStatus.OK, {
            "ok": True,
            "frames": len(processed.frames),
            "animated": processed.animated,
            "bytes": len(stream),
            "category": processed.category,
            "option": processed.option,
            "preview": "data:image/png;base64," + __import__("base64").b64encode(preview).decode(),
        }

    def handle_asset_display(self, payload: dict) -> tuple[int, dict]:
        try:
            category = int(payload.get("category"))
            option = int(payload.get("option"))
        except (TypeError, ValueError):
            status = self.service.status()
            if not status.asset_category:
                return HTTPStatus.BAD_REQUEST, {"error": "还没有上传过素材"}
            category, option = status.asset_category, status.asset_option
        self.service.request_scene(category, option)
        return HTTPStatus.OK, {"ok": True}


_DEFAULT_MAX_BODY = 64 * 1024
_UPLOAD_MAX_BODY = 50 * 1024 * 1024


def _init_handle_counter():
    try:
        import ctypes
        import ctypes.wintypes as wt

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wt.HANDLE
        kernel32.GetProcessHandleCount.restype = wt.BOOL
        kernel32.GetProcessHandleCount.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
        return kernel32, wt.DWORD
    except Exception:  # noqa: BLE001 - non-Windows
        return None, None


_WIN_KERNEL32, _WIN_DWORD = _init_handle_counter()


def _handle_count() -> int:
    """Current handle count of this process (Windows only, best effort)."""
    if _WIN_KERNEL32 is None or _WIN_DWORD is None:
        return 0
    try:
        import ctypes

        count = _WIN_DWORD(0)
        if _WIN_KERNEL32.GetProcessHandleCount(
            _WIN_KERNEL32.GetCurrentProcess(), ctypes.byref(count)
        ):
            return int(count.value)
    except Exception:  # noqa: BLE001 - diagnostics only
        pass
    return 0


class _PayloadTooLarge(ValueError):
    pass


def _make_handler(server: ConsoleServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "halobar-console/0.1"
        protocol_version = "HTTP/1.0"
        timeout = 15          # socketserver applies this to each connection

        def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
            log.debug("http %s", fmt % args)

        # ------------------------------------------------------------- utilities
        def _authorised(self) -> bool:
            query = parse_qs(urlparse(self.path).query)
            token = (query.get("token") or [None])[0] or self.headers.get("X-Halo-Token")
            return token == server.token

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self, max_bytes: int = _DEFAULT_MAX_BODY) -> dict:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return {}
            if length <= 0:
                return {}
            if length > max_bytes:
                raise _PayloadTooLarge(f"request body exceeds {max_bytes} bytes")
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}
            return data if isinstance(data, dict) else {}

        # ------------------------------------------------------------------- GET
        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._serve_page()
                return
            if not self._authorised():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "token required"})
                return
            if path == "/api/status":
                self._json(HTTPStatus.OK, server.status_payload())
            elif path == "/api/logs":
                self._json(HTTPStatus.OK, {"lines": server.logs.snapshot()})
            elif path == "/api/debug/threads":
                self._json(HTTPStatus.OK, server.thread_snapshot())
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        # ------------------------------------------------------------------ POST
        def do_POST(self) -> None:  # noqa: N802
            if not self._authorised():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "token required"})
                return
            path = urlparse(self.path).path
            max_bytes = _UPLOAD_MAX_BODY if path == "/api/asset/upload" else _DEFAULT_MAX_BODY
            try:
                payload = self._body(max_bytes=max_bytes)
            except _PayloadTooLarge as exc:
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": str(exc)})
                return
            if path == "/api/command":
                status, data = server.handle_command(payload)
            elif path == "/api/settings":
                status, data = server.handle_settings(payload)
            elif path == "/api/asset/upload":
                status, data = server.handle_asset_upload(payload)
            elif path == "/api/asset/display":
                status, data = server.handle_asset_display(payload)
            elif path == "/api/shortcut":
                status, data = server.handle_shortcut()
            elif path == "/api/update/check":
                status, data = server.handle_update_check()
            elif path == "/api/update/apply":
                status, data = server.handle_update_apply()
            else:
                status, data = HTTPStatus.NOT_FOUND, {"error": "not found"}
            self._json(status, data)

        # ------------------------------------------------------------------ page
        def _serve_page(self) -> None:
            page = STATIC_DIR / "index.html"
            if not page.exists():
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "console assets missing"})
                return
            html = page.read_text(encoding="utf-8").replace("__HALO_TOKEN__", server.token)
            body = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler