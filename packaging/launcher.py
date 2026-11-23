"""Frozen-entry launcher: one place that calls the CLI main (used by PyInstaller)."""

from halobar.app import main

if __name__ == "__main__":
    raise SystemExit(main())