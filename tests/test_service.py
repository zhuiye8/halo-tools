"""Service tests with a fake screen and fake playback source (no hardware)."""

from __future__ import annotations

import time

from halobar.lyrics import Lyrics, LyricLine
from halobar.media import Playback
from halobar.service import LyricsService, ServiceConfig


class FakeScreen:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.scenes: list[int] = []
        self.restored = 0
        self.closed = False
        self.firmware = "1.1.1-fake"

    def show_text(self, text: str, **kwargs: object) -> str:
        self.texts.append(text)
        return text

    def show_scene(self, category: int, option: int = 0, template: int = 0xFF) -> None:
        self.scenes.append(category)

    def snapshot(self) -> object:
        return {"state": "fake"}

    def restore(self, snapshot: object) -> None:
        self.restored += 1

    def close(self) -> None:
        self.closed = True


class FakeLyricsClient:
    def __init__(self) -> None:
        self.calls = 0

    def lyrics_for(self, title, artist, duration=0.0, song_id="") -> Lyrics:
        self.calls += 1
        return Lyrics([
            LyricLine(0.0, "第一句", 2.0),
            LyricLine(2.0, "第二句", 4.0),
        ], title=title, artist=artist, source="fake")


def make_reader():
    state = {"pos": 0.0}

    async def reader(preferred=None, use_sixk=True):
        state["pos"] += 0.05
        return Playback(
            title="测试歌曲", artist="测试歌手", playing=True,
            position=state["pos"], duration=200.0, aumid="6k-labs:1", song_id="1")

    return reader


def test_service_writes_text_and_restores_on_stop():
    screen = FakeScreen()
    service = LyricsService(
        ServiceConfig(poll_seconds=0.02, standby_after=0.5),
        screen_factory=lambda: screen,
        reader=make_reader(),
        lyrics_client=FakeLyricsClient(),
    )
    service.start()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not screen.texts:
            time.sleep(0.05)
        status = service.status()
    finally:
        service.stop()

    assert screen.texts, "service should have written a lyric line"
    assert status.connected is True
    assert status.track == "测试歌曲"
    assert screen.restored >= 1
    assert screen.closed is True


def test_restore_suspends_following():
    screen = FakeScreen()
    service = LyricsService(
        ServiceConfig(poll_seconds=0.02),
        screen_factory=lambda: screen,
        reader=make_reader(),
        lyrics_client=FakeLyricsClient(),
    )
    service.start()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not screen.texts:
            time.sleep(0.05)
        service.request_restore()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and screen.restored == 0:
            time.sleep(0.05)
        written_at_restore = len(screen.texts)
        time.sleep(0.4)
        assert service.status().state == "restored"
        assert len(screen.texts) == written_at_restore, "no writes after restore"
        service.resume()
        time.sleep(0.3)
        assert len(screen.texts) >= written_at_restore
    finally:
        service.stop()


def test_service_waits_without_device():
    def broken_factory():
        raise OSError("no device")

    service = LyricsService(
        ServiceConfig(poll_seconds=0.02, reconnect_seconds=0.05),
        screen_factory=broken_factory,
        reader=make_reader(),
        lyrics_client=FakeLyricsClient(),
    )
    service.start()
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            status = service.status()
            if status.state == "waiting-device":
                break
            time.sleep(0.05)
        status = service.status()
    finally:
        service.stop()

    assert status.connected is False
    assert status.state == "waiting-device"
    assert "no device" in status.last_error