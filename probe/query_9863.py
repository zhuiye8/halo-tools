"""Check whether the 6K Labs plugin endpoint is live and show playback state.

Run after installing BetterNCM + InfLink-rs + 6K Labs and restarting NetEase:

    python query_9863.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

ENDPOINT = "http://127.0.0.1:9863/query"


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    for index in range(rounds):
        try:
            with urllib.request.urlopen(ENDPOINT, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001 - diagnostic script
            print(f"[{index}] endpoint unavailable: {exc}")
            print("  -> 6K Labs plugin not installed, or NetEase is not running")
            return 1
        player = payload.get("player") or {}
        track = payload.get("track") or {}
        print(f"[{index}] hasSong={player.get('hasSong')} paused={player.get('isPaused')} "
              f"pos={player.get('seekbarCurrentPosition')}s ({player.get('seekbarCurrentPositionHuman')}) "
              f"dur={track.get('duration')}s")
        print(f"      {track.get('title')!r} - {track.get('author')!r} "
              f"id={track.get('id')!r} album={track.get('album')!r}")
        if rounds > 1:
            time.sleep(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())