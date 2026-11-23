"""Diagnostic: query the latest GitHub release and print what the updater sees.

Usage:  python probe/update_check.py [current_version]
"""

from __future__ import annotations

import sys

from halobar import __version__, update


def main() -> int:
    current = sys.argv[1] if len(sys.argv) > 1 else __version__
    print(f"current: {current}")
    print(f"proxy  : {update.detect_proxy() or 'direct'}")
    try:
        release = update.fetch_latest(proxy="auto")
    except update.UpdateError as exc:
        print(f"fetch failed: {exc}")
        return 1
    print(f"latest : v{release.version} ({release.tag})")
    print(f"asset  : {release.asset_name or '-'}  {release.asset_size / 1024 / 1024:.1f} MB")
    print(f"digest : {release.digest or '-'}")
    print(f"newer  : {update.is_newer(release.version, current)}")
    print(f"url    : {release.html_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())