"""Settings persistence and environment detection tests."""

from __future__ import annotations

import json
import sys

import pytest

from halobar import environment, settings


def test_settings_roundtrip(tmp_path):
    path = tmp_path / "settings.json"
    config = settings.Settings.load(path)
    assert config.token
    config.align = "left"
    config.animation = 3
    config.standby_after = 42
    config.save(path)

    again = settings.Settings.load(path)
    assert again.token == config.token
    assert again.align == "left"
    assert again.animation == 3
    assert again.standby_after == 42


def test_v02_settings_and_per_track_memory(tmp_path):
    path = tmp_path / "settings.json"
    config = settings.Settings.load(path)
    config.apply_lyric_lang("bilingual")
    config.apply_theme("dark")
    config.apply_color([130, 105, 255])
    config.set_track_offset("如愿\n王菲", 1.5)
    config.set_track_song_id("如愿\n王菲", "1888381008")
    config.save(path)

    again = settings.Settings.load(path)
    assert again.lyric_lang == "bilingual"
    assert again.theme == "dark"
    assert again.color == [130, 105, 255]
    assert again.get_track_offset("如愿\n王菲") == 1.5
    assert again.get_track_song_id("如愿\n王菲") == "1888381008"

    # Zeroing offset removes the entry cleanly.
    again.set_track_offset("如愿\n王菲", 0.0)
    assert "如愿\n王菲" not in again.track_offsets


def test_settings_recovers_from_corruption(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    config = settings.Settings.load(path)
    assert config.token
    assert (tmp_path / "settings.corrupt.json").exists()


def test_console_url_uses_token_and_port(tmp_path):
    config = settings.Settings.load(tmp_path / "settings.json")
    url = config.console_url(8790)
    assert url.startswith("http://127.0.0.1:8790/?token=")
    assert config.token in url


def test_apply_validation():
    config = settings.Settings()
    config.apply_align("bogus")
    assert config.align == "center"
    config.apply_align("right")
    assert config.align == "right"
    config.apply_animation(9)
    assert config.animation is None
    config.apply_animation(2)
    assert config.animation == 2


def test_sixk_available_handles_missing_endpoint(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("refused")

    monkeypatch.setattr(environment.urllib.request, "urlopen", boom)
    assert environment.sixk_available() is False


def test_sixk_available_parses_payload(monkeypatch):
    class Response:
        def read(self):
            return json.dumps({"player": {"hasSong": True}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(environment.urllib.request, "urlopen", lambda *a, **k: Response())
    assert environment.sixk_available() is True


def test_environment_monitor_caches(monkeypatch):
    calls = {"n": 0}

    def fake_sixk(timeout=0.5):
        calls["n"] += 1
        return True

    monkeypatch.setattr(environment, "sixk_available", fake_sixk)
    monkeypatch.setattr(environment, "tempo_hub_running", lambda timeout=3.0: False)
    monitor = environment.EnvironmentMonitor(ttl=60)
    monitor.refresh_now()
    first = monitor.get()
    second = monitor.get()
    assert first.sixk is True
    assert second is first
    assert calls["n"] == 1


def test_environment_monitor_get_is_non_blocking():
    monitor = environment.EnvironmentMonitor(ttl=60)
    snapshot = monitor.get()
    assert snapshot.sixk is False          # no probe yet, but returns instantly
    assert snapshot.as_dict()["links"]["sixk"]


@pytest.mark.skipif(sys.platform != "win32", reason="named mutex is Windows only")
def test_single_instance_detects_second():
    from halobar.single_instance import SingleInstance

    first = SingleInstance("halobar-test-mutex")
    try:
        second = SingleInstance("halobar-test-mutex")
        try:
            assert first.already_running is False
            assert second.already_running is True
        finally:
            second.release()
    finally:
        first.release()