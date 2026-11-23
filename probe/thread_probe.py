"""Reproduce the console's thread growth and print what the threads are doing.

Runs a real ConsoleServer with a service that never touches hardware, hammers
/api/status like an open browser tab, then dumps the live thread stacks.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halobar.server import ConsoleServer  # noqa: E402
from halobar.service import LyricsService, ServiceConfig  # noqa: E402

REQUESTS = int(sys.argv[1]) if len(sys.argv) > 1 else 400
INTERVAL = float(sys.argv[2]) if len(sys.argv) > 2 else 0.02


def main() -> int:
    def no_device():
        raise OSError("no device in this probe")

    async def no_playback(preferred=None, use_sixk=True):
        return None

    service = LyricsService(ServiceConfig(poll_seconds=0.1), screen_factory=no_device,
                            reader=no_playback)
    console = ConsoleServer(service, port=0)
    url = console.start()
    token = url.split("token=")[1]
    base = f"http://127.0.0.1:{console.port}/api/status?token={token}"

    print(f"console on {console.port}; sending {REQUESTS} requests...")
    for index in range(REQUESTS):
        try:
            with urllib.request.urlopen(base, timeout=5) as response:
                response.read()
        except Exception as exc:  # noqa: BLE001
            print("request failed:", exc)
            break
        time.sleep(INTERVAL)

    time.sleep(0.5)
    frames = sys._current_frames()
    threads = threading.enumerate()
    print(f"\nlive threads: {len(threads)}")

    groups: dict[str, int] = {}
    for thread in threads:
        frame = frames.get(thread.ident)
        stack = "".join(traceback.format_stack(frame)) if frame else "(no frame)"
        key = stack.strip().splitlines()[-1] if stack.strip() else "(unknown)"
        groups[key] = groups.get(key, 0) + 1
    for key, count in sorted(groups.items(), key=lambda item: -item[1])[:12]:
        print(f"  {count:5d}  {key}")

    console.stop()
    service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())