"""Repository sanity checks: no non-ASCII in scripts consumed by CI, valid workflow YAML."""

from __future__ import annotations

import sys
from pathlib import Path

ASCII_ONLY = [
    "scripts/build.ps1",
    "packaging/halobar.spec",
    "packaging/launcher.py",
    "packaging/version_info.txt",
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
]

WORKFLOWS = [".github/workflows/ci.yml", ".github/workflows/release.yml"]


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    problems = 0

    for name in ASCII_ONLY:
        path = root / name
        if not path.exists():
            print(f"[missing] {name}")
            problems += 1
            continue
        data = path.read_bytes()
        bad = sum(1 for byte in data if byte > 127)
        flag = "ok" if bad == 0 else f"{bad} non-ascii bytes"
        print(f"{name:38s} {len(data):7d} bytes  {flag}")
        if bad:
            problems += 1

    try:
        import yaml
    except ImportError:
        print("PyYAML not installed; skipping workflow validation")
        return problems

    for name in WORKFLOWS:
        try:
            doc = yaml.safe_load((root / name).read_text(encoding="utf-8"))
            jobs = list(doc.get("jobs", {}))
            print(f"{name:38s} yaml ok, jobs: {jobs}")
        except Exception as exc:  # noqa: BLE001
            print(f"{name:38s} yaml error: {exc}")
            problems += 1
    return problems


if __name__ == "__main__":
    sys.exit(1 if main() else 0)