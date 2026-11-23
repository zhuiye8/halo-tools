"""Build metadata: which commit this binary was built from.

``scripts/build.ps1`` writes ``build-info.json`` next to the executable, so a
downloaded package can always be traced back to a commit.  During development the
file does not exist and the loader simply returns ``{}``.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def _candidates() -> list[Path]:
    if getattr(sys, "frozen", False):
        return [Path(sys.executable).resolve().parent / "build-info.json"]
    return [Path(__file__).resolve().parent.parent / "build-info.json"]


def load() -> dict:
    for path in _candidates():
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.debug("build-info unreadable: %s", exc)
            continue
        return data if isinstance(data, dict) else {}
    return {}


def describe() -> str:
    info = load()
    if not info:
        return "dev"
    parts = [str(info.get("version") or "?"), "@" + str(info.get("commit") or "?")]
    return " ".join(parts)