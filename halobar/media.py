"""Windows media-session (SMTC) & 6K Labs unified playback reader.

Design goals
------------
1. **Low cross-process ALPC pressure**: reuse the WinRT SMTC manager and session,
   subscribe to WinRT change events (only setting lightweight dirty flags inside
   callbacks), and apply tiered TTLs for session scans, media properties, and
   playback/timeline state.  If event subscription fails, fall back cleanly to
   low-frequency polling.
2. **Single-flight protection**: at most one WinRT SMTC operation may be in
   flight at any time.  If a previous call timed out and the underlying WinRT
   operation is still hanging in ``NPSMSvc`` or the player, subsequent reads
   never stack additional calls on top of it.
3. **Exponential backoff & circuit breaker**: repeated timeouts or COM failures
   back off exponentially and detach stale COM proxies before probing again.
4. **6K Labs priority without idle churn**: prefer the local 6K Labs endpoint
   when online, but use persistent Keep-Alive + offline backoff so an absent
   plugin never hammers ``127.0.0.1:9863``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger(__name__)

try:  # pragma: no cover - import guarded for non-Windows test runs
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as _Manager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as _Status,
    )

    WINRT_AVAILABLE = True
except ImportError:  # pragma: no cover
    _Manager = None  # type: ignore[assignment]
    _Status = None  # type: ignore[assignment]
    WINRT_AVAILABLE = False

SESSION_TTL = 5.0
METADATA_TTL = 10.0
METADATA_FALLBACK_TTL = 2.0
STATE_TTL = 1.0
DEFAULT_BACKOFF_SCHEDULE = (2.0, 5.0, 15.0, 30.0, 60.0)


@dataclass
class Playback:
    """A point-in-time view of one media session."""

    title: str = ""
    artist: str = ""
    album: str = ""
    playing: bool = False
    position: float | None = None
    duration: float | None = None
    rate: float = 1.0
    aumid: str = ""
    song_id: str = ""
    observed_at: float = field(default_factory=time.monotonic, compare=False)

    @property
    def track_key(self) -> str:
        return f"{self.title}\n{self.artist}"

    @property
    def valid(self) -> bool:
        return bool(self.title)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_cancel_op(op: Any) -> None:
    """Best-effort cancellation of a WinRT IAsyncOperation."""
    if op is None:
        return
    cancel = getattr(op, "cancel", None)
    if callable(cancel):
        try:
            cancel()
        except Exception:  # noqa: BLE001
            pass


class SMTCReader:
    """Event-driven, single-flight, circuit-broken Windows SMTC reader."""

    def __init__(
        self,
        *,
        session_ttl: float = SESSION_TTL,
        metadata_ttl: float = METADATA_TTL,
        metadata_fallback_ttl: float = METADATA_FALLBACK_TTL,
        state_ttl: float = STATE_TTL,
        timeout: float = 3.0,
        backoff_schedule: tuple[float, ...] = DEFAULT_BACKOFF_SCHEDULE,
        circuit_threshold: int = 3,
        manager_factory: Callable[[], Any] | None = None,
        playing_status: Any = None,
        clock: Callable[[], float] = time.monotonic,
        utc_clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.session_ttl = session_ttl
        self.metadata_ttl = metadata_ttl
        self.metadata_fallback_ttl = metadata_fallback_ttl
        self.state_ttl = state_ttl
        self.timeout = timeout
        self.backoff_schedule = backoff_schedule or (2.0,)
        self.circuit_threshold = max(1, circuit_threshold)
        self._manager_factory = manager_factory
        self._playing_status = playing_status if playing_status is not None else (
            _Status.PLAYING if _Status is not None else 4
        )
        self._clock = clock
        self._utc_clock = utc_clock

        # Cached WinRT proxies and event tokens.
        self._manager: Any = None
        self._manager_tokens: list[tuple[str, Any]] = []
        self._session: Any = None
        self._session_aumid: str = ""
        self._session_tokens: list[tuple[str, Any]] = []
        self._events_attached: bool = False
        self._last_preferred: str | None = None

        # Dirty flags set from WinRT event callbacks (never invoke COM inside callbacks).
        self._sessions_dirty = threading.Event()
        self._media_dirty = threading.Event()
        self._state_dirty = threading.Event()
        self._sessions_dirty.set()

        # Timestamps for tiered TTL refreshes.
        self._next_session_scan: float = 0.0
        self._next_metadata_read: float = 0.0
        self._next_state_read: float = 0.0

        # Cached metadata & playback snapshot.
        self._title: str = ""
        self._artist: str = ""
        self._album: str = ""
        self._has_timeline: bool | None = None
        self._cached_playback: Playback | None = None

        # Single-flight and circuit breaker state.
        self._inflight: asyncio.Task[Playback | None] | None = None
        self._active_op: Any = None
        self._timed_out: bool = False
        self._reset_pending: bool = False
        self._consecutive_failures: int = 0
        self._backoff_until: float = 0.0
        self._last_error: str = ""
        self._last_warn_at: float = 0.0

        # Diagnostic counters.
        self.manager_requests: int = 0
        self.session_scans: int = 0
        self.metadata_reads: int = 0
        self.state_reads: int = 0
        self.events_received: int = 0
        self.timeouts: int = 0
        self.errors: int = 0
        self.inflight_skips: int = 0
        self.backoff_skips: int = 0
        self.circuit_trips: int = 0

    @property
    def available(self) -> bool:
        return self._manager_factory is not None or WINRT_AVAILABLE

    def has_pending_event(self) -> bool:
        """True when a WinRT event signalled fresh state and SMTC is healthy."""
        if not self.available:
            return False
        if self._inflight is not None and not self._inflight.done():
            return False
        if self._clock() < self._backoff_until:
            return False
        return (
            self._sessions_dirty.is_set()
            or self._media_dirty.is_set()
            or self._state_dirty.is_set()
        )

    # ------------------------------------------------------------ event wiring
    def _on_sessions_changed(self, _sender: Any = None, _args: Any = None) -> None:
        self.events_received += 1
        self._sessions_dirty.set()

    def _on_media_changed(self, _sender: Any = None, _args: Any = None) -> None:
        self.events_received += 1
        self._media_dirty.set()
        self._state_dirty.set()

    def _on_state_changed(self, _sender: Any = None, _args: Any = None) -> None:
        self.events_received += 1
        self._state_dirty.set()

    def _on_timeline_changed(self, _sender: Any = None, _args: Any = None) -> None:
        self.events_received += 1
        # Players like NetEase emit timeline_properties_changed every second even
        # when their timeline is all-zero; ignore early wakeups in that case so
        # routine state_ttl handles it without extra WinRT calls.
        if self._has_timeline is not False:
            self._state_dirty.set()

    def _attach_manager(self, manager: Any) -> None:
        self._detach_manager()
        self._manager = manager
        attached = False
        for add_name, remove_name in (
            ("add_sessions_changed", "remove_sessions_changed"),
            ("add_current_session_changed", "remove_current_session_changed"),
        ):
            adder = getattr(manager, add_name, None)
            if callable(adder):
                try:
                    token = adder(self._on_sessions_changed)
                    self._manager_tokens.append((remove_name, token))
                    attached = True
                except Exception as exc:  # noqa: BLE001
                    log.debug("SMTC manager event %s unavailable: %s", add_name, exc)
        if not attached:
            self._events_attached = False

    def _detach_manager(self) -> None:
        self._detach_session()
        manager, self._manager = self._manager, None
        tokens, self._manager_tokens = self._manager_tokens, []
        if manager is not None:
            for remove_name, token in tokens:
                remover = getattr(manager, remove_name, None)
                if callable(remover):
                    try:
                        remover(token)
                    except Exception:  # noqa: BLE001
                        pass
        self._sessions_dirty.set()
        self._next_session_scan = 0.0

    def _attach_session(self, session: Any) -> None:
        self._detach_session()
        self._session = session
        if session is None:
            self._session_aumid = ""
            self._title = ""
            self._artist = ""
            self._album = ""
            self._cached_playback = None
            return
        self._session_aumid = str(getattr(session, "source_app_user_model_id", "") or "")
        self._has_timeline = None
        attached = 0
        for add_name, remove_name, callback in (
            ("add_media_properties_changed", "remove_media_properties_changed", self._on_media_changed),
            ("add_playback_info_changed", "remove_playback_info_changed", self._on_state_changed),
            ("add_timeline_properties_changed", "remove_timeline_properties_changed", self._on_timeline_changed),
        ):
            adder = getattr(session, add_name, None)
            if callable(adder):
                try:
                    token = adder(callback)
                    self._session_tokens.append((remove_name, token))
                    attached += 1
                except Exception as exc:  # noqa: BLE001
                    log.debug("SMTC session event %s unavailable: %s", add_name, exc)
        self._events_attached = attached > 0
        self._media_dirty.set()
        self._state_dirty.set()
        self._next_metadata_read = 0.0
        self._next_state_read = 0.0

    def _detach_session(self) -> None:
        session, self._session = self._session, None
        tokens, self._session_tokens = self._session_tokens, []
        self._events_attached = False
        if session is not None:
            for remove_name, token in tokens:
                remover = getattr(session, remove_name, None)
                if callable(remover):
                    try:
                        remover(token)
                    except Exception:  # noqa: BLE001
                        pass
        self._session_aumid = ""
        self._media_dirty.clear()
        self._state_dirty.clear()

    def close(self) -> None:
        """Detach WinRT event handlers and release cached proxies."""
        _safe_cancel_op(self._active_op)
        self._active_op = None
        if self._inflight is not None:
            try:
                if not self._inflight.done() and not self._inflight.get_loop().is_closed():
                    self._inflight.cancel()
            except Exception:  # noqa: BLE001
                pass
            self._inflight = None
        self._detach_manager()
        self._cached_playback = None
        self._reset_pending = False

    # --------------------------------------------------------------- polling
    def _needs_refresh(self, now: float, preferred_aumid: str | None) -> bool:
        if self._reset_pending or self._manager is None:
            return True
        if preferred_aumid != self._last_preferred:
            return True
        if self._sessions_dirty.is_set() or now >= self._next_session_scan:
            return True
        if self._session is None:
            return False
        if self._media_dirty.is_set() or now >= self._next_metadata_read:
            return True
        if self._state_dirty.is_set() or now >= self._next_state_read:
            return True
        return False

    async def _await_op(self, op: Any) -> Any:
        self._active_op = op
        try:
            return await op
        finally:
            if self._active_op is op:
                self._active_op = None

    async def _ensure_manager(self) -> Any:
        if self._manager is not None:
            return self._manager
        self.manager_requests += 1
        factory = self._manager_factory or _Manager.request_async
        manager = await self._await_op(factory())
        self._attach_manager(manager)
        return manager

    def _select_session(self, sessions: list[Any], preferred_aumid: str | None) -> Any:
        def rank(session: Any) -> tuple[int, int]:
            aumid = str(getattr(session, "source_app_user_model_id", "") or "").lower()
            preferred = 1 if preferred_aumid and preferred_aumid.lower() in aumid else 0
            try:
                playing = 1 if session.get_playback_info().playback_status == self._playing_status else 0
            except Exception:  # noqa: BLE001 - COM can fail when the player closes
                playing = 0
            return preferred, playing

        return max(sessions, key=rank)

    async def _poll_once(self, preferred_aumid: str | None) -> Playback | None:
        if self._reset_pending:
            self._detach_manager()
            self._reset_pending = False

        now = self._clock()
        manager = await self._ensure_manager()

        preferred_changed = preferred_aumid != self._last_preferred
        self._last_preferred = preferred_aumid
        sessions_event = self._sessions_dirty.is_set()
        if sessions_event or preferred_changed or now >= self._next_session_scan:
            self._sessions_dirty.clear()
            self.session_scans += 1
            sessions = list(manager.get_sessions())
            self._next_session_scan = self._clock() + self.session_ttl
            if not sessions:
                self._attach_session(None)
                return None
            selected = self._select_session(sessions, preferred_aumid)
            selected_aumid = str(getattr(selected, "source_app_user_model_id", "") or "")
            # Re-attach when session changed, when a sessions_changed event fired,
            # or when no session was attached yet.  On a routine TTL scan with the
            # same AUMID, keep the existing attached wrapper to avoid COM churn.
            if self._session is None or sessions_event or selected_aumid != self._session_aumid:
                self._attach_session(selected)

        session = self._session
        if session is None:
            return None

        now = self._clock()
        meta_ttl = self.metadata_ttl if self._events_attached else self.metadata_fallback_ttl
        if self._media_dirty.is_set() or now >= self._next_metadata_read:
            self._media_dirty.clear()
            self.metadata_reads += 1
            try:
                media = await self._await_op(session.try_get_media_properties_async())
                self._title = getattr(media, "title", "") or ""
                self._artist = getattr(media, "artist", "") or ""
                self._album = getattr(media, "album_title", "") or ""
            except Exception as exc:  # noqa: BLE001
                log.debug("media properties unavailable: %s", exc)
                # The cached session proxy may be stale (player exited); rescan next time.
                self._attach_session(None)
                self._sessions_dirty.set()
                return None
            self._next_metadata_read = self._clock() + meta_ttl
            self._state_dirty.set()

        now = self._clock()
        if self._state_dirty.is_set() or now >= self._next_state_read or self._cached_playback is None:
            self._state_dirty.clear()
            self.state_reads += 1
            playing = False
            rate = 1.0
            position: float | None = None
            duration: float | None = None
            try:
                info = session.get_playback_info()
                playing = info.playback_status == self._playing_status
                rate = float(getattr(info, "playback_rate", None) or 1.0)
            except Exception as exc:  # noqa: BLE001
                log.debug("playback info unavailable: %s", exc)
                self._attach_session(None)
                self._sessions_dirty.set()
                return None

            try:
                timeline = session.get_timeline_properties()
                start = timeline.start_time.total_seconds()
                end = timeline.end_time.total_seconds()
                raw_pos = timeline.position.total_seconds()
                if end > start and end > 0:
                    duration = end - start
                    position = raw_pos - start
                    last_updated = getattr(timeline, "last_updated_time", None)
                    if playing and isinstance(last_updated, datetime) and last_updated.year >= 2000:
                        if last_updated.tzinfo is None:
                            last_updated = last_updated.replace(tzinfo=timezone.utc)
                        age = (self._utc_clock() - last_updated).total_seconds()
                        if 0.0 < age < 3600.0:
                            position += age * max(0.0, rate)
                    if position < 0.0:
                        position = 0.0
                    if duration > 0.0 and position > duration:
                        position = duration
            except Exception as exc:  # noqa: BLE001
                log.debug("timeline unavailable: %s", exc)

            if position is not None and duration == 0:
                position = None
            self._has_timeline = position is not None

            self._next_state_read = self._clock() + self.state_ttl
            self._cached_playback = Playback(
                title=self._title,
                artist=self._artist,
                album=self._album,
                playing=playing,
                position=position,
                duration=duration,
                rate=rate,
                aumid=self._session_aumid,
                observed_at=self._clock(),
            )
            if self._cached_playback.position is None and self._cached_playback.duration is None:
                log.debug("%s exposes no timeline (position=None)", self._cached_playback.aumid)

        return self._cached_playback

    def _record_success(self) -> None:
        if self._consecutive_failures > 0:
            log.info("SMTC recovered after %d failure(s)", self._consecutive_failures)
        self._consecutive_failures = 0
        self._backoff_until = 0.0
        self._timed_out = False
        self._last_error = ""

    def _record_failure(self, detail: str, *, is_timeout: bool) -> None:
        self._consecutive_failures += 1
        self._last_error = detail
        if is_timeout:
            self.timeouts += 1
            self._timed_out = True
        else:
            self.errors += 1
        idx = min(self._consecutive_failures - 1, len(self.backoff_schedule) - 1)
        delay = self.backoff_schedule[idx]
        now = self._clock()
        self._backoff_until = now + delay
        if self._consecutive_failures >= self.circuit_threshold:
            if self._consecutive_failures == self.circuit_threshold:
                self.circuit_trips += 1
            self._reset_pending = True
            self._cached_playback = None
            if self._inflight is None or self._inflight.done():
                self._detach_manager()
        if now - self._last_warn_at >= 5.0:
            self._last_warn_at = now
            log.warning(
                "SMTC %s (failures=%d, backoff=%.1fs): %s",
                "timed out" if is_timeout else "failed",
                self._consecutive_failures,
                delay,
                detail,
            )

    def _on_late_task_done(self, task: asyncio.Task[Playback | None]) -> None:
        if self._inflight is task:
            self._inflight = None
            self._active_op = None
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        if self._reset_pending:
            self._detach_manager()
            self._reset_pending = False

    async def read(
        self,
        preferred_aumid: str | None = None,
        *,
        timeout: float | None = None,
    ) -> Playback | None:
        """Read playback state with caching, single-flight guard, and backoff."""
        if not self.available:
            return None

        # 1. Single-flight check: never stack a second WinRT call while one is active.
        if self._inflight is not None:
            try:
                loop_closed = self._inflight.get_loop().is_closed()
            except Exception:  # noqa: BLE001
                loop_closed = True
            if loop_closed:
                self._inflight = None
                self._active_op = None
            elif not self._inflight.done():
                self.inflight_skips += 1
                return None if self._timed_out else self._cached_playback
            else:
                self._inflight = None

        # 2. Circuit breaker / backoff window.
        now = self._clock()
        if now < self._backoff_until:
            self.backoff_skips += 1
            return None

        # 3. Cache fast path: nothing dirty and no TTL expired -> zero WinRT calls.
        if not self._needs_refresh(now, preferred_aumid):
            return self._cached_playback

        # 4. Launch single-flight task and wait up to timeout.
        effective_timeout = self.timeout if timeout is None else timeout
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._poll_once(preferred_aumid))
        self._inflight = task

        done, _ = await asyncio.wait({task}, timeout=effective_timeout)
        if task in done:
            self._inflight = None
            try:
                result = task.result()
            except asyncio.CancelledError:
                self._record_failure("cancelled", is_timeout=False)
                return None
            except Exception as exc:  # noqa: BLE001
                self._record_failure(str(exc), is_timeout=False)
                return None
            self._record_success()
            return result

        # Timed out: try cancelling the underlying WinRT op, but keep `_inflight`
        # pinned until the task actually settles so no concurrent calls pile up.
        _safe_cancel_op(self._active_op)
        task.add_done_callback(self._on_late_task_done)
        self._record_failure(f"timed out after {effective_timeout:.1f}s", is_timeout=True)
        return None

    async def list_sessions(self, *, timeout: float | None = None) -> list[str]:
        """Return active SMTC session AUMIDs without leaking extra managers."""
        if not self.available:
            return []
        if self._inflight is not None and not self._inflight.done():
            return [self._session_aumid] if self._session_aumid else []
        effective_timeout = self.timeout if timeout is None else timeout
        try:
            manager = await asyncio.wait_for(self._ensure_manager(), timeout=effective_timeout)
            return [s.source_app_user_model_id for s in manager.get_sessions()]
        except Exception as exc:  # noqa: BLE001
            log.debug("list_sessions failed: %s", exc)
            return []

    def diagnostics(self) -> dict[str, object]:
        now = self._clock()
        backoff_remaining = max(0.0, self._backoff_until - now)
        inflight = bool(self._inflight is not None and not self._inflight.done())
        if not self.available:
            mode = "disabled"
        elif backoff_remaining > 0:
            mode = "circuit-open" if self._consecutive_failures >= self.circuit_threshold else "backoff"
        elif self._events_attached or (self._session is None and bool(self._manager_tokens)):
            mode = "events"
        else:
            mode = "polling"
        return {
            "mode": mode,
            "inflight": inflight,
            "session_aumid": self._session_aumid,
            "manager_requests": self.manager_requests,
            "session_scans": self.session_scans,
            "metadata_reads": self.metadata_reads,
            "state_reads": self.state_reads,
            "events_received": self.events_received,
            "timeouts": self.timeouts,
            "errors": self.errors,
            "inflight_skips": self.inflight_skips,
            "backoff_skips": self.backoff_skips,
            "circuit_trips": self.circuit_trips,
            "consecutive_failures": self._consecutive_failures,
            "backoff_remaining": round(backoff_remaining, 2),
            "last_error": self._last_error,
        }


class PlaybackReader:
    """Unified playback reader: 6K Labs (Keep-Alive + backoff) -> Windows SMTC."""

    def __init__(
        self,
        *,
        sixk_client: Any = None,
        smtc_reader: SMTCReader | None = None,
        sixk_interval: float = 0.35,
        smtc_interval: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if sixk_client is None:
            from .sixk import SixKClient

            sixk_client = SixKClient(clock=clock)
        self.sixk = sixk_client
        self.smtc = smtc_reader or SMTCReader(clock=clock)
        self.sixk_interval = sixk_interval
        self.smtc_interval = smtc_interval
        self._clock = clock

        self._sixk_inflight: asyncio.Task[tuple[bool, Playback | None]] | None = None
        self._sixk_cached: Playback | None = None
        self._next_sixk_at: float = 0.0
        self._next_smtc_at: float = 0.0
        self._smtc_cached: Playback | None = None

    def has_pending_event(self) -> bool:
        return self.smtc.has_pending_event()

    async def _poll_sixk(self) -> tuple[bool, Playback | None]:
        if self._sixk_inflight is not None:
            try:
                closed = self._sixk_inflight.get_loop().is_closed()
            except Exception:  # noqa: BLE001
                closed = True
            if closed:
                self._sixk_inflight = None
            elif not self._sixk_inflight.done():
                return self.sixk.online, self._sixk_cached
            else:
                self._sixk_inflight = None

        loop = asyncio.get_running_loop()
        task = loop.create_task(asyncio.to_thread(self.sixk.read))
        self._sixk_inflight = task
        done, _ = await asyncio.wait({task}, timeout=1.5)
        if task in done:
            self._sixk_inflight = None
            try:
                return task.result()
            except Exception as exc:  # noqa: BLE001
                log.debug("6k-labs task failed: %s", exc)
                return False, None
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        return False, None

    async def read(
        self,
        preferred_aumid: str | None = None,
        use_sixk: bool = True,
        timeout: float = 3.0,
    ) -> Playback | None:
        now = self._clock()
        if use_sixk:
            if self.sixk.online and self._sixk_cached is not None and now < self._next_sixk_at:
                return self._sixk_cached
            if now >= self._next_sixk_at or not self.sixk.online:
                online, plugin = await self._poll_sixk()
                self._sixk_cached = plugin
                if online:
                    self._next_sixk_at = self._clock() + self.sixk_interval
                if plugin is not None:
                    log.debug("playback from 6k-labs: %r pos=%.1f",
                              plugin.title, plugin.position or -1)
                    return plugin

        now = self._clock()
        if (
            self._smtc_cached is not None
            and now < self._next_smtc_at
            and not self.smtc.has_pending_event()
        ):
            return self._smtc_cached

        playback = await self.smtc.read(preferred_aumid, timeout=timeout)
        self._smtc_cached = playback
        self._next_smtc_at = self._clock() + self.smtc_interval
        return playback

    def close(self) -> None:
        self.smtc.close()
        self.sixk.close()
        self._sixk_cached = None
        self._smtc_cached = None

    def diagnostics(self) -> dict[str, object]:
        return {
            "sixk": self.sixk.diagnostics(),
            "smtc": self.smtc.diagnostics(),
        }


_default_reader: PlaybackReader | None = None


def default_reader() -> PlaybackReader:
    global _default_reader
    if _default_reader is None:
        _default_reader = PlaybackReader()
    return _default_reader


def reset_default_reader() -> None:
    """Close and reset the module-level default reader (for tests / shutdown)."""
    global _default_reader
    if _default_reader is not None:
        _default_reader.close()
        _default_reader = None


async def list_sessions() -> list[str]:
    """Return the AUMID of every SMTC session."""
    return await default_reader().smtc.list_sessions()


async def read_playback(
    preferred_aumid: str | None = None,
    use_sixk: bool = True,
    timeout: float = 3.0,
) -> Playback | None:
    """Read the best available playback state, with caching, single-flight and backoff."""
    return await default_reader().read(
        preferred_aumid=preferred_aumid,
        use_sixk=use_sixk,
        timeout=timeout,
    )


def diagnostics() -> dict[str, object]:
    """Snapshot of the default reader's SMTC & 6K Labs health counters."""
    return default_reader().diagnostics()


def read_playback_sync(preferred_aumid: str | None = None) -> Playback | None:
    """Blocking wrapper for callers that are not async."""
    return asyncio.run(read_playback(preferred_aumid))


def clone_playback(playback: Playback) -> Playback:
    return replace(playback)
