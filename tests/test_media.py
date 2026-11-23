"""Tests for SMTC caching, event handling, single-flight protection, and circuit breaker."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from halobar.media import Playback, PlaybackReader, SMTCReader


class FakeClock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeOp:
    """Awaitable mimicking a WinRT IAsyncOperation with ``cancel()``."""

    def __init__(self, result=None, *, gate: asyncio.Event | None = None,
                 error: Exception | None = None) -> None:
        self._result = result
        self._gate = gate
        self._error = error
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def __await__(self):
        async def _run():
            if self._gate is not None:
                await self._gate.wait()
            if self._error is not None:
                raise self._error
            return self._result

        return _run().__await__()


class FakeMediaProps:
    def __init__(self, title: str, artist: str = "", album: str = "") -> None:
        self.title = title
        self.artist = artist
        self.album_title = album


class FakePlaybackInfo:
    def __init__(self, status: int = 4, rate: float = 1.0) -> None:
        self.playback_status = status
        self.playback_rate = rate


class FakeTimeline:
    def __init__(self, position: float = 0.0, duration: float = 0.0,
                 last_updated: datetime | None = None) -> None:
        self.start_time = timedelta(seconds=0)
        self.end_time = timedelta(seconds=duration)
        self.position = timedelta(seconds=position)
        self.last_updated_time = last_updated


class FakeSession:
    def __init__(
        self,
        aumid: str = "cloudmusic.exe",
        title: str = "如愿",
        artist: str = "王菲",
        playing: bool = True,
        position: float = 0.0,
        duration: float = 0.0,
        *,
        support_events: bool = True,
    ) -> None:
        self.source_app_user_model_id = aumid
        self.title = title
        self.artist = artist
        self.playing = playing
        self.position = position
        self.duration = duration
        self.last_updated: datetime | None = None
        self.support_events = support_events
        self.media_calls = 0
        self.info_calls = 0
        self.timeline_calls = 0
        self.media_gate: asyncio.Event | None = None
        self.media_error: Exception | None = None
        self.last_op: FakeOp | None = None
        self._callbacks: dict[str, dict[int, object]] = {
            "media": {}, "playback": {}, "timeline": {},
        }
        self._next_token = 1
        self.removed_tokens: list[tuple[str, int]] = []

    def _add(self, kind: str, cb) -> int:
        if not self.support_events:
            raise RuntimeError("events unsupported")
        tok = self._next_token
        self._next_token += 1
        self._callbacks[kind][tok] = cb
        return tok

    def _remove(self, kind: str, tok: int) -> None:
        self._callbacks[kind].pop(tok, None)
        self.removed_tokens.append((kind, tok))

    def add_media_properties_changed(self, cb):
        return self._add("media", cb)

    def remove_media_properties_changed(self, tok):
        self._remove("media", tok)

    def add_playback_info_changed(self, cb):
        return self._add("playback", cb)

    def remove_playback_info_changed(self, tok):
        self._remove("playback", tok)

    def add_timeline_properties_changed(self, cb):
        return self._add("timeline", cb)

    def remove_timeline_properties_changed(self, tok):
        self._remove("timeline", tok)

    def fire(self, kind: str) -> None:
        for cb in list(self._callbacks[kind].values()):
            cb(self, None)

    def try_get_media_properties_async(self):
        self.media_calls += 1
        op = FakeOp(
            FakeMediaProps(self.title, self.artist),
            gate=self.media_gate,
            error=self.media_error,
        )
        self.last_op = op
        return op

    def get_playback_info(self):
        self.info_calls += 1
        return FakePlaybackInfo(status=4 if self.playing else 5)

    def get_timeline_properties(self):
        self.timeline_calls += 1
        return FakeTimeline(self.position, self.duration, self.last_updated)


class FakeManager:
    def __init__(self, sessions: list[FakeSession]) -> None:
        self.sessions = list(sessions)
        self.get_sessions_calls = 0
        self._callbacks: dict[int, object] = {}
        self._next_token = 1
        self.removed_tokens: list[int] = []

    def add_sessions_changed(self, cb):
        tok = self._next_token
        self._next_token += 1
        self._callbacks[tok] = cb
        return tok

    def remove_sessions_changed(self, tok):
        self._callbacks.pop(tok, None)
        self.removed_tokens.append(tok)

    def add_current_session_changed(self, cb):
        return self.add_sessions_changed(cb)

    def remove_current_session_changed(self, tok):
        self.remove_sessions_changed(tok)

    def fire_sessions_changed(self) -> None:
        for cb in list(self._callbacks.values()):
            cb(self, None)

    def get_sessions(self):
        self.get_sessions_calls += 1
        return list(self.sessions)


def test_smtc_caches_session_and_responds_to_events():
    clock = FakeClock()
    session = FakeSession(title="歌一", artist="歌手A", position=12.0, duration=200.0)
    manager = FakeManager([session])

    reader = SMTCReader(
        session_ttl=5.0,
        metadata_ttl=10.0,
        state_ttl=1.0,
        manager_factory=lambda: FakeOp(manager),
        playing_status=4,
        clock=clock,
    )

    async def scenario():
        first = await reader.read()
        assert first is not None and first.title == "歌一"
        assert reader.diagnostics()["mode"] == "events"

        # Repeated reads within TTLs must issue ZERO additional WinRT calls.
        for _ in range(10):
            clock.advance(0.05)
            cached = await reader.read()
            assert cached is first

        assert reader.manager_requests == 1
        assert reader.session_scans == 1
        assert reader.metadata_reads == 1
        assert reader.state_reads == 1

        # Playback state event refreshes state only, not metadata or session scan.
        session.playing = False
        session.fire("playback")
        assert reader.has_pending_event() is True
        updated = await reader.read()
        assert updated is not None and updated.playing is False
        assert reader.session_scans == 1
        assert reader.metadata_reads == 1
        assert reader.state_reads == 2

        # Media properties event refreshes metadata and state.
        session.title = "歌二"
        session.playing = True
        session.fire("media")
        switched = await reader.read()
        assert switched is not None and switched.title == "歌二"
        assert reader.metadata_reads == 2

        # Closing detaches all event tokens cleanly.
        reader.close()
        assert len(manager.removed_tokens) == 2
        assert len(session.removed_tokens) == 3

    asyncio.run(scenario())


def test_smtc_falls_back_to_polling_when_events_unavailable():
    clock = FakeClock()
    session = FakeSession(title="旧歌", support_events=False)
    manager = FakeManager([session])

    reader = SMTCReader(
        session_ttl=5.0,
        metadata_ttl=10.0,
        metadata_fallback_ttl=2.0,
        state_ttl=1.0,
        manager_factory=lambda: FakeOp(manager),
        playing_status=4,
        clock=clock,
    )

    async def scenario():
        res = await reader.read()
        assert res is not None and res.title == "旧歌"
        assert reader.diagnostics()["mode"] == "polling"

        session.title = "新歌"
        clock.advance(1.5)
        res2 = await reader.read()
        assert res2 is not None and res2.title == "旧歌"  # still cached (< 2.0s)

        clock.advance(0.6)
        res3 = await reader.read()
        assert res3 is not None and res3.title == "新歌"  # refreshed after fallback TTL
        reader.close()

    asyncio.run(scenario())


def test_smtc_single_flight_prevents_concurrent_pileup():
    clock = FakeClock()
    session = FakeSession(title="慢响应")
    manager = FakeManager([session])

    reader = SMTCReader(
        timeout=1.0,
        manager_factory=lambda: FakeOp(manager),
        playing_status=4,
        clock=clock,
    )

    async def scenario():
        gate = asyncio.Event()
        session.media_gate = gate

        first_task = asyncio.create_task(reader.read())
        await asyncio.sleep(0.02)

        # Second call while first is still waiting must NOT start another WinRT read.
        second = await reader.read()
        assert second is None
        assert session.media_calls == 1
        assert reader.inflight_skips == 1

        gate.set()
        first = await first_task
        assert first is not None and first.title == "慢响应"
        assert session.media_calls == 1
        reader.close()

    asyncio.run(scenario())


def test_smtc_timeout_cancels_op_and_backs_off():
    clock = FakeClock()
    session = FakeSession(title="卡死")
    manager = FakeManager([session])

    reader = SMTCReader(
        timeout=0.05,
        backoff_schedule=(2.0, 5.0, 15.0),
        circuit_threshold=3,
        manager_factory=lambda: FakeOp(manager),
        playing_status=4,
        clock=clock,
    )

    async def scenario():
        gate = asyncio.Event()
        session.media_gate = gate

        res = await reader.read()
        assert res is None
        assert reader.timeouts == 1
        assert session.last_op is not None and session.last_op.cancelled is True

        # Calling repeatedly while hung & backing off must NOT issue another WinRT call.
        for _ in range(5):
            assert await reader.read() is None
        assert session.media_calls == 1
        assert reader.inflight_skips >= 5

        # Release the hung task; reader is still in backoff window until clock advances.
        gate.set()
        await asyncio.sleep(0.02)
        assert await reader.read() is None
        assert reader.backoff_skips >= 1
        assert session.media_calls == 1

        # Advance past backoff -> recovers cleanly.
        session.media_gate = None
        clock.advance(2.5)
        recovered = await reader.read()
        assert recovered is not None and recovered.title == "卡死"
        assert reader.diagnostics()["consecutive_failures"] == 0
        reader.close()

    asyncio.run(scenario())


def test_smtc_circuit_breaker_detaches_and_recovers():
    clock = FakeClock()
    healthy = FakeManager([FakeSession(title="恢复后")])
    fail_mode = {"broken": True}
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        if fail_mode["broken"]:
            return FakeOp(error=RuntimeError("COM RPC failed"))
        return FakeOp(healthy)

    reader = SMTCReader(
        backoff_schedule=(2.0, 5.0, 15.0),
        circuit_threshold=3,
        manager_factory=factory,
        playing_status=4,
        clock=clock,
    )

    async def scenario():
        assert await reader.read() is None
        clock.advance(2.1)
        assert await reader.read() is None
        clock.advance(5.1)
        assert await reader.read() is None

        diag = reader.diagnostics()
        assert diag["circuit_trips"] == 1
        assert diag["mode"] == "circuit-open"
        assert diag["backoff_remaining"] == pytest.approx(15.0, abs=0.2)

        # Blocked during 15s circuit open window.
        clock.advance(10.0)
        assert await reader.read() is None
        assert calls["n"] == 3

        # Half-open probe succeeds after 15s.
        fail_mode["broken"] = False
        clock.advance(6.0)
        res = await reader.read()
        assert res is not None and res.title == "恢复后"
        assert reader.diagnostics()["consecutive_failures"] == 0
        reader.close()

    asyncio.run(scenario())


def test_smtc_timeline_uses_last_updated_time():
    clock = FakeClock()
    base_utc = datetime(2026, 11, 24, 12, 0, 10, tzinfo=timezone.utc)
    session = FakeSession(title="Spotify Track", position=20.0, duration=180.0, playing=True)
    session.last_updated = base_utc - timedelta(seconds=3.5)
    manager = FakeManager([session])

    reader = SMTCReader(
        manager_factory=lambda: FakeOp(manager),
        playing_status=4,
        clock=clock,
        utc_clock=lambda: base_utc,
    )

    async def scenario():
        res = await reader.read()
        assert res is not None
        assert res.position == pytest.approx(23.5, abs=0.05)
        reader.close()

    asyncio.run(scenario())


def test_playback_reader_prefers_sixk_and_caches():
    clock = FakeClock()

    class StubSixK:
        def __init__(self) -> None:
            self.online = True
            self.calls = 0
            self.playback = Playback(title="6K歌", artist="王菲", playing=True, position=5.0, aumid="6k-labs:1")

        def read(self):
            self.calls += 1
            return self.online, self.playback if self.online else None

        def close(self):
            pass

        def diagnostics(self):
            return {"online": self.online, "reads": self.calls}

    sixk_stub = StubSixK()
    smtc = SMTCReader(
        manager_factory=lambda: FakeOp(FakeManager([FakeSession(title="SMTC歌")])),
        playing_status=4,
        clock=clock,
    )
    reader = PlaybackReader(
        sixk_client=sixk_stub,
        smtc_reader=smtc,
        sixk_interval=0.5,
        smtc_interval=0.5,
        clock=clock,
    )

    async def scenario():
        p1 = await reader.read()
        assert p1 is not None and p1.title == "6K歌"
        # Within sixk_interval, cached result is returned without hitting sixk or SMTC.
        clock.advance(0.1)
        p2 = await reader.read()
        assert p2 is p1
        assert sixk_stub.calls == 1
        assert smtc.manager_requests == 0

        # When 6K goes offline, falls back to SMTC.
        sixk_stub.online = False
        clock.advance(0.6)
        p3 = await reader.read()
        assert p3 is not None and p3.title == "SMTC歌"
        assert smtc.manager_requests == 1
        reader.close()

    asyncio.run(scenario())
