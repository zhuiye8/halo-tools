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

import json
import logging
import urllib.request
from dataclasses import dataclass

from .media import Playback

log = logging.getLogger(__name__)

ENDPOINT = "http://127.0.0.1:9863/query"


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