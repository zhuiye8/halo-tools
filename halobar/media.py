"""Windows media-session (SMTC) reader.

SMTC gives us metadata and play/pause for every well-behaved player.  Position is
only reliable when the player implements timeline properties; NetEase's desktop
client reports a session but an all-zero timeline, so :attr:`Playback.position`
falls back to ``None`` and callers must estimate.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

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

    @property
    def track_key(self) -> str:
        return f"{self.title}\n{self.artist}"

    @property
    def valid(self) -> bool:
        return bool(self.title)


async def list_sessions() -> list[str]:
    """Return the AUMID of every SMTC session."""
    if not WINRT_AVAILABLE:
        return []
    manager = await _Manager.request_async()
    return [session.source_app_user_model_id for session in manager.get_sessions()]


async def read_playback(preferred_aumid: str | None = None,
                        use_sixk: bool = True) -> Playback | None:
    """Read the best available playback state.

    Order: the 6K Labs local endpoint (exact position, opt-in plugin) → Windows
    SMTC (metadata always, position only when the player implements it).
    """
    if use_sixk:
        from . import sixk

        plugin = await asyncio.to_thread(sixk.read_playback)
        if plugin is not None:
            log.debug("playback from 6k-labs: %r pos=%.1f", plugin.title, plugin.position or -1)
            return plugin
    if not WINRT_AVAILABLE:
        return None
    manager = await _Manager.request_async()
    sessions = list(manager.get_sessions())
    if not sessions:
        return None

    def rank(session) -> tuple[int, int]:
        aumid = session.source_app_user_model_id.lower()
        preferred = 1 if preferred_aumid and preferred_aumid.lower() in aumid else 0
        try:
            playing = 1 if session.get_playback_info().playback_status == _Status.PLAYING else 0
        except Exception:  # noqa: BLE001 - COM can fail when the app is closing
            playing = 0
        return preferred, playing

    session = max(sessions, key=rank)
    playback = Playback(aumid=session.source_app_user_model_id)
    try:
        media = await session.try_get_media_properties_async()
        playback.title = getattr(media, "title", "") or ""
        playback.artist = getattr(media, "artist", "") or ""
        playback.album = getattr(media, "album_title", "") or ""
    except Exception as exc:  # noqa: BLE001
        log.debug("media properties unavailable: %s", exc)
        return None
    try:
        info = session.get_playback_info()
        playback.playing = info.playback_status == _Status.PLAYING
        playback.rate = info.playback_rate or 1.0
    except Exception as exc:  # noqa: BLE001
        log.debug("playback info unavailable: %s", exc)
    try:
        timeline = session.get_timeline_properties()
        start = timeline.start_time.total_seconds()
        end = timeline.end_time.total_seconds()
        position = timeline.position.total_seconds()
        if end > 0:
            playback.duration = end - start
            playback.position = position - start
        # A zero-length timeline means the player does not implement progress.
        if playback.position is not None and playback.position < 0:
            playback.position = 0.0
    except Exception as exc:  # noqa: BLE001
        log.debug("timeline unavailable: %s", exc)
        return playback

    if playback.position is not None and playback.duration == 0:
        playback.position = None
    if playback.position is None and playback.duration is None:
        log.debug("%s exposes no timeline (position=None)", playback.aumid)
    return playback


def read_playback_sync(preferred_aumid: str | None = None) -> Playback | None:
    """Blocking wrapper for callers that are not async."""
    return asyncio.run(read_playback(preferred_aumid))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)