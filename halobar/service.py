"""Background service: owns the device, the playback source and the sync engine.

Everything that touches the speaker goes through :class:`LyricsService`.  The tray
and the web console only call its thread-safe methods, so there is exactly one
writer for the HID device at any time.

Design notes
------------
* One worker thread runs its own asyncio loop: the render/sync engine advances
  every ``poll_seconds`` (100 ms) using monotonic clock interpolation, while
  playback source reads are decoupled to ``media_poll_seconds`` (default 0.5 s)
  or woken early by WinRT SMTC change events.
* Non-device errors (media read or lyric fetch failures) never disconnect the
  HID screen or overwrite the initial display snapshot.
* Per-track lyric offsets and manual song-id overrides are persisted when a
  :class:`halobar.settings.Settings` instance is attached.
* On stop, the display snapshot taken at start is restored and all media/update
  resources are released cleanly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Protocol

from .device.screen import Align, DisplaySnapshot, RenderError, Screen, Scroll, gamma_correct_rgb
from .lyrics import Lyrics, NeteaseClient
from .media import Playback, PlaybackReader
from .sync import EngineConfig, LyricsEngine

log = logging.getLogger(__name__)


class ScreenFactory(Protocol):
    def __call__(self) -> Screen: ...


@dataclass
class ServiceStatus:
    """Immutable status snapshot for the UI."""

    running: bool = False
    connected: bool = False
    firmware: str = ""
    source: str = "none"
    track: str = ""
    artist: str = ""
    track_key: str = ""
    position: float | None = None
    duration: float | None = None
    playing: bool = False
    line: str = ""
    display_text: str = ""
    display_mode: str = "idle"          # idle | text | cleared | scene | standby | restored
    display_scene: int | None = None
    color: tuple[int, int, int] = (255, 255, 255)
    clock_offset: float = 0.0
    lyric_index: int = -1
    lyric_count: int = 0
    lyrics_source: str = ""
    smtc_health: str = "ok"             # ok | backoff | circuit-open | custom
    state: str = "idle"
    last_error: str = ""
    upload_state: str = "idle"          # idle | uploading | done | error
    upload_progress: float = 0.0
    asset_name: str = ""
    asset_frames: int = 0
    asset_category: int = 0
    asset_option: int = 0
    asset_time: float = 0.0
    update_state: str = "idle"          # idle | checking | available | none | downloading | ready | error
    update_version: str = ""
    update_notes: str = ""
    update_url: str = ""
    update_progress: float = 0.0
    update_error: str = ""
    updated_at: float = field(default_factory=time.time)


@dataclass
class UploadJob:
    """A prepared bitmap payload waiting for the device."""

    data: bytes
    category: int
    option: int
    name: str
    frames: int
    as_standby: bool = True
    pause_after: bool = False


@dataclass
class ServiceConfig:
    align: Align = Align.CENTER
    scroll: Scroll | None = None
    animation: int | None = None
    color: tuple[int, int, int] | None = None
    color_calibration: bool = True
    standby_scene: int | None = 8
    standby_after: float = 20.0
    poll_seconds: float = 0.1
    media_poll_seconds: float = 0.5
    lyric_poll_seconds: float = 1.0     # kept for backwards compatibility
    reconnect_seconds: float = 3.0
    scroll_long_lines: bool = True
    scroll_threshold_units: int = 32
    line_mode: str = "page"
    lyric_lang: str = "original"        # original | translation | bilingual
    page_units: int = 32
    scroll_lead_spaces: int = 2
    interlude_clear_after: float = 4.0
    interlude_scene_after: float = 10.0
    standby_scene_option: int = 0
    auto_check_updates: bool = True
    update_proxy: str = "auto"
    update_check_hours: float = 6.0


class LyricsService:
    """Threaded coordinator between playback, lyrics and the pixel display."""

    def __init__(
        self,
        config: ServiceConfig | None = None,
        *,
        screen_factory: ScreenFactory | None = None,
        reader: Callable[..., object] | None = None,
        lyrics_client: NeteaseClient | None = None,
        settings=None,
    ) -> None:
        self.config = config or ServiceConfig()
        self.settings = settings
        self._screen_factory = screen_factory or (lambda: Screen())
        self._custom_reader = reader
        self._playback_reader: PlaybackReader | None = None
        if reader is None:
            self._playback_reader = PlaybackReader(
                sixk_interval=min(0.35, max(0.1, self.config.media_poll_seconds)),
                smtc_interval=max(0.2, self.config.media_poll_seconds),
            )
            self._reader: Callable[..., object] = self._playback_reader.read
        else:
            self._reader = reader
        self._lyrics_client = lyrics_client or NeteaseClient(path_for_cache())
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._suspended = threading.Event()
        self._restore_requested = threading.Event()
        self._status = ServiceStatus(
            color=self.config.color or (255, 255, 255),
        )
        self._engine: LyricsEngine | None = None
        self._screen: Screen | None = None
        self._snapshot: DisplaySnapshot | None = None
        self._nudge_queue: list[float] = []
        self._next_line_requested = False
        self._jump_line_index: int | None = None
        self._reset_offset_requested = False
        self._upload_job: UploadJob | None = None
        self._uploading = False
        self._scene_request: tuple[int, int] | None = None
        self._color_request: tuple[int, int, int] | None = None
        self._update_thread: threading.Thread | None = None
        self._update_check_requested = threading.Event()
        self._update_apply_requested = threading.Event()
        self._release = None

        # Decoupled media polling cache & active lyrics snapshot for UI timeline.
        self._cached_playback: Playback | None = None
        self._next_media_poll: float = 0.0
        self._last_media_warn_at: float = 0.0
        self._active_lyrics: Lyrics = Lyrics()

    # ------------------------------------------------------------------ control
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._next_media_poll = 0.0
        self._thread = threading.Thread(target=self._run, name="halobar-service", daemon=True)
        self._thread.start()
        self._update_thread = threading.Thread(
            target=self._update_worker, name="halobar-updates", daemon=True
        )
        self._update_thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        current = threading.current_thread()
        if self._thread and self._thread is not current:
            self._thread.join(timeout)
        if self._update_thread and self._update_thread is not current:
            self._update_thread.join(min(2.0, timeout))
        if self._playback_reader is not None:
            self._playback_reader.close()

    def pause(self) -> None:
        self._paused.set()
        self._set_status(state="paused")

    def resume(self) -> None:
        self._suspended.clear()
        self._paused.clear()
        self._next_media_poll = 0.0
        self._set_status(state="following")

    def nudge(self, seconds: float) -> None:
        with self._lock:
            self._nudge_queue.append(seconds)

    def nudge_to_next_line(self) -> None:
        """Jump the clock forward so the next lyric line starts now."""
        with self._lock:
            self._next_line_requested = True

    def jump_to_line(self, index: int) -> None:
        """Align the playback clock so line ``index`` starts right now."""
        with self._lock:
            self._jump_line_index = int(index)

    def reset_offset(self) -> None:
        """Reset manual timing offset for the current track back to 0."""
        with self._lock:
            self._reset_offset_requested = True

    def _hardware_color(self, rgb: tuple[int, int, int] | None) -> tuple[int, int, int] | None:
        if rgb is None:
            return None
        return gamma_correct_rgb(rgb) if self.config.color_calibration else rgb

    def request_color(self, rgb: tuple[int, int, int] | None) -> None:
        """Set or clear the PixelBar RGB text/accent colour."""
        with self._lock:
            if rgb is None:
                self.config.color = None
                if self._engine is not None:
                    self._engine.config.color = None
                    self._engine.force_redraw()
                return
            clean = (
                max(0, min(255, int(rgb[0]))),
                max(0, min(255, int(rgb[1]))),
                max(0, min(255, int(rgb[2]))),
            )
            self.config.color = clean
            hw_rgb = self._hardware_color(clean)
            self._color_request = hw_rgb
            if self._engine is not None:
                self._engine.config.color = hw_rgb
                self._engine.force_redraw()
            self._status = replace(self._status, color=clean, updated_at=time.time())

    def media_diagnostics(self) -> dict[str, object]:
        if self._playback_reader is None:
            return {"mode": "custom-reader"}
        return self._playback_reader.diagnostics()

    # ------------------------------------------------------------- lyrics API
    def lyrics_timeline(self) -> dict[str, object]:
        """Thread-safe snapshot of the current song's lyric timeline for the UI."""
        with self._lock:
            lyrics = self._active_lyrics
            status = self._status
            lines = [
                {
                    "index": idx,
                    "start": round(line.start, 2),
                    "end": round(line.end, 2),
                    "text": line.text,
                    "translation": line.translation,
                }
                for idx, line in enumerate(lyrics.lines)
            ]
            return {
                "track_key": status.track_key,
                "title": status.track,
                "artist": status.artist,
                "source": status.lyrics_source,
                "offset": round(status.clock_offset, 2),
                "active_index": status.lyric_index,
                "display_text": status.display_text,
                "has_translation": any(bool(item["translation"]) for item in lines),
                "lines": lines,
            }

    def lyric_candidates(self) -> list[dict[str, object]]:
        """Search NetEase candidates for the currently playing track."""
        status = self.status()
        if not status.track:
            return []
        search_fn = getattr(self._lyrics_client, "search_candidates", None)
        if not callable(search_fn):
            return []
        matches = search_fn(status.track, status.artist, status.duration or 0.0)
        return [
            {
                "song_id": item.song_id,
                "title": item.title,
                "artist": item.artist,
                "duration": round(item.duration_ms / 1000.0, 1) if item.duration_ms else 0.0,
                "score": round(item.score, 2),
            }
            for item in matches
        ]

    def select_lyric_candidate(self, song_id: int) -> dict[str, object]:
        """Switch the active track's lyrics to an explicit NetEase song id."""
        status = self.status()
        if not status.track:
            raise ValueError("当前没有正在播放的曲目")
        fetch_fn = getattr(self._lyrics_client, "fetch_by_id", None)
        if callable(fetch_fn):
            lyrics = fetch_fn(int(song_id), status.track, status.artist, status.duration or 0.0)
        else:
            lyrics = self._lyrics_client.lyrics_for(
                status.track, status.artist, status.duration or 0.0, str(song_id)
            )
        with self._lock:
            self._active_lyrics = lyrics
            if self._engine is not None:
                self._engine.set_lyrics(lyrics)
            self._status = replace(
                self._status,
                lyrics_source=lyrics.source,
                lyric_count=len(lyrics.lines),
                lyric_index=-1,
                updated_at=time.time(),
            )
            if self.settings is not None and status.track_key:
                self.settings.set_track_song_id(status.track_key, str(song_id))
                self.settings.save()
        log.info("switched lyrics for %s to song_id=%s (%d lines)",
                 status.track, song_id, len(lyrics.lines))
        return self.lyrics_timeline()

    # ------------------------------------------------------------------- assets
    def submit_upload(self, job: UploadJob) -> None:
        """Queue a bitmap upload; the worker thread performs it (single writer)."""
        with self._lock:
            self._upload_job = job
            self._suspended.clear()
            self._status = replace(self._status, upload_state="queued", upload_progress=0.0)

    def request_scene(self, category: int, option: int = 0) -> None:
        """Switch the screen to a scene (e.g. the uploaded asset) right now."""
        with self._lock:
            self._scene_request = (int(category), int(option))

    def _do_upload(self, job: UploadJob) -> None:
        screen = self._screen
        if screen is None:
            raise RenderError("device is not connected")
        screen.upload_bitmap(
            job.data,
            category=job.category,
            option=job.option,
            progress=lambda value: self._set_status(upload_progress=value),
        )
        if job.as_standby:
            self.config.standby_scene = job.category
            self.config.standby_scene_option = job.option
            if self._engine is not None:
                self._engine.config.standby_scene = job.category
                self._engine.config.standby_scene_option = job.option
        if job.pause_after:
            self._paused.set()
        self._set_status(
            upload_state="done", upload_progress=1.0,
            asset_name=job.name, asset_frames=job.frames,
            asset_category=job.category, asset_option=job.option,
            asset_time=time.time(),
            display_mode="scene", display_scene=job.category, display_text="",
        )
        log.info("asset uploaded: %s (%d frames, scene %d/%d)",
                 job.name, job.frames, job.category, job.option)

    # ---------------------------------------------------------------- updates
    def request_update_check(self) -> None:
        self._update_check_requested.set()

    def request_update_apply(self) -> None:
        self._update_apply_requested.set()

    def _update_worker(self) -> None:
        from . import update as update_module

        if self._stop.wait(4.0):
            return
        while not self._stop.is_set():
            try:
                if self.config.auto_check_updates or self._update_check_requested.is_set():
                    self._update_check_requested.clear()
                    self._check_updates(update_module)
                if self._update_apply_requested.is_set():
                    self._update_apply_requested.clear()
                    self._perform_update(update_module)
            except Exception as exc:  # noqa: BLE001 - the updater must never crash the app
                log.warning("update worker: %s", exc)
                self._set_status(update_state="error", update_error=str(exc))
            deadline = time.monotonic() + max(0.25, self.config.update_check_hours) * 3600
            while time.monotonic() < deadline and not self._stop.is_set():
                if (self._update_check_requested.is_set()
                        or self._update_apply_requested.is_set()):
                    break
                if self._stop.wait(0.5):
                    return

    def _check_updates(self, update_module) -> None:
        from . import __version__

        self._set_status(update_state="checking", update_error="")
        release = update_module.check(current=__version__, proxy=self.config.update_proxy)
        self._release = release
        if release is None:
            self._set_status(update_state="none", update_version="", update_notes="")
            log.info("no update available (current %s)", __version__)
            return
        self._set_status(
            update_state="available",
            update_version=release.version,
            update_notes=release.notes[:4000],
            update_url=release.html_url,
        )
        log.info("update available: %s", release.version)

    def _perform_update(self, update_module) -> None:
        from .settings import app_dir

        release = self._release
        if release is None:
            self._check_updates(update_module)
            release = self._release
        if release is None or not release.available:
            raise update_module.UpdateError("没有可用的更新包")

        update_dir = app_dir() / "updates"
        zip_path = update_dir / release.asset_name
        self._set_status(update_state="downloading", update_progress=0.0)
        update_module.download(
            release,
            zip_path,
            proxy=self.config.update_proxy,
            progress=lambda value: self._set_status(update_progress=value),
        )
        staging = update_dir / f"staging-{release.version}"
        update_module.extract(zip_path, staging)
        self._set_status(update_state="ready", update_progress=1.0)

        log.info("restarting into the updater for %s", release.version)
        self._set_status(update_state="applying")
        update_module.launch_updater(staging)
        try:
            self.stop()
        except Exception:  # noqa: BLE001 - updater is already launched, must exit now
            log.exception("service stop failed during update handoff")
        os._exit(0)

    def update_config(
        self,
        *,
        align: str | None = None,
        animation: int | None | str = "keep",
        scroll_long_lines: bool | None = None,
        line_mode: str | None = None,
        lyric_lang: str | None = None,
        page_units: int | None = None,
        standby_after: float | None = None,
        standby_scene: int | None | str = "keep",
        standby_scene_option: int | None = None,
        color: list[int] | tuple[int, int, int] | None | str = "keep",
        color_calibration: bool | None = None,
    ) -> None:
        """Apply live settings from the console."""
        with self._lock:
            if align is not None:
                self.config.align = Align(str(align))
            if line_mode is not None:
                if line_mode not in ("page", "scroll", "first"):
                    raise ValueError("line_mode must be page/scroll/first")
                self.config.line_mode = line_mode
            if lyric_lang is not None:
                if lyric_lang not in ("original", "translation", "bilingual"):
                    raise ValueError("lyric_lang must be original/translation/bilingual")
                self.config.lyric_lang = lyric_lang
            if page_units is not None:
                units = int(page_units)
                if not 8 <= units <= 64:
                    raise ValueError("page_units must be 8..64")
                self.config.page_units = units
            if animation != "keep":
                if animation is None:
                    self.config.animation = None
                else:
                    value = int(animation)
                    if not 0 <= value <= 4:
                        raise ValueError("animation must be 0..4 or null")
                    self.config.animation = value
            if scroll_long_lines is not None:
                self.config.scroll_long_lines = bool(scroll_long_lines)
            if standby_after is not None:
                seconds = float(standby_after)
                if not 0 <= seconds <= 600:
                    raise ValueError("standby_after must be 0..600")
                self.config.standby_after = seconds
            if standby_scene != "keep":
                if standby_scene is None:
                    self.config.standby_scene = None
                else:
                    scene_id = int(standby_scene)
                    if not 0 <= scene_id <= 16:
                        raise ValueError("standby_scene must be 0..16 or null")
                    self.config.standby_scene = scene_id
            if standby_scene_option is not None:
                opt = int(standby_scene_option)
                if not 0 <= opt <= 255:
                    raise ValueError("standby_scene_option must be 0..255")
                self.config.standby_scene_option = opt
            if color_calibration is not None:
                self.config.color_calibration = bool(color_calibration)
                if self.config.color is not None:
                    self._color_request = self._hardware_color(self.config.color)
            if color != "keep":
                if color is None:
                    self.config.color = None
                else:
                    if len(color) != 3:  # type: ignore[arg-type]
                        raise ValueError("color must be [R, G, B] or null")
                    clean = (
                        max(0, min(255, int(color[0]))),  # type: ignore[index]
                        max(0, min(255, int(color[1]))),  # type: ignore[index]
                        max(0, min(255, int(color[2]))),  # type: ignore[index]
                    )
                    self.config.color = clean
                    self._color_request = self._hardware_color(clean)
                    self._status = replace(self._status, color=clean, updated_at=time.time())
            engine = self._engine
            if engine is not None:
                engine.config.align = self.config.align
                engine.config.animation = self.config.animation
                engine.config.scroll_long_lines = self.config.scroll_long_lines
                engine.config.line_mode = self.config.line_mode
                engine.config.lyric_lang = self.config.lyric_lang
                engine.config.page_units = self.config.page_units
                engine.config.standby_after = self.config.standby_after
                engine.config.standby_scene = self.config.standby_scene
                engine.config.standby_scene_option = self.config.standby_scene_option
                engine.config.color = self._hardware_color(self.config.color)
                engine.force_redraw()

    def request_restore(self) -> None:
        """Restore the original screen and stop following until :meth:`resume`."""
        self._restore_requested.set()

    def status(self) -> ServiceStatus:
        with self._lock:
            return replace(self._status)

    # ------------------------------------------------------------------- worker
    def _run(self) -> None:
        try:
            asyncio.run(self._loop())
        except Exception as exc:  # noqa: BLE001 - worker must never crash silently
            log.exception("service loop crashed")
            self._set_status(state="error", last_error=str(exc))
        finally:
            if self._playback_reader is not None:
                self._playback_reader.close()

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        self._set_status(running=True, state="connecting")
        try:
            while not self._stop.is_set():
                if self._suspended.is_set():
                    await asyncio.sleep(0.2)
                    continue
                if self._screen is None:
                    if not await self._connect(loop):
                        await asyncio.sleep(self.config.reconnect_seconds)
                        continue
                if self._upload_job is not None:
                    with self._lock:
                        job, self._upload_job = self._upload_job, None
                    if job is not None:
                        self._uploading = True
                        self._set_status(upload_state="uploading", upload_progress=0.0)
                        try:
                            await loop.run_in_executor(None, self._do_upload, job)
                        except Exception as exc:  # noqa: BLE001 - a bad upload must not kill the worker
                            log.warning("upload failed: %s", exc)
                            self._set_status(upload_state="error", last_error=str(exc))
                        finally:
                            self._uploading = False
                        await asyncio.sleep(0.1)
                        continue
                if self._color_request is not None:
                    with self._lock:
                        rgb, self._color_request = self._color_request, None
                    if rgb is not None and hasattr(self._screen, "set_color"):
                        try:
                            self._screen.set_color(rgb)
                            log.info("screen colour set to #%02x%02x%02x", *rgb)
                        except (RenderError, OSError) as exc:
                            self._set_status(last_error=str(exc))
                if self._scene_request is not None:
                    with self._lock:
                        req, self._scene_request = self._scene_request, None
                    if req is not None:
                        category, option = req
                        try:
                            self._screen.show_scene(category, option=option)
                            self._set_status(display_mode="scene", display_scene=category, display_text="")
                            log.info("switched to scene %d/%d", category, option)
                        except (RenderError, OSError) as exc:
                            self._set_status(last_error=str(exc))
                        await asyncio.sleep(0.1)
                        continue
                if self._restore_requested.is_set():
                    self._restore_requested.clear()
                    self._restore_now()
                    self._suspended.set()
                    self._set_status(state="restored", display_mode="restored", display_text="")
                    await asyncio.sleep(0.2)
                    continue
                if self._suspended.is_set() or self._paused.is_set():
                    await asyncio.sleep(0.2)
                    continue
                try:
                    await self._tick(loop)
                except (OSError, RenderError, TimeoutError) as exc:
                    log.warning("device tick failed: %s", exc)
                    self._set_status(last_error=str(exc))
                    self._drop_device()
                except Exception as exc:  # noqa: BLE001 - non-device errors keep HID alive
                    log.warning("tick error (keeping device connected): %s", exc)
                    self._set_status(last_error=str(exc))
                await asyncio.sleep(self.config.poll_seconds)
        finally:
            self._restore_now()
            self._set_status(running=False, state="stopped")

    async def _connect(self, loop: asyncio.AbstractEventLoop) -> bool:
        screen: Screen | None = None
        try:
            screen = await loop.run_in_executor(None, self._screen_factory)
            snapshot = await loop.run_in_executor(None, screen.snapshot)
        except (OSError, RenderError, TimeoutError) as exc:
            if screen is not None:
                try:
                    screen.close()
                except Exception:  # noqa: BLE001
                    pass
            log.info("device not ready: %s", exc)
            self._set_status(connected=False, state="waiting-device", last_error=str(exc))
            return False

        device_rgb = self.config.color or (255, 255, 255)
        if self.config.color is None and hasattr(snapshot, "state") and len(snapshot.state) >= 4:
            device_rgb = (snapshot.state[1], snapshot.state[2], snapshot.state[3])

        self._screen = screen
        self._snapshot = snapshot
        hw_color = self._hardware_color(self.config.color)
        if hw_color is not None:
            self._color_request = hw_color
        self._engine = LyricsEngine(screen, EngineConfig(
            poll_seconds=self.config.poll_seconds,
            media_poll_seconds=self.config.media_poll_seconds,
            align=self.config.align,
            scroll=self.config.scroll,
            animation=self.config.animation,
            color=hw_color,
            standby_scene=self.config.standby_scene,
            standby_scene_option=self.config.standby_scene_option,
            standby_after=self.config.standby_after,
            scroll_long_lines=self.config.scroll_long_lines,
            scroll_threshold_units=self.config.scroll_threshold_units,
            line_mode=self.config.line_mode,
            lyric_lang=self.config.lyric_lang,
            page_units=self.config.page_units,
            scroll_lead_spaces=self.config.scroll_lead_spaces,
            interlude_clear_after=self.config.interlude_clear_after,
            interlude_scene_after=self.config.interlude_scene_after,
        ))
        self._next_media_poll = 0.0
        log.info("device connected (firmware %s)", screen.firmware)
        self._set_status(
            connected=True, firmware=screen.firmware, state="following",
            color=device_rgb, last_error="",
        )
        return True

    def _effective_media_interval(self) -> float:
        if self._playback_reader is not None and self._playback_reader.sixk.online:
            return min(0.35, max(self.config.poll_seconds, self.config.media_poll_seconds))
        return max(self.config.poll_seconds, self.config.media_poll_seconds)

    async def _poll_playback(self) -> Playback | None:
        now = time.monotonic()
        event_ready = (
            self._playback_reader is not None
            and self._playback_reader.has_pending_event()
        )
        if now < self._next_media_poll and not event_ready:
            return self._cached_playback

        self._next_media_poll = now + self._effective_media_interval()
        try:
            result = await self._reader()
        except Exception as exc:  # noqa: BLE001 - media read error must not drop HID
            if now - self._last_media_warn_at >= 5.0:
                self._last_media_warn_at = now
                log.warning("playback read failed: %s", exc)
            self._set_status(last_error=str(exc))
            return self._cached_playback

        self._cached_playback = result if isinstance(result, Playback) else None
        return self._cached_playback

    def _persist_track_offset(self, track_key: str, offset: float) -> None:
        if self.settings is None or not track_key:
            return
        self.settings.set_track_offset(track_key, offset)
        self.settings.save()

    async def _tick(self, loop: asyncio.AbstractEventLoop) -> None:
        assert self._engine is not None
        playback = await self._poll_playback()

        with self._lock:
            nudges, self._nudge_queue = self._nudge_queue, []
            next_line = self._next_line_requested
            self._next_line_requested = False
            jump_index = self._jump_line_index
            self._jump_line_index = None
            reset_offset = self._reset_offset_requested
            self._reset_offset_requested = False

        if playback is not None and playback.valid:
            if playback.track_key != self._engine.clock.track_key:
                saved_offset = (
                    self.settings.get_track_offset(playback.track_key)
                    if self.settings is not None else 0.0
                )
                self._engine.clock.offset = saved_offset
                try:
                    lyrics = await loop.run_in_executor(None, self._load_lyrics, playback)
                except Exception as exc:  # noqa: BLE001 - lyric error must not drop HID
                    log.warning("lyric lookup failed: %s", exc)
                    lyrics = Lyrics(title=playback.title, artist=playback.artist, source="error")
                with self._lock:
                    self._active_lyrics = lyrics
                    self._engine.set_lyrics(lyrics)
                self._set_status(
                    lyrics_source=lyrics.source,
                    lyric_count=len(lyrics.lines),
                    clock_offset=saved_offset,
                )
                log.info("track: %s - %s (%d lines, %s, offset=%+.1fs)",
                         playback.title, playback.artist, len(lyrics.lines),
                         lyrics.source, saved_offset)

        offset_changed = False
        if reset_offset:
            delta = -self._engine.clock.offset
            self._engine.clock.offset = 0.0
            self._engine.clock.nudge(delta)
            self._engine.force_redraw()
            offset_changed = True
            log.info("clock offset reset to 0.0s")
        for seconds in nudges:
            self._engine.clock.offset = max(-30.0, min(30.0, self._engine.clock.offset + seconds))
            self._engine.clock.nudge(seconds)
            self._engine.force_redraw()
            offset_changed = True
            log.info("clock nudged %+.1fs (offset %+.1fs)", seconds, self._engine.clock.offset)
        if next_line:
            if self._align_to_next_line():
                offset_changed = True
        if jump_index is not None:
            if self._align_to_line_index(jump_index):
                offset_changed = True
        if offset_changed and playback is not None and playback.valid:
            self._persist_track_offset(playback.track_key, self._engine.clock.offset)

        self._engine.tick(playback)

        smtc_health = "custom"
        if self._playback_reader is not None:
            mode = str(self._playback_reader.smtc.diagnostics().get("mode", "ok"))
            smtc_health = mode if mode in ("backoff", "circuit-open") else "ok"

        if playback is None or not playback.valid:
            self._set_status(
                source="none", track="", artist="", track_key="",
                position=None, duration=None, playing=False, line="",
                display_text=self._engine.current_chunk,
                display_mode=self._engine.display_mode,
                display_scene=self._engine.display_scene,
                lyric_index=-1, smtc_health=smtc_health, state="idle",
            )
            return
        line = self._engine._current_line  # noqa: SLF001 - same package, read-only status
        reported_pos = (
            self._engine.clock.position if playback.position is not None else self._engine.clock.position
        )
        self._set_status(
            source=playback.aumid.split(":")[0] if playback.aumid else "smtc",
            track=playback.title,
            artist=playback.artist,
            track_key=playback.track_key,
            position=reported_pos,
            duration=playback.duration,
            playing=playback.playing,
            line=line.text if line else "",
            display_text=self._engine.current_chunk,
            display_mode=self._engine.display_mode,
            display_scene=self._engine.display_scene,
            clock_offset=round(self._engine.clock.offset, 2),
            lyric_index=self._engine.current_line_index,
            lyric_count=len(self._engine.lyrics.lines),
            smtc_health=smtc_health,
            state="following" if playback.playing else "paused",
        )

    def _load_lyrics(self, playback: Playback) -> Lyrics:
        override_id = (
            self.settings.get_track_song_id(playback.track_key)
            if self.settings is not None else ""
        )
        song_id = override_id or playback.song_id
        return self._lyrics_client.lyrics_for(
            playback.title, playback.artist, playback.duration or 0.0, song_id)

    def _align_to_next_line(self) -> bool:
        """Move the clock to the next line's start (the "lyrics are behind" fix)."""
        engine = self._engine
        if engine is None or not engine.lyrics:
            return False
        position = engine.clock.position
        upcoming = next((line for line in engine.lyrics.lines if line.start > position + 0.3), None)
        if upcoming is None:
            return False
        delta = upcoming.start - position
        engine.clock.offset = max(-30.0, min(30.0, engine.clock.offset + delta))
        engine.clock.nudge(delta)
        engine.force_redraw()
        log.info("aligned to next line at %.1fs (%+.1fs)", upcoming.start, delta)
        return True

    def _align_to_line_index(self, index: int) -> bool:
        engine = self._engine
        if engine is None or not engine.lyrics or not (0 <= index < len(engine.lyrics.lines)):
            return False
        target = engine.lyrics.lines[index]
        delta = target.start - engine.clock.position
        engine.clock.offset = max(-30.0, min(30.0, engine.clock.offset + delta))
        engine.clock.nudge(delta)
        engine.force_redraw()
        log.info("jumped to line #%d at %.1fs (%+.1fs)", index, target.start, delta)
        return True

    # ------------------------------------------------------------------ helpers
    def _restore_now(self) -> None:
        if self._screen is None or self._snapshot is None:
            return
        try:
            self._screen.restore(self._snapshot)
            log.info("display restored")
        except (RenderError, OSError) as exc:
            log.warning("restore failed: %s", exc)
            self._set_status(last_error=f"restore failed: {exc}")
        finally:
            self._drop_device()

    def _drop_device(self) -> None:
        if self._screen is not None:
            try:
                self._screen.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                pass
        self._screen = None
        self._set_status(connected=False)

    def _set_status(self, **changes: object) -> None:
        with self._lock:
            self._status = replace(self._status, updated_at=time.time(), **changes)  # type: ignore[arg-type]


def path_for_cache():
    from .settings import cache_dir

    return cache_dir()
