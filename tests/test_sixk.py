"""6K Labs local endpoint parsing (no plugin required for the tests)."""

from __future__ import annotations

import json

from halobar import sixk


SAMPLE = {
    "player": {
        "hasSong": True,
        "isPaused": False,
        "volumePercent": 80.0,
        "seekbarCurrentPosition": 128.4,
        "seekbarCurrentPositionHuman": "2:08",
        "statePercent": 48.2,
        "likeStatus": "INDIFFERENT",
        "repeatType": "ALL",
    },
    "track": {
        "author": "王菲",
        "title": "如愿",
        "album": "如愿",
        "cover": "",
        "duration": 265.0,
        "durationHuman": "4:25",
        "url": "",
        "id": "NCM-1888381008",
        "isVideo": False,
        "isAdvertisement": False,
        "inLibrary": True,
    },
}

EMPTY = {
    "player": {"hasSong": False, "isPaused": True, "seekbarCurrentPosition": 0.0},
    "track": {"title": "", "author": "", "duration": 0.0, "id": ""},
}


def test_parse_sample_query():
    query = sixk.SixKQuery(SAMPLE)
    assert query.has_song
    playback = query.to_playback()
    assert playback.title == "如愿"
    assert playback.artist == "王菲"
    assert playback.playing is True
    assert playback.position == 128.4
    assert playback.duration == 265.0
    assert playback.aumid == "6k-labs:1888381008"


def test_empty_query_is_not_playback():
    assert sixk.SixKQuery(EMPTY).has_song is False


def test_fetch_returns_none_when_endpoint_missing(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(sixk.urllib.request, "urlopen", boom)
    assert sixk.fetch() is None
    assert sixk.read_playback() is None


def test_fetch_parses_json(monkeypatch):
    class FakeResponse:
        status = 200
        _body = json.dumps(SAMPLE).encode()

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(sixk.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    playback = sixk.read_playback()
    assert playback is not None
    assert playback.position == 128.4