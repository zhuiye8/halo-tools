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

    NetEase (and some other clients) publish an SMTC session with an all-zero
    timeline.  When that happens we accumulate wall-clock time while the session
    reports ``playing``.  Pauses freeze the estimate; a new track resets it.
    """

    position: float = 0.0
    playing: bool = False
    track_key: str = ""
    offset: float = 0.0
    _last_tick: float = field(default_factory=time.monotonic, repr=False)

    def reset(self, position: float = 0.0) -> None:
        self.position = max(0.0, position) + self.offset
        self._last_tick = time.monotonic()

    def nudge(self, seconds: float) -> None:
        self.position = max(0.0, self.position + seconds)

    def update(self, playback: Playback) -> float:
        """Feed a playback snapshot and return the estimated position."""
        now = time.monotonic()
        elapsed = now - self._last_tick
        self._last_tick = now

        if playback.track_key != self.track_key:
            self.track_key = playback.track_key
            self.position = self.offset
            self.playing = playback.playing
            return self.position

        if playback.position is not None:
            # The player gives us ground truth; smooth tiny jitter only.
            if abs(playback.position + self.offset - self.position) > 1.5:
                self.position = playback.position + self.offset
            else:
                self.position += (playback.position + self.offset - self.position) * 0.2
        elif playback.playing and self.playing:
            self.position += elapsed * max(0.0, playback.rate or 1.0)
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
    align: Align = Align.CENTER
    scroll: Scroll | None = None
    animation: int | None = None
    standby_scene: int | None = 8          # built-in "waves" scene; None disables
    standby_scene_option: int = 0          # option/template id inside that scene
    standby_after: float = 20.0            # seconds of pause before standby
    empty_text: str = ""
    preferred_aumid: str | None = None
    scroll_long_lines: bool = True         # marquee for lines wider than the screen
    scroll_threshold_units: int = 14       # half-width cells; CJK counts as 2
    segment_fade: bool = True              # fade between chunks of a long line
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
        self._current_chunk: str | None = None
        self._current_scroll: Scroll | None = None
        self._paused_since: float | None = None
        self._in_standby = False
        self._interlude_state = "none"      # none | cleared | scene
        self._snapshot = None
        self._track_key = ""

    # ------------------------------------------------------------- lyric lookup
    def set_lyrics(self, lyrics: Lyrics) -> None:
        self.lyrics = lyrics.with_ends()
        self._current_line = None
        self._current_chunk = None
        self._current_scroll = None
        self._interlude_state = "none"

    # ------------------------------------------------------------------ helpers
    def _render(self, line: LyricLine, position: float) -> None:
        chunks = segment_text(line.text)
        scroll = self.config.scroll
        long_line = (self.config.scroll_long_lines and len(chunks) == 1
                     and display_units(line.text) > self.config.scroll_threshold_units)
        animation = self.config.animation
        if long_line:
            # The firmware scrolls its stored text; one packet is enough because
            # ``segment_text`` only produced a single chunk.
            chunk = line.text
            scroll = Scroll.RIGHT_TO_LEFT
            animation = None
        else:
            chunk = chunks[segment_index(line, position, len(chunks))]
            if animation is None and self.config.segment_fade and len(chunks) > 1:
                # Long lines change chunk abruptly; borrow the device's fade-in
                # animation so the switch does not look like a glitch.
                animation = 2
        if chunk == self._current_chunk and scroll == self._current_scroll:
            return
        self._current_chunk = chunk
        self._current_scroll = scroll
        self.screen.show_text(
            chunk,
            align=self.config.align if scroll is None else None,
            scroll=scroll,
            animation=animation if scroll is None else None,
        )
        log.info("line %.1fs [%d/%d]%s%s: %s", line.start,
                 segment_index(line, position, len(chunks)) + 1, len(chunks),
                 " scroll" if scroll else "", " fade" if animation == 2 and
                 self.config.animation is None else "", chunk)

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
                        log.info("interlude %.0fs -> scene %d", gap, self.config.standby_scene)
                    except RenderError as exc:
                        log.warning("interlude scene failed: %s", exc)
                else:
                    self.screen.show_text("", align=self.config.align)
        elif gap >= self.config.interlude_clear_after:
            if self._interlude_state != "cleared":
                self._interlude_state = "cleared"
                self._current_chunk = ""
                self._current_scroll = None
                self.screen.show_text("", align=self.config.align)
                log.info("interlude %.0fs -> cleared", gap)

    def _enter_standby(self) -> None:
        if self._in_standby:
            return
        self._in_standby = True
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
        line = self.lyrics.line_at(position)
        if line is not None and position < line.end:
            self._leave_interlude()
            self._current_line = line
            self._render(line, position)
        else:
            self._handle_interlude(position)

    # ---------------------------------------------------------------------- run
    async def run(self, duration: float | None = None, reader=None) -> None:
        """Run the loop until ``duration`` elapses (or forever)."""
        from .media import read_playback

        reader = reader or read_playback
        started = time.monotonic()
        self._snapshot = self.screen.snapshot()
        try:
            while True:
                playback = await reader(self.config.preferred_aumid)
                self.tick(playback)
                if duration is not None and time.monotonic() - started >= duration:
                    break
                await asyncio.sleep(self.config.poll_seconds)
        finally:
            if self._snapshot is not None:
                try:
                    self.screen.restore(self._snapshot)
                    log.info("screen restored")
                except RenderError as exc:
                    log.error("restore failed: %s", exc)