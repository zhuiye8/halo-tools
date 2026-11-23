"""Updater tests: version ordering, release parsing, download verification."""

from __future__ import annotations

import hashlib
import io
import zipfile

import pytest

from halobar import update


def test_version_ordering():
    assert update.version_tuple("v0.1.1") == (0, 1, 1)
    assert update.is_newer("0.1.1", "0.1.0")
    assert update.is_newer("v0.2.0", "0.1.9")
    assert not update.is_newer("0.1.0", "0.1.0")
    assert not update.is_newer("0.0.9", "0.1.0")


def test_parse_release_picks_windows_zip():
    payload = {
        "tag_name": "v0.1.1",
        "name": "halobar v0.1.1",
        "body": "更新说明",
        "html_url": "https://example/rel",
        "published_at": "2026-11-24T00:00:00Z",
        "assets": [
            {"name": "checksums.txt", "browser_download_url": "u0", "size": 10},
            {"name": "halobar-v0.1.1-win64.zip", "browser_download_url": "u1",
             "size": 1024, "digest": "sha256:abc"},
        ],
    }
    release = update.parse_release(payload)
    assert release.version == "0.1.1"
    assert release.asset_name == "halobar-v0.1.1-win64.zip"
    assert release.asset_url == "u1"
    assert release.available


def test_release_without_asset_is_not_available():
    release = update.parse_release({"tag_name": "v1.0.0", "assets": []})
    assert release.available is False


class FakeResponse:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.headers = {"Content-Length": str(len(data))}

    def read(self, size: int) -> bytes:
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def open(self, request, timeout=None):
        return FakeResponse(self.data)


def make_release(data: bytes) -> update.Release:
    return update.Release(
        version="0.1.1", tag="v0.1.1", name="halobar v0.1.1", notes="",
        html_url="", asset_url="https://example/pkg.zip",
        asset_name="halobar-v0.1.1-win64.zip", asset_size=len(data),
        digest="sha256:" + hashlib.sha256(data).hexdigest(),
    )


def test_download_verifies_checksum(tmp_path, monkeypatch):
    data = b"fake zip payload"
    monkeypatch.setattr(update, "_opener", lambda proxy: FakeOpener(data))
    monkeypatch.setattr(update, "_curl_available", lambda: False)
    target = tmp_path / "pkg.zip"
    progress: list[float] = []
    update.download(make_release(data), target, proxy="none", progress=progress.append)
    assert target.read_bytes() == data
    assert progress and progress[-1] == pytest.approx(1.0)


def test_download_rejects_bad_checksum(tmp_path, monkeypatch):
    good = b"expected bytes"
    bad = b"tampered bytes!"
    release = make_release(good)
    monkeypatch.setattr(update, "_opener", lambda proxy: FakeOpener(bad))
    monkeypatch.setattr(update, "_curl_available", lambda: False)
    target = tmp_path / "pkg.zip"
    with pytest.raises(update.UpdateError):
        update.download(release, target, proxy="none")
    assert not target.exists()


def test_extract_requires_halobar_exe(tmp_path):
    zip_path = tmp_path / "pkg.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("README.txt", "hi")
    with pytest.raises(update.UpdateError):
        update.extract(zip_path, tmp_path / "staging")


def test_extract_ok(tmp_path):
    zip_path = tmp_path / "pkg.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("halobar.exe", "MZ")
        archive.writestr("_internal/x.txt", "data")
    staging = tmp_path / "staging"
    update.extract(zip_path, staging)
    assert (staging / "halobar.exe").exists()
    assert (staging / "_internal" / "x.txt").exists()


def test_launch_updater_requires_frozen_build():
    with pytest.raises(update.UpdateError):
        update.launch_updater(__import__("pathlib").Path("/tmp/nope"))


def test_resolve_proxy():
    assert update.resolve_proxy("none") is None
    assert update.resolve_proxy(None) is None
    assert update.resolve_proxy("http://127.0.0.1:7897") == "http://127.0.0.1:7897"