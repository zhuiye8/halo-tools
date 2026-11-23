"""6K Labs local HTTP source (BetterNCM plugin "6k-labs").

The plugin exposes playback state on ``GET http://127.0.0.1:9863/query``::

    {"player": {"hasSong": true, "isPaused": false,
                "seekbarCurrentPosition": 128.4, "seekbarCurrentPositionHuman": "2:08",
                "statePercent": 48.2, "volumePercent": 80, "likeStatus": "...",
                "repeatType": "ALL"},
     "track":  {"title": "...", "author": "...", "album": "...", "duration": 265.0,
                "durationHuman": "4:25", "id": "NCM-1888381008", "cover": "data:...",
                "isVideo": false, "isAdvertisement": false, "inLibrary": true}}

Values are seconds.  When the plugin has no fresh data it returns an "empty query"
(``hasSong: false``).  This source needs the user to install
BetterNCM + InfLink-rs + 6K Labs into the official (non-Store) NetEase client.
"""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

from .media import Playback

log = logging.getLogger(__name__)

ENDPOINT = "http://127.0.0.1:9863/query"
_PARSED = urlparse(ENDPOINT)
_HOST = _PARSED.hostname or "127.0.0.1"
_PORT = _PARSED.port or 9863
_PATH = _PARSED.path or "/query"


@dataclass(frozen=True)
class SixKQuery:
    raw: dict

    @property
    def player(self) -> dict:
        return self.raw.get("player") or {}

    @property
    def track(self) -> dict:
        return self.raw.get("track") or {}

    @property
    def has_song(self) -> bool:
        return bool(self.player.get("hasSong"))

    def to_playback(self) -> Playback:
        track = self.track
        player = self.player
        song_id = str(track.get("id") or "")
        if song_id.startswith("NCM-"):
            song_id = song_id[4:]
        return Playback(
            title=str(track.get("title") or ""),
            artist=str(track.get("author") or ""),
            album=str(track.get("album") or ""),
            playing=not bool(player.get("isPaused", True)),
            position=float(player.get("seekbarCurrentPosition") or 0.0),
            duration=float(track.get("duration") or 0.0) or None,
            rate=1.0,
            aumid=f"6k-labs:{song_id}" if song_id else "6k-labs",
            song_id=song_id,
        )


def fetch(timeout: float = 0.8) -> SixKQuery | None:
    """Return the plugin's query payload, or ``None`` when it is not running."""
    try:
        with urllib.request.urlopen(ENDPOINT, timeout=timeout) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, ValueError) as exc:
        log.debug("6k-labs endpoint unavailable: %s", exc)
        return None
    if not isinstance(payload, dict):
        return None
    return SixKQuery(payload)


def read_playback(timeout: float = 0.8) -> Playback | None:
    """Return playback info from 6K Labs, or ``None`` when unavailable/empty."""
    query = fetch(timeout)
    if query is None or not query.has_song:
        return None
    playback = query.to_playback()
    return playback if playback.valid else None


class SixKClient:
    """Persistent Keep-Alive client for 6K Labs with offline exponential backoff.

    When the plugin is not installed or NetEase is closed, probing a closed
    loopback port at 10 Hz wastes executor threads and stalls on Windows TCP
    SYN retries.  This client backs off when offline and reuses a single
    socket when online.
    """

    def __init__(
        self,
        *,
        host: str = _HOST,
        port: int = _PORT,
        path: str = _PATH,
        online_timeout: float = 0.6,
        offline_timeout: float = 0.25,
        backoff_schedule: tuple[float, ...] = (2.0, 5.0, 10.0),
        clock=time.monotonic,
        connection_factory=http.client.HTTPConnection,
    ) -> None:
        self.host = host
        self.port = port
        self.path = path
        self.online_timeout = online_timeout
        self.offline_timeout = offline_timeout
        self.backoff_schedule = backoff_schedule
        self._clock = clock
        self._connection_factory = connection_factory
        self._lock = threading.Lock()
        self._conn: http.client.HTTPConnection | None = None
        self._online = False
        self._failures = 0
        self._next_probe_at = 0.0
        self.reads = 0
        self.skips = 0
        self.errors = 0

    @property
    def online(self) -> bool:
        return self._online

    def reset_backoff(self) -> None:
        with self._lock:
            self._next_probe_at = 0.0

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    def _request_once_locked(self, timeout: float) -> dict | None:
        for attempt in range(2):
            reused = self._conn is not None
            if self._conn is None:
                self._conn = self._connection_factory(self.host, self.port, timeout=timeout)
            else:
                self._conn.timeout = timeout
            try:
                self._conn.request("GET", self.path, headers={"Connection": "keep-alive"})
                response = self._conn.getresponse()
                body = response.read()
                if response.status != 200:
                    return None
                payload = json.loads(body.decode("utf-8", "replace"))
                return payload if isinstance(payload, dict) else None
            except (http.client.HTTPException, OSError, ValueError):
                self._close_locked()
                if reused and attempt == 0:
                    # Stale keep-alive socket closed by peer; retry once on a fresh socket.
                    continue
                raise
        return None

    def read(self) -> tuple[bool, Playback | None]:
        """Return ``(online, playback)``; honours offline backoff without network I/O."""
        with self._lock:
            now = self._clock()
            if not self._online and now < self._next_probe_at:
                self.skips += 1
                return False, None
            timeout = self.online_timeout if self._online else self.offline_timeout
            self.reads += 1
            try:
                payload = self._request_once_locked(timeout)
            except (http.client.HTTPException, OSError, ValueError) as exc:
                self._online = False
                self._failures += 1
                self.errors += 1
                delay = self.backoff_schedule[min(self._failures - 1, len(self.backoff_schedule) - 1)]
                self._next_probe_at = self._clock() + delay
                log.debug("6k-labs unavailable (backoff %.1fs): %s", delay, exc)
                return False, None

            self._online = True
            self._failures = 0
            self._next_probe_at = 0.0
            if payload is None:
                return True, None
            query = SixKQuery(payload)
            if not query.has_song:
                return True, None
            playback = query.to_playback()
            return True, playback if playback.valid else None

    def diagnostics(self) -> dict[str, object]:
        with self._lock:
            remaining = max(0.0, self._next_probe_at - self._clock()) if not self._online else 0.0
            return {
                "online": self._online,
                "reads": self.reads,
                "skips": self.skips,
                "errors": self.errors,
                "backoff_remaining": round(remaining, 2),
            }
