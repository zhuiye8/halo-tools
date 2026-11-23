"""Background service: owns the device, the playback source and the sync engine.

Everything that touches the speaker goes through :class:`LyricsService`.  The tray
and the web console only call its thread-safe methods, so there is exactly one
writer for the HID device at any time.

Design notes
------------
* One worker thread runs its own asyncio loop: playback polling (6K Labs / SMTC)
  is async, lyric fetching is blocking and runs in an executor.
* The service keeps a :class:`ServiceStatus` snapshot that any thread may read.
* Lyrics are fetched through an injected callable so tests run fully offline.
* On stop, the display snapshot taken at start is restored.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Protocol

from .device.screen import Align, DisplaySnapshot, RenderError, Screen, Scroll
from .lyrics import Lyrics, NeteaseClient
from .media import Playback, read_playback
from .sync import EngineConfig, LyricsEngine, segment_text

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
    position: float | None = None
    duration: float | None = None
    playing: bool = False
    line: str = ""
    lyrics_source: str = ""
    state: str = "idle"
    last_error: str = ""
    upload_state: str = "idle"          # idle | uploading | done | error
    upload_progress: float = 0.0
    asset_name: str = ""
    asset_frames: int = 0
    asset_category: int = 0
    asset_option: int = 0
    asset_time: float = 0.0
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
    standby_scene: int | None = 8
    standby_after: float = 20.0
    poll_seconds: float = 0.1
    lyric_poll_seconds: float = 1.0
    reconnect_seconds: float = 3.0
    scroll_long_lines: bool = True
    scroll_threshold_units: int = 32
    line_mode: str = "page"
    page_units: int = 32
    scroll_lead_spaces: int = 2
    interlude_clear_after: float = 4.0
    interlude_scene_after: float = 10.0
    standby_scene_option: int = 0


class LyricsService:
    """Threaded coordinator between playback, lyrics and the pixel display."""

    def __init__(
        self,
        config: ServiceConfig | None = None,
        *,
        screen_factory: ScreenFactory | None = None,
        reader: Callable[..., object] = read_playback,
        lyrics_client: NeteaseClient | None = None,
    ) -> None:
        self.config = config or ServiceConfig()
        self._screen_factory = screen_factory or (lambda: Screen())
        self._reader = reader
        self._lyrics_client = lyrics_client or NeteaseClient(
            path_for_cache())
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._suspended = threading.Event()
        self._restore_requested = threading.Event()
        self._status = ServiceStatus()
        self._engine: LyricsEngine | None = None
        self._screen: Screen | None = None
        self._snapshot: DisplaySnapshot | None = None
        self._nudge_queue: list[float] = []
        self._next_line_requested = False
        self._upload_job: UploadJob | None = None
        self._uploading = False
        self._scene_request: tuple[int, int] | None = None

    # ------------------------------------------------------------------ control
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="halobar-service", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def pause(self) -> None:
        self._paused.set()
        self._set_status(state="paused")

    def resume(self) -> None:
        self._suspended.clear()
        self._paused.clear()
        self._set_status(state="following")

    def nudge(self, seconds: float) -> None:
        with self._lock:
            self._nudge_queue.append(seconds)

    def nudge_to_next_line(self) -> None:
        """Jump the clock forward so the next lyric line starts now."""
        self._next_line_requested = True

    # ------------------------------------------------------------------- assets
    def submit_upload(self, job: UploadJob) -> None:
        """Queue a bitmap upload; the worker thread performs it (single writer)."""
        with self._lock:
            self._upload_job = job
            self._suspended.clear()
            self._status = replace(self._status, upload_state="queued", upload_progress=0.0)

    def request_scene(self, category: int, option: int) -> None:
        """Switch the screen to a scene (e.g. the uploaded asset) right now."""
        with self._lock:
            self._scene_request = (category, option)

    def _do_upload(self, job: UploadJob) -> None:
        screen = self._screen
        if screen is None:
            raise RenderError("device is not connected")
        screen.upload_bitmap(job.data, category=job.category, option=job.option,
                             progress=lambda value: self._set_status(upload_progress=value))
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
        )
        log.info("asset uploaded: %s (%d frames, scene %d/%d)",
                 job.name, job.frames, job.category, job.option)

    def update_config(self, *, align: str | None = None, animation: int | None | str = "keep",
                      scroll_long_lines: bool | None = None,
                      line_mode: str | None = None,
                      page_units: int | None = None,
                      standby_after: float | None = None) -> None:
        """Apply live settings from the console."""
        with self._lock:
            if align is not None:
                self.config.align = Align(str(align))
            if line_mode is not None:
                if line_mode not in ("page", "scroll", "first"):
                    raise ValueError("line_mode must be page/scroll/first")
                self.config.line_mode = line_mode
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
            engine = self._engine
            if engine is not None:
                engine.config.align = self.config.align
                engine.config.animation = self.config.animation
                engine.config.scroll_long_lines = self.config.scroll_long_lines
                engine.config.line_mode = self.config.line_mode
                engine.config.page_units = self.config.page_units
                engine.config.standby_after = self.config.standby_after
                engine._current_chunk = None  # force a redraw with the new settings
                engine._current_scroll = None

    def request_restore(self) -> None:
        """Restore the original screen and stop following until :meth:`resume`.

        Restoring without suspending was confusing: the next tick immediately wrote
        lyrics again.  "Restore" now means "hand the screen back and stay out of
        the way".
        """
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

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        self._set_status(running=True, state="connecting")
        while not self._stop.is_set():
            if self._suspended.is_set():
                # "Restore" hands the screen back and stays out of the way: no
                # device access at all until resume() is called.
                await asyncio.sleep(0.2)
                continue
            if self._screen is None:
                if not await self._connect(loop):
                    await asyncio.sleep(self.config.reconnect_seconds)
                    continue
            if self._upload_job is not None:
                job, self._upload_job = self._upload_job, None
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
            if self._scene_request is not None:
                category, option = self._scene_request
                self._scene_request = None
                try:
                    self._screen.show_scene(category, option=option)
                    log.info("switched to scene %d/%d", category, option)
                except (RenderError, OSError) as exc:
                    self._set_status(last_error=str(exc))
                await asyncio.sleep(0.1)
                continue
            if self._restore_requested.is_set():
                self._restore_requested.clear()
                self._restore_now()
                self._suspended.set()
                self._set_status(state="restored")
                await asyncio.sleep(0.2)
                continue
            if self._suspended.is_set() or self._paused.is_set():
                await asyncio.sleep(0.2)
                continue
            try:
                await self._tick(loop)
            except Exception as exc:  # noqa: BLE001 - one bad frame must not kill the loop
                log.warning("tick failed: %s", exc)
                self._set_status(last_error=str(exc))
                self._drop_device()
            await asyncio.sleep(self.config.poll_seconds)
        self._restore_now()
        self._set_status(running=False, state="stopped")

    async def _connect(self, loop: asyncio.AbstractEventLoop) -> bool:
        try:
            screen = await loop.run_in_executor(None, self._screen_factory)
            snapshot = await loop.run_in_executor(None, screen.snapshot)
        except (OSError, RenderError, TimeoutError) as exc:
            log.info("device not ready: %s", exc)
            self._set_status(connected=False, state="waiting-device", last_error=str(exc))
            return False
        self._screen = screen
        self._snapshot = snapshot
        self._engine = LyricsEngine(screen, EngineConfig(
            poll_seconds=self.config.poll_seconds,
            align=self.config.align,
            scroll=self.config.scroll,
            animation=self.config.animation,
            standby_scene=self.config.standby_scene,
            standby_scene_option=self.config.standby_scene_option,
            standby_after=self.config.standby_after,
            scroll_long_lines=self.config.scroll_long_lines,
            scroll_threshold_units=self.config.scroll_threshold_units,
            line_mode=self.config.line_mode,
            page_units=self.config.page_units,
            scroll_lead_spaces=self.config.scroll_lead_spaces,
            interlude_clear_after=self.config.interlude_clear_after,
            interlude_scene_after=self.config.interlude_scene_after,
        ))
        log.info("device connected (firmware %s)", screen.firmware)
        self._set_status(connected=True, firmware=screen.firmware, state="following",
                         last_error="")
        return True

    async def _tick(self, loop: asyncio.AbstractEventLoop) -> None:
        assert self._engine is not None
        playback = await self._reader()

        with self._lock:
            nudges, self._nudge_queue = self._nudge_queue, []
            next_line = self._next_line_requested
            self._next_line_requested = False
        for seconds in nudges:
            self._engine.clock.nudge(seconds)
            log.info("clock nudged %+.1fs", seconds)
        if next_line:
            self._align_to_next_line()

        if playback is not None and isinstance(playback, Playback):
            if playback.track_key != self._engine.clock.track_key:
                lyrics = await loop.run_in_executor(None, self._load_lyrics, playback)
                self._engine.set_lyrics(lyrics)
                self._set_status(lyrics_source=lyrics.source)
                log.info("track: %s - %s (%d lines, %s)", playback.title, playback.artist,
                         len(lyrics.lines), lyrics.source)

        self._engine.tick(playback)  # type: ignore[arg-type]

        if playback is None or not isinstance(playback, Playback):
            self._set_status(source="none", track="", artist="", state="idle")
            return
        line = self._engine._current_line  # noqa: SLF001 - same package, read-only status
        self._set_status(
            source=playback.aumid.split(":")[0] if playback.aumid else "smtc",
            track=playback.title,
            artist=playback.artist,
            position=playback.position,
            duration=playback.duration,
            playing=playback.playing,
            line=line.text if line else "",
            state="following" if playback.playing else "paused",
        )

    def _load_lyrics(self, playback: Playback) -> Lyrics:
        return self._lyrics_client.lyrics_for(
            playback.title, playback.artist, playback.duration or 0.0, playback.song_id)

    def _align_to_next_line(self) -> None:
        """Move the clock to the next line's start (the "lyrics are behind" fix)."""
        engine = self._engine
        if engine is None or not engine.lyrics:
            return
        position = engine.clock.position
        upcoming = next((line for line in engine.lyrics.lines if line.start > position + 0.3), None)
        if upcoming is None:
            return
        delta = upcoming.start - position
        engine.clock.nudge(delta)
        engine._current_chunk = None
        engine._current_scroll = None
        log.info("aligned to next line at %.1fs (%+.1fs)", upcoming.start, delta)

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
    from pathlib import Path

    return Path(__file__).resolve().parent.parent / ".cache" / "lyrics"