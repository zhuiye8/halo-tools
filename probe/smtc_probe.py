"""Probe Windows SMTC media sessions (Python / WinRT projection)."""
import asyncio

from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as Manager,
)


async def main():
    mgr = await Manager.request_async()
    sessions = list(mgr.get_sessions())
    print(f"sessions: {len(sessions)}")
    for s in sessions:
        print(f"--- AUMID: {s.source_app_user_model_id}")
        try:
            media = await s.try_get_media_properties_async()
            print(f"    title={media.title!r} artist={media.artist!r} album={media.album!r} thumb={media.thumbnail is not None}")
        except Exception as e:
            print(f"    media FAILED: {e}")
        try:
            info = s.get_playback_info()
            print(f"    status={info.playback_status} rate={info.playback_rate}")
        except Exception as e:
            print(f"    playback FAILED: {e}")
        try:
            t = s.get_timeline_properties()
            print(f"    start={t.start_time.total_seconds():.1f}s pos={t.position.total_seconds():.1f}s "
                  f"end={t.end_time.total_seconds():.1f}s updated={t.last_updated_time}")
        except Exception as e:
            print(f"    timeline FAILED: {e}")


if __name__ == "__main__":
    asyncio.run(main())