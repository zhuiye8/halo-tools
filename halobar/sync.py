"""Playback clock, lyric segmentation and the synchronisation engine.

The engine is deliberately small and framework free: it takes a *screen* (anything
with ``show_text``/``show_scene``/``snapshot``/``restore``), a *playback source*
returning :class:`halobar.media.Playback`, and a *lyrics provider*.  That makes the
whole pipeline testable without hardware.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Protocol

from .device import protocol
from .device.screen import Align, RenderError, Screen, Scroll
from .lyrics import Lyrics, LyricLine
from .media import Playback

log = logging.getLogger(__name__)


# --------------------------------------------------------------------- slicing
def segment_text(text: str, max_bytes: int = protocol.MAX_TEXT_BYTES,
                 max_chars: int | None = None) -> list[str]:
    """Split ``text`` into chunks that fit one device text packet.

    The firmware's real budget is **bytes** (50 on this build; we use 48).  A
    character-count limit would wrongly slice latin lyrics ("...lying kin" + "d"),
    so ``max_chars`` is opt-in and disabled by default.  Splits only on code-point
    boundaries, so the firmware never receives invalid UTF-8.
    """
    if not text:
        return [""]
    chunks: list[str] = []
    current: list[str] = []
    used = 0
    for char in text:
        size = len(char.encode("utf-8"))
        if size > max_bytes:
            continue
        too_many_chars = max_chars is not None and len(current) + 1 > max_chars
        if used + size > max_bytes or too_many_chars:
            chunks.append("".join(current))
            current, used = [], 0
        current.append(char)
        used += size
    if current:
        chunks.append("".join(current))
    return chunks or [""]


def segment_index(line: LyricLine, position: float, count: int) -> int:
    """Pick the chunk that should be visible at ``position`` inside ``line``."""
    if count <= 1:
        return 0
    span = max(0.001, line.end - line.start)
    fraction = min(max((position - line.start) / span, 0.0), 0.999999)
    return min(count - 1, int(fraction * count))


def display_units(text: str) -> int:
    """Rough on-screen width of ``text`` in half-width cells (CJK counts as 2)."""
    return sum(2 if ord(char) > 0x2E7F else 1 for char in text)


# ------------------------------------------------------------------------ clock
@dataclass
class ProgressClock:
    """Estimates play position, preferring the player's own timeline when present.

    Supports decoupled sampling: a playback source may be sampled at 0.5-1.0 s
    while the render loop ticks at 100 ms.  Between fresh samples (identified by
    ``playback.observed_at`` or a changed ``playback.position``), the clock
    interpolates forward smoothly using monotonic wall-clock time while playing,
    and freezes while paused.
    """

    position: float = 0.0
    playing: bool = False
    track_key: str = ""
    offset: float = 0.0
    _last_tick: float = field(default_factory=time.monotonic, repr=False)
    _last_observed_at: float | None = field(default=None, repr=False)
    _last_reported_pos: float | None = field(default=None, repr=False)

    def reset(self, position: float = 0.0) -> None:
        self.position = max(0.0, position + self.offset)
        self._last_tick = time.monotonic()
        self._last_observed_at = None
        self._last_reported_pos = None

    def nudge(self, seconds: float) -> None:
        self.position = max(0.0, self.position + seconds)

    def update(self, playback: Playback) -> float:
        """Feed a playback snapshot (fresh or cached) and return the estimated position."""
        now = time.monotonic()
        elapsed = max(0.0, now - self._last_tick)
        self._last_tick = now
        rate = max(0.0, playback.rate or 1.0)

        if playback.track_key != self.track_key:
            self.track_key = playback.track_key
            self.playing = playback.playing
            self._last_observed_at = playback.observed_at
            self._last_reported_pos = playback.position
            if playback.position is not None:
                age = max(0.0, now - playback.observed_at) if playback.playing else 0.0
                self.position = max(0.0, playback.position + age * rate + self.offset)
            else:
                self.position = max(0.0, self.offset)
            if playback.duration and self.position > playback.duration:
                self.position = playback.duration
            return self.position

        if playback.position is not None:
            fresh = (
                playback.observed_at != self._last_observed_at
                or playback.position != self._last_reported_pos
            )
            if fresh:
                self._last_observed_at = playback.observed_at
                self._last_reported_pos = playback.position
                age = max(0.0, now - playback.observed_at) if playback.playing else 0.0
                target = max(0.0, playback.position + age * rate + self.offset)
                if abs(target - self.position) > 0.75:
                    self.position = target
                else:
                    self.position += (target - self.position) * 0.25
            elif playback.playing and self.playing:
                self.position += elapsed * rate
        elif playback.playing and self.playing:
            self._last_observed_at = playback.observed_at
            self._last_reported_pos = None
            self.position += elapsed * rate

        if playback.duration and self.position > playback.duration:
            self.position = playback.duration
        self.playing = playback.playing
        return self.position


# ----------------------------------------------------------------------- engine
class ScreenLike(Protocol):
    def show_text(self, text: str, **kwargs: object) -> str: ...
    def show_scene(self, category: int, option: int = 0, template: int = 0xFF) -> None: ...
    def snapshot(self): ...
    def restore(self, snapshot) -> None: ...


@dataclass
class EngineConfig:
    """User-tunable behaviour of the sync engine."""

    poll_seconds: float = 0.1
    media_poll_seconds: float = 0.5
    align: Align = Align.CENTER
    scroll: Scroll | None = None
    animation: int | None = None
    color: tuple[int, int, int] | None = None
    standby_scene: int | None = 8          # built-in "waves" scene; None disables
    standby_scene_option: int = 0          # option/template id inside that scene
    standby_after: float = 20.0            # seconds of pause before standby
    empty_text: str = ""
    preferred_aumid: str | None = None
    scroll_long_lines: bool = True         # keep for compatibility; see line_mode
    scroll_threshold_units: int = 32       # half-width cells; CJK counts as 2
    line_mode: str = "page"                # page | scroll | first
    lyric_lang: str = "original"           # original | translation | bilingual
    page_units: int = 32                   # characters per page (half-width cells)
    scroll_lead_spaces: int = 2            # delay before the first character scrolls in
    segment_fade: bool = True              # fade between pages of a long line
    interlude_clear_after: float = 4.0     # silence longer than this clears the text
    interlude_scene_after: float = 10.0    # longer than this falls back to a scene


class LyricsEngine:
    """Drives the screen from a media session and a lyric document."""

    def __init__(self, screen: ScreenLike, config: EngineConfig | None = None) -> None:
        self.screen = screen
        self.config = config or EngineConfig()
        self.clock = ProgressClock()
        self.lyrics: Lyrics = Lyrics()
        self._current_line: LyricLine | None = None
        self._current_line_index: int = -1
        self._current_chunk: str | None = None
        self._current_scroll: Scroll | None = None
        self._display_mode: str = "idle"    # idle | text | cleared | scene | standby
        self._display_scene: int | None = None
        self._paused_since: float | None = None
        self._in_standby = False
        self._interlude_state = "none"      # none | cleared | scene
        self._snapshot = None
        self._track_key = ""

    @property
    def current_chunk(self) -> str:
        return self._current_chunk or ""

    @property
    def current_line_index(self) -> int:
        return self._current_line_index

    @property
    def display_mode(self) -> str:
        return self._display_mode

    @property
    def display_scene(self) -> int | None:
        return self._display_scene

    # ------------------------------------------------------------- lyric lookup
    def set_lyrics(self, lyrics: Lyrics) -> None:
        self.lyrics = lyrics.with_ends()
        self._current_line = None
        self._current_line_index = -1
        self._current_chunk = None
        self._current_scroll = None
        self._interlude_state = "none"

    def force_redraw(self) -> None:
        self._current_chunk = None
        self._current_scroll = None

    # ------------------------------------------------------------------ paging
    def _pages(self, text: str) -> list[str]:
        """Split a line into screen-sized pages (byte and width limits)."""
        pages: list[str] = []
        current: list[str] = []
        used_bytes = 0
        used_units = 0
        for char in text:
            size = len(char.encode("utf-8"))
            units = 2 if ord(char) > 0x2E7F else 1
            if (used_bytes + size > protocol.MAX_TEXT_BYTES
                    or used_units + units > self.config.page_units):
                pages.append("".join(current))
                current = []
                used_bytes = 0
                used_units = 0
            current.append(char)
            used_bytes += size
            used_units += units
        if current:
            pages.append("".join(current))
        return pages or [""]

    @staticmethod
    def _page_at(line: LyricLine, position: float, pages: list[str]) -> int:
        """Pick the page by elapsed fraction, weighted by each page's width."""
        if len(pages) <= 1:
            return 0
        weights = [max(1, display_units(page)) for page in pages]
        total = sum(weights)
        span = max(0.001, line.end - line.start)
        fraction = min(max((position - line.start) / span, 0.0), 0.999999)
        target = fraction * total
        seen = 0
        for index, weight in enumerate(weights):
            seen += weight
            if target < seen:
                return index
        return len(pages) - 1

    def _resolve_line_text(self, line: LyricLine, position: float) -> tuple[str, bool]:
        """Select text according to ``lyric_lang``; returns ``(text, switched_lang)``."""
        lang = self.config.lyric_lang
        has_trans = bool(line.translation and line.translation.strip()
                         and line.translation.strip() != line.text.strip())
        if lang == "translation" and has_trans:
            return line.translation, False
        if lang == "bilingual" and has_trans:
            span = max(0.001, line.end - line.start)
            fraction = min(max((position - line.start) / span, 0.0), 0.999999)
            if fraction >= 0.55:
                return line.translation, True
            return line.text, True
        return line.text, False

    def _render(self, line: LyricLine, position: float) -> None:
        active_text, bilingual_split = self._resolve_line_text(line, position)
        pages = self._pages(active_text)
        mode = self.config.line_mode
        scroll = self.config.scroll
        animation = self.config.animation
        index = 0

        hardware_scroll = (
            mode == "scroll" and len(active_text.encode("utf-8")) <= protocol.MAX_TEXT_BYTES
        )
        if hardware_scroll:
            # Lead-in spaces delay the first character; the firmware then scrolls at
            # its own pace, so this stays an opt-in mode.
            chunk = " " * max(0, self.config.scroll_lead_spaces) + active_text
            scroll = Scroll.RIGHT_TO_LEFT
            animation = None
        else:
            index = 0 if mode == "first" else self._page_at(line, position, pages)
            chunk = pages[index]
            if animation is None and self.config.segment_fade and (len(pages) > 1 or bilingual_split):
                animation = 2

        if chunk == self._current_chunk and scroll == self._current_scroll:
            return
        self._current_chunk = chunk
        self._current_scroll = scroll
        self._display_mode = "text"
        self._display_scene = None
        kwargs: dict[str, object] = {
            "align": self.config.align if scroll is None else None,
            "scroll": scroll,
            "animation": animation if scroll is None else None,
        }
        if self.config.color is not None:
            kwargs["color"] = self.config.color
        self.screen.show_text(chunk, **kwargs)
        log.info("line %.1fs page %d/%d%s%s: %s", line.start, index + 1, len(pages),
                 " scroll" if scroll else "",
                 " fade" if animation == 2 and self.config.animation is None else "",
                 chunk.strip() or chunk)

    # ------------------------------------------------------------- interlude
    def _leave_interlude(self) -> None:
        if self._interlude_state != "none":
            self._interlude_state = "none"
            self._current_chunk = None
            self._current_scroll = None
            log.debug("interlude ended")

    def _handle_interlude(self, position: float) -> None:
        """Silence between lines: keep, clear, or fall back to a scene."""
        upcoming = next((line for line in self.lyrics.lines if line.start > position), None)
        gap = (upcoming.start - position) if upcoming is not None else float("inf")

        if gap >= self.config.interlude_scene_after:
            if self._interlude_state != "scene":
                self._interlude_state = "scene"
                self._current_chunk = ""
                self._current_scroll = None
                if self.config.standby_scene is not None:
                    try:
                        self.screen.show_scene(self.config.standby_scene, option=self.config.standby_scene_option)
                        self._display_mode = "scene"
                        self._display_scene = self.config.standby_scene
                        log.info("interlude %.0fs -> scene %d", gap, self.config.standby_scene)
                    except RenderError as exc:
                        log.warning("interlude scene failed: %s", exc)
                else:
                    self.screen.show_text("", align=self.config.align)
                    self._display_mode = "cleared"
                    self._display_scene = None
        elif gap >= self.config.interlude_clear_after:
            if self._interlude_state != "cleared":
                self._interlude_state = "cleared"
                self._current_chunk = ""
                self._current_scroll = None
                self._display_mode = "cleared"
                self._display_scene = None
                self.screen.show_text("", align=self.config.align)
                log.info("interlude %.0fs -> cleared", gap)

    def _enter_standby(self) -> None:
        if self._in_standby:
            return
        self._in_standby = True
        self._current_chunk = ""
        self._display_mode = "standby"
        self._display_scene = self.config.standby_scene
        if self.config.standby_scene is not None:
            try:
                self.screen.show_scene(self.config.standby_scene, option=self.config.standby_scene_option)
                log.info("paused -> standby scene %d", self.config.standby_scene)
            except RenderError as exc:
                log.warning("standby scene failed: %s", exc)

    def _leave_standby(self) -> None:
        if not self._in_standby:
            return
        self._in_standby = False
        self._current_chunk = None
        self._current_scroll = None
        log.info("resumed -> lyrics")

    # --------------------------------------------------------------------- tick
    def tick(self, playback: Playback | None) -> None:
        """Advance one step.  Safe to call at a fixed rate (100 ms)."""
        if playback is None or not playback.valid:
            self._enter_standby()
            return

        if playback.track_key != self._track_key:
            self._track_key = playback.track_key
            self._current_line = None
            self._current_line_index = -1
            self._current_chunk = None
            self._current_scroll = None
            self._interlude_state = "none"
            self._paused_since = None
            self._leave_standby()

        position = self.clock.update(playback)
        if not playback.playing:
            self._paused_since = self._paused_since or time.monotonic()
            if time.monotonic() - self._paused_since >= self.config.standby_after:
                self._enter_standby()
            return
        self._paused_since = None
        self._leave_standby()

        if not self.lyrics:
            return
        line_idx = -1
        line: LyricLine | None = None
        for idx, candidate in enumerate(self.lyrics.lines):
            if candidate.start <= position:
                line_idx = idx
                line = candidate
            else:
                break
        if line is not None and position < line.end:
            self._leave_interlude()
            self._current_line = line
            self._current_line_index = line_idx
            self._render(line, position)
        else:
            self._current_line = None
            self._current_line_index = -1
            self._handle_interlude(position)

    # ---------------------------------------------------------------------- run
    async def run(self, duration: float | None = None, reader=None) -> None:
        """Run the loop until ``duration`` elapses (or forever).

        Decouples media polling (``media_poll_seconds``) from rendering ticks
        (``poll_seconds``) so the CLI demo does not hammer SMTC at 10 Hz either.
        """
        from .media import PlaybackReader

        owned_reader = PlaybackReader(smtc_interval=self.config.media_poll_seconds) if reader is None else None
        read_fn = reader or owned_reader.read
        started = time.monotonic()
        next_poll = 0.0
        cached: Playback | None = None
        self._snapshot = self.screen.snapshot()
        try:
            while True:
                now = time.monotonic()
                event_ready = owned_reader is not None and owned_reader.has_pending_event()
                if now >= next_poll or event_ready:
                    cached = await read_fn(self.config.preferred_aumid)
                    next_poll = time.monotonic() + max(self.config.poll_seconds, self.config.media_poll_seconds)
                self.tick(cached)
                if duration is not None and time.monotonic() - started >= duration:
                    break
                await asyncio.sleep(self.config.poll_seconds)
        finally:
            if owned_reader is not None:
                owned_reader.close()
            if self._snapshot is not None:
                try:
                    self.screen.restore(self._snapshot)
                    log.info("screen restored")
                except RenderError as exc:
                    log.error("restore failed: %s", exc)
