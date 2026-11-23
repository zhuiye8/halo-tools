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


def test_sixk_client_keepalive_and_offline_backoff():
    now = {"t": 10.0}
    created = {"n": 0}
    fail = {"on": False}

    class FakeResp:
        status = 200

        def read(self):
            return json.dumps(SAMPLE).encode()

    class FakeConn:
        def __init__(self, host, port, timeout=0.6):
            created["n"] += 1
            self.timeout = timeout
            self.closed = False

        def request(self, method, path, headers=None):
            if fail["on"]:
                raise OSError("connection refused")

        def getresponse(self):
            return FakeResp()

        def close(self):
            self.closed = True

    client = sixk.SixKClient(
        backoff_schedule=(2.0, 5.0),
        clock=lambda: now["t"],
        connection_factory=FakeConn,
    )
    online, pb1 = client.read()
    assert online is True and pb1 is not None and pb1.title == "如愿"
    online, pb2 = client.read()
    assert online is True and pb2 is not None
    assert created["n"] == 1  # Keep-Alive socket reused

    # Going offline triggers exponential backoff without hitting socket again.
    fail["on"] = True
    online, pb3 = client.read()
    assert online is False and pb3 is None
    reads_after_fail = client.reads

    now["t"] += 1.0
    online, pb4 = client.read()
    assert online is False and pb4 is None
    assert client.reads == reads_after_fail
    assert client.skips == 1

    # After 2.0s backoff expires, probes again and recovers when server returns.
    fail["on"] = False
    now["t"] += 1.5
    online, pb5 = client.read()
    assert online is True and pb5 is not None
    client.close()