"""Segmentation, clock and engine tests using a fake screen."""

from __future__ import annotations

from halobar.device import protocol
from halobar.device.screen import Align
from halobar.lyrics import Lyrics, LyricLine
from halobar.media import Playback
from halobar.sync import (
    EngineConfig,
    LyricsEngine,
    ProgressClock,
    segment_index,
    segment_text,
)


def test_segment_text_respects_byte_limit():
    text = "一二三四五六七八九十一二三四五六七八"  # 18 chars, 54 bytes
    chunks = segment_text(text)
    assert all(len(chunk.encode("utf-8")) <= protocol.MAX_TEXT_BYTES for chunk in chunks)
    assert "".join(chunks) == text
    assert [len(chunk) for chunk in chunks] == [16, 2]


def test_segment_text_keeps_latin_lines_whole():
    # 40 ASCII chars fit in 48 bytes and must not be sliced mid-word.
    line = "And all the things that I read there"
    assert segment_text(line) == [line]
    assert segment_text("abcdefghij") == ["abcdefghij"]


def test_segment_index_distributes_over_line():
    line = LyricLine(10.0, "x" * 40, 14.0)
    assert segment_index(line, 10.1, 3) == 0
    assert segment_index(line, 12.0, 3) == 1
    assert segment_index(line, 13.9, 3) == 2
    assert segment_index(line, 10.0, 1) == 0


def test_clock_prefers_player_timeline():
    clock = ProgressClock()
    playing = Playback(title="t", artist="a", playing=True, position=30.0, duration=200.0)
    # Starting mid-song immediately aligns to the reported position instead of 0.0.
    assert abs(clock.update(playing) - 30.0) < 0.05
    assert abs(clock.update(playing) - 30.0) < 0.05


def test_clock_interpolates_cached_snapshot_between_polls():
    clock = ProgressClock()
    snapshot = Playback(title="t", artist="a", playing=True, position=10.0, duration=200.0)
    assert abs(clock.update(snapshot) - 10.0) < 0.05

    # Simulate three 100ms render ticks reusing the same cached snapshot.
    for expected in (10.1, 10.2, 10.3):
        clock._last_tick -= 0.1
        pos = clock.update(snapshot)
        assert abs(pos - expected) < 0.03

    # Pausing freezes interpolation even if time passes on the cached snapshot.
    paused = Playback(title="t", artist="a", playing=False, position=10.3, duration=200.0)
    frozen = clock.update(paused)
    clock._last_tick -= 1.0
    assert clock.update(paused) == frozen


def test_clock_accumulates_without_timeline():
    clock = ProgressClock()
    first = Playback(title="t", artist="a", playing=True, position=None)
    clock.update(first)
    clock._last_tick -= 5.0  # pretend five seconds passed
    assert abs(clock.update(first) - 5.0) < 0.05


def test_clock_freezes_while_paused():
    clock = ProgressClock()
    playing = Playback(title="t", artist="a", playing=True, position=None)
    clock.update(playing)
    clock._last_tick -= 2.0
    clock.update(playing)
    paused = Playback(title="t", artist="a", playing=False, position=None)
    before = clock.update(paused)
    clock._last_tick -= 3.0
    assert clock.update(paused) == before


class FakeScreen:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.scenes: list[int] = []

    def show_text(self, text: str, **kwargs: object) -> str:
        self.texts.append(text)
        return text

    def show_scene(self, category: int, option: int = 0, template: int = 0xFF) -> None:
        self.scenes.append(category)

    def snapshot(self) -> object:
        return "snapshot"

    def restore(self, snapshot: object) -> None:
        self.restored = snapshot


class RecordingScreen(FakeScreen):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict] = []

    def show_text(self, text: str, **kwargs: object) -> str:
        self.calls.append({"text": text, **kwargs})
        return super().show_text(text, **kwargs)


def test_long_line_is_paged_by_default():
    screen = RecordingScreen()
    engine = LyricsEngine(screen, EngineConfig())  # page mode, page_units=32
    long_line = "这是一句特别特别长的歌词用来测试分页效果"  # 20 CJK == 40 units / 60 bytes
    engine.set_lyrics(Lyrics([LyricLine(0.0, "短句", 1.0),
                              LyricLine(1.0, long_line, 5.0)]))

    engine.tick(Playback(title="t", artist="a", playing=True, position=0.5))
    assert screen.calls[-1]["scroll"] is None

    # 20 CJK chars -> two pages (16 + 4), shown as static text, not a marquee.
    engine.tick(Playback(title="t", artist="a", playing=True, position=1.5))
    first_page = screen.calls[-1]["text"]
    assert screen.calls[-1]["scroll"] is None
    assert first_page == long_line[:16]
    assert screen.calls[-1]["animation"] == 2  # fade between pages

    engine.tick(Playback(title="t", artist="a", playing=True, position=4.5))
    assert screen.calls[-1]["text"] == long_line[16:]


def test_scroll_mode_prepends_lead_spaces():
    screen = RecordingScreen()
    engine = LyricsEngine(screen, EngineConfig(line_mode="scroll", scroll_lead_spaces=2))
    medium = "一二三四五六七八九十一二三"  # 13 CJK == 39 bytes, fits one packet
    engine.set_lyrics(Lyrics([LyricLine(1.0, medium, 5.0)]))
    engine.tick(Playback(title="t", artist="a", playing=True, position=2.0))  # registers the track
    engine.tick(Playback(title="t", artist="a", playing=True, position=2.0))
    assert screen.calls[-1]["scroll"] is not None
    assert screen.calls[-1]["text"] == "  " + medium


def test_first_page_mode_never_switches():
    screen = RecordingScreen()
    engine = LyricsEngine(screen, EngineConfig(line_mode="first"))
    long_line = "这是一句特别特别长的歌词用来测试分页效果"
    engine.set_lyrics(Lyrics([LyricLine(0.0, long_line, 5.0)]))
    engine.tick(Playback(title="t", artist="a", playing=True, position=0.5))
    first = screen.calls[-1]["text"]
    engine.tick(Playback(title="t", artist="a", playing=True, position=4.5))
    assert screen.calls[-1]["text"] == first
    assert first == long_line[:16]


def test_interlude_clears_then_falls_back_to_scene():
    screen = FakeScreen()
    engine = LyricsEngine(screen, EngineConfig(
        standby_after=999, interlude_clear_after=4.0, interlude_scene_after=10.0,
        scroll_long_lines=False, segment_fade=False))
    engine.set_lyrics(Lyrics([
        LyricLine(0.0, "第一句", 2.0),
        LyricLine(30.0, "第二句", 32.0),
    ]))

    engine.tick(Playback(title="t", artist="a", playing=True, position=1.0))
    assert screen.texts[-1] == "第一句"

    # 5 s to the next line -> clear the text, stay on the text layer.
    engine.tick(Playback(title="t", artist="a", playing=True, position=25.0))
    assert screen.texts[-1] == ""
    assert screen.scenes == []

    # 2 s to the next line -> keep the cleared state (no scene yet)
    engine.tick(Playback(title="t", artist="a", playing=True, position=28.0))
    assert screen.texts[-1] == ""
    assert screen.scenes == []

    # next line starts -> renders again
    engine.tick(Playback(title="t", artist="a", playing=True, position=30.5))
    assert screen.texts[-1] == "第二句"


def test_long_interlude_switches_to_standby_scene():
    screen = FakeScreen()
    engine = LyricsEngine(screen, EngineConfig(
        standby_after=999, interlude_clear_after=4.0, interlude_scene_after=10.0,
        standby_scene=8, scroll_long_lines=False, segment_fade=False))
    engine.set_lyrics(Lyrics([
        LyricLine(0.0, "第一句", 2.0),
        LyricLine(40.0, "第二句", 42.0),
    ]))
    engine.tick(Playback(title="t", artist="a", playing=True, position=1.0))
    engine.tick(Playback(title="t", artist="a", playing=True, position=5.0))   # 35 s gap
    assert screen.scenes == [8]
    engine.tick(Playback(title="t", artist="a", playing=True, position=40.5))
    assert screen.texts[-1] == "第二句"


def test_engine_writes_changing_lines_and_standby():
    screen = FakeScreen()
    engine = LyricsEngine(screen, EngineConfig(standby_after=0.0, standby_scene=8))
    engine.set_lyrics(Lyrics([
        LyricLine(0.0, "第一句", 2.0),
        LyricLine(2.0, "第二句", 4.0),
    ]))
    engine.tick(Playback(title="t", artist="a", playing=True, position=None))
    engine.clock._last_tick -= 2.1
    engine.tick(Playback(title="t", artist="a", playing=True, position=None))
    assert screen.texts[:2] == ["第一句", "第二句"]

    engine.tick(Playback(title="t", artist="a", playing=False, position=None))
    engine.clock._last_tick -= 0.1
    engine.tick(Playback(title="t", artist="a", playing=False, position=None))
    assert screen.scenes == [8]