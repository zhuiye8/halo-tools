"""Command line entry points: probe, watch, lyrics and a live sync demo.

Examples::

    python -m halobar probe
    python -m halobar watch --seconds 10
    python -m halobar lyrics --title 如愿 --artist 王菲
    python -m halobar demo --seconds 30 --offset 60 --animation 2
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
import webbrowser
from pathlib import Path

from . import __version__

from .device.screen import Align, RenderError, Screen, Scroll
from .lyrics import NeteaseClient
from .media import Playback, read_playback
from .sync import EngineConfig, LyricsEngine, segment_text

DEFAULT_CACHE = Path(__file__).resolve().parent.parent / ".cache" / "lyrics"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_probe(args: argparse.Namespace) -> int:
    with Screen() as screen:
        print(f"firmware      : {screen.firmware}")
        print(f"protocol      : {screen.protocol_version}")
        print(f"state         : {screen.state().raw.hex(' ')}")
        for source, label in ((0x00, "custom"), (0x0E, "lyric")):
            max_chars, max_bytes = screen.capabilities(source)
            print(f"slot {label:6s} : max_chars={max_chars} max_bytes={max_bytes} "
                  f"text={screen.get_text(source)!r}")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    async def run() -> None:
        end = asyncio.get_running_loop().time() + args.seconds
        last = None
        while asyncio.get_running_loop().time() < end:
            playback = await read_playback(args.aumid)
            if playback is None:
                print("no media session")
            else:
                line = (f"{playback.aumid}: {playback.title!r}/{playback.artist!r} "
                        f"playing={playback.playing} pos={playback.position} dur={playback.duration}")
                if line != last:
                    print(line)
                    last = line
            await asyncio.sleep(0.5)
    asyncio.run(run())
    return 0


def cmd_lyrics(args: argparse.Namespace) -> int:
    title, artist = args.title, args.artist
    if not title:
        playback = asyncio.run(read_playback(args.aumid))
        if playback is None or not playback.title:
            print("no media session and no --title given", file=sys.stderr)
            return 2
        title, artist = playback.title, playback.artist
    client = NeteaseClient(args.cache)
    lyrics = client.lyrics_for(title, artist)
    print(f"# {lyrics.title} - {lyrics.artist} [{lyrics.source}] {len(lyrics.lines)} lines")
    for line in lyrics.lines:
        chunks = segment_text(line.text)
        print(f"{line.start:7.2f}  {line.text}  -> {len(chunks)} chunk(s): {chunks}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    client = NeteaseClient(args.cache)
    config = EngineConfig(
        align=Align(args.align),
        scroll=Scroll(args.scroll) if args.scroll else None,
        animation=args.animation,
        standby_scene=None if args.no_standby else args.standby_scene,
        standby_after=args.standby_after,
        preferred_aumid=args.aumid,
    )

    async def run(screen: Screen) -> None:
        playback = await read_playback(args.aumid)
        title = args.title or (playback.title if playback else "")
        artist = args.artist or (playback.artist if playback else "")
        if not title:
            raise SystemExit("no media session and no --title given")
        duration = (playback.duration or 0.0) if playback else 0.0
        song_id = (playback.song_id or "") if playback else ""
        lyrics = client.lyrics_for(title, artist, duration, song_id)
        if not lyrics:
            raise SystemExit(f"no lyrics found for {title!r}/{artist!r}")
        engine = LyricsEngine(screen, config)
        engine.set_lyrics(lyrics)
        if args.offset:
            engine.clock.offset = args.offset
        log = logging.getLogger("halobar.demo")
        log.info("demo: %s - %s (%d lines, offset %+.1fs)", title, artist,
                 len(lyrics.lines), args.offset)
        log.info("press Ctrl+C to stop; screen state is restored on exit")
        await engine.run(duration=args.seconds)

    with Screen() as screen:
        try:
            asyncio.run(run(screen))
        except KeyboardInterrupt:
            print("\ninterrupted")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run the background service with the web console and (optionally) the tray."""
    from . import __version__, settings as settings_module
    from .environment import EnvironmentMonitor
    from .logging_setup import install_crash_hook, setup_logging
    from .server import ConsoleServer
    from .service import LyricsService, ServiceConfig
    from .single_instance import SingleInstance
    from .tray import TRAY_AVAILABLE, run_tray

    config = settings_module.Settings.load()
    if args.console_port:
        config.console_port = args.console_port
    log_file = setup_logging(settings_module.log_dir(), console=not args.no_tray)
    install_crash_hook(settings_module.log_dir())
    logging.getLogger("halobar").info("halobar %s starting (log: %s)", __version__, log_file)

    guard = SingleInstance()
    if guard.already_running and not args.allow_second:
        url = config.console_url()
        logging.getLogger("halobar").info("another instance is running; opening %s", url)
        webbrowser.open(url)
        return 0

    service = LyricsService(ServiceConfig(
        align=Align(config.align),
        animation=config.animation,
        standby_scene=config.standby_scene,
        standby_scene_option=config.standby_scene_option,
        standby_after=config.standby_after,
        scroll_long_lines=config.scroll_long_lines,
    ))

    console = None
    console_url = config.console_url()
    if not args.no_console:
        console = ConsoleServer(service, port=config.console_port,
                                token=config.token, settings=config,
                                environment=EnvironmentMonitor())
        console_url = console.start()
        config.console_port = console.port
        config.save()
        print(f"控制台: {console_url}")

    try:
        if not args.no_tray and TRAY_AVAILABLE:
            if config.open_console_on_start:
                webbrowser.open(console_url if console else config.console_url())
            print("halobar 已在托盘运行，右键托盘图标打开菜单（Ctrl+C 退出）")
            run_tray(service, console_url)
            return 0

        service.start()
        deadline = time.monotonic() + args.seconds if args.seconds else None
        while deadline is None or time.monotonic() < deadline:
            status = service.status()
            print(f"[{status.state}] {status.track or '-'} - {status.artist or '-'} "
                  f"pos={status.position if status.position is None else round(status.position, 1)} "
                  f"line={status.line!r} src={status.source} err={status.last_error or '-'}")
            time.sleep(1.0)
        return 0
    except KeyboardInterrupt:
        print("\nstopping…")
        return 0
    finally:
        service.stop()
        if console is not None:
            console.stop()
        guard.release()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="halobar", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"halobar {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser("probe", help="print device state and text slots")
    probe.set_defaults(func=cmd_probe)

    watch = sub.add_parser("watch", help="watch SMTC media sessions")
    watch.add_argument("--seconds", type=float, default=10)
    watch.add_argument("--aumid", default=None)
    watch.set_defaults(func=cmd_watch)

    lyrics = sub.add_parser("lyrics", help="fetch and inspect lyrics")
    lyrics.add_argument("--title", default="")
    lyrics.add_argument("--artist", default="")
    lyrics.add_argument("--aumid", default=None)
    lyrics.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    lyrics.set_defaults(func=cmd_lyrics)

    demo = sub.add_parser("demo", help="push lyrics to the speaker for N seconds")
    demo.add_argument("--seconds", type=float, default=60)
    demo.add_argument("--offset", type=float, default=0.0, help="seconds to add to the clock")
    demo.add_argument("--title", default="")
    demo.add_argument("--artist", default="")
    demo.add_argument("--aumid", default=None)
    demo.add_argument("--align", default="center", choices=[a.value for a in Align])
    demo.add_argument("--scroll", default=None, choices=[s.value for s in Scroll])
    demo.add_argument("--animation", type=int, default=None, choices=range(0, 5))
    demo.add_argument("--standby-scene", type=int, default=8)
    demo.add_argument("--standby-after", type=float, default=20.0)
    demo.add_argument("--no-standby", action="store_true")
    demo.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    demo.set_defaults(func=cmd_demo)

    run = sub.add_parser("run", help="run the background service (tray by default)")
    run.add_argument("--no-tray", action="store_true", help="headless mode (for smoke tests)")
    run.add_argument("--seconds", type=float, default=0.0, help="stop after N seconds (headless)")
    run.add_argument("--console", default="", help="override the console URL shown in the tray menu")
    run.add_argument("--console-port", type=int, default=0, help="override the console port")
    run.add_argument("--no-console", action="store_true", help="do not start the web console")
    run.add_argument("--allow-second", action="store_true",
                     help="run even if another instance is already running (debug)")
    run.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    # A windowed build has no console streams at all; printing would raise.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        # Double-clicking the exe (or a desktop shortcut) should start the tray app.
        argv = ["run"]

    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except RenderError as exc:
        print(f"device error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"io error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())