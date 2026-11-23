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


def test_service_decouples_media_polling_from_render_ticks():
    screen = FakeScreen()
    calls = {"n": 0}

    async def slow_counting_reader():
        calls["n"] += 1
        return Playback(
            title="测试歌曲", artist="测试歌手", playing=True,
            position=0.5, duration=200.0, aumid="smtc",
        )

    service = LyricsService(
        ServiceConfig(poll_seconds=0.02, media_poll_seconds=0.18),
        screen_factory=lambda: screen,
        reader=slow_counting_reader,
        lyrics_client=FakeLyricsClient(),
    )
    service.start()
    try:
        time.sleep(0.35)
    finally:
        service.stop()

    # Over ~350ms at 20ms render ticks (~17 ticks), media reader should only be polled ~2-4 times.
    assert 1 <= calls["n"] <= 5


def test_service_keeps_device_connected_on_media_error():
    screen = FakeScreen()

    async def flaky_reader():
        raise RuntimeError("transient media failure")

    service = LyricsService(
        ServiceConfig(poll_seconds=0.02, media_poll_seconds=0.05),
        screen_factory=lambda: screen,
        reader=flaky_reader,
        lyrics_client=FakeLyricsClient(),
    )
    service.start()
    try:
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and not service.status().connected:
            time.sleep(0.02)
        time.sleep(0.15)
        status = service.status()
        assert status.connected is True
        assert screen.closed is False
        assert "transient media failure" in status.last_error
    finally:
        service.stop()


def test_connect_closes_screen_when_snapshot_fails():
    closed = {"n": 0}

    class BrokenSnapshotScreen(FakeScreen):
        def snapshot(self):
            raise OSError("usb read timeout")

        def close(self):
            closed["n"] += 1
            super().close()

    service = LyricsService(
        ServiceConfig(poll_seconds=0.02, reconnect_seconds=0.2),
        screen_factory=lambda: BrokenSnapshotScreen(),
        reader=make_reader(),
        lyrics_client=FakeLyricsClient(),
    )
    service.start()
    try:
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and closed["n"] == 0:
            time.sleep(0.02)
    finally:
        service.stop()

    assert closed["n"] >= 1


def test_screen_closes_owned_transport_on_handshake_failure(monkeypatch):
    from halobar.device import screen as screen_mod

    closed = {"n": 0}

    class FailingTransport:
        def write(self, packet):
            raise OSError("handshake io error")

        def close(self):
            closed["n"] += 1

    monkeypatch.setattr(screen_mod, "WinHidTransport", FailingTransport)
    try:
        screen_mod.Screen()
    except OSError:
        pass
    assert closed["n"] == 1


def test_service_persists_per_track_offset_and_switches_candidate(tmp_path):
    from halobar.lyrics import SongMatch
    from halobar.settings import Settings

    class CandidateLyricsClient(FakeLyricsClient):
        def search_candidates(self, title, artist="", duration=0.0, limit=8):
            return [SongMatch(999, title, artist, 200000, score=2.2)]

        def fetch_by_id(self, song_id, title="", artist="", duration=0.0):
            return Lyrics([
                LyricLine(0.0, f"候选词-{song_id}", 3.0, "Candidate trans"),
                LyricLine(3.0, "第二句候选", 6.0),
            ], title=title, artist=artist, source="netease")

    cfg_path = tmp_path / "settings.json"
    user_settings = Settings.load(cfg_path)
    # Redirect save() to tmp_path so tests never touch %LOCALAPPDATA%.
    orig_save = user_settings.save
    user_settings.save = lambda path=None: orig_save(cfg_path)

    screen = FakeScreen()
    service = LyricsService(
        ServiceConfig(poll_seconds=0.02, media_poll_seconds=0.04),
        screen_factory=lambda: screen,
        reader=make_reader(),
        lyrics_client=CandidateLyricsClient(),
        settings=user_settings,
    )
    service.start()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not screen.texts:
            time.sleep(0.02)

        # Nudge persists per-track offset
        service.nudge(1.5)
        time.sleep(0.1)
        assert user_settings.get_track_offset("测试歌曲\n测试歌手") == 1.5

        # Candidate search & switch persists song_id
        cands = service.lyric_candidates()
        assert cands and cands[0]["song_id"] == 999
        tl = service.select_lyric_candidate(999)
        assert tl["lines"][0]["text"] == "候选词-999"
        assert user_settings.get_track_song_id("测试歌曲\n测试歌手") == "999"

        # Reset offset clears it
        service.reset_offset()
        time.sleep(0.1)
        assert user_settings.get_track_offset("测试歌曲\n测试歌手") == 0.0
    finally:
        service.stop()