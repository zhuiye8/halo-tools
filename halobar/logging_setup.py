"""Logging setup: rotating file log, optional console output, crash hook."""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
import traceback
from pathlib import Path

LOGGER_NAME = "halobar"


def setup_logging(log_directory: Path, *, console: bool = True, level: int = logging.INFO) -> Path:
    """Configure the ``halobar`` logger and return the active log file path."""
    log_directory.mkdir(parents=True, exist_ok=True)
    log_file = log_directory / "halobar.log"
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if console and sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        logger.addHandler(stream)

    return log_file


def install_crash_hook(log_directory: Path) -> None:
    """Write uncaught exceptions to ``crash.log`` instead of losing them."""
    log_directory.mkdir(parents=True, exist_ok=True)
    crash_file = log_directory / "crash.log"

    def write(kind: str, exc_type, exc_value, exc_tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            with crash_file.open("a", encoding="utf-8") as handle:
                handle.write(f"==== {kind} ====\n{text}\n")
        except OSError:
            pass
        logging.getLogger(LOGGER_NAME).critical("uncaught %s: %s", kind, exc_value)

    def excepthook(exc_type, exc_value, exc_tb):
        write("main", exc_type, exc_value, exc_tb)

    def thread_excepthook(args):
        write(f"thread {args.thread.name if args.thread else '?'}",
              args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook