"""Tests for v0.3.0 Pixel Studio 2.0: billboard renderer, asset library & server routes."""

from __future__ import annotations

import base64
import io

import pytest

from halobar import billboard, bitmap
from halobar.library import AssetLibrary, build_preset, builtin_presets
from halobar.server import ConsoleServer
from halobar.service import LyricsService, ServiceConfig

pytest.importorskip("PIL")


def _gif_b64(frames: int = 12) -> str:
    from PIL import Image

    buf = io.BytesIO()
    imgs = [Image.new("RGB", (64, 32), ((i * 21) % 256, 180, 90)) for i in range(frames)]
    imgs[0].save(buf, format="GIF", save_all=True, append_images=imgs[1:], duration=100, loop=0)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_billboard_static_and_animated_render():
    static = billboard.render("FOCUS MODE", font_style="bold", font_size=22, bold_stroke=1, border="corners")
    assert len(static.frames) == 1
    assert static.animated is False
    assert static.category == bitmap.STATIC_CATEGORY
    assert len(bitmap.encode(static)) == 8 * 133

    anim = billboard.render("CODING", subtext="DO NOT DISTURB", motion="bars", border="frame", delay_ms=250)
    assert len(anim.frames) == 4
    assert anim.animated is True
    assert anim.category == bitmap.ANIMATION_CATEGORY
    assert len(bitmap.encode(anim)) == 4 * 8 * 133


def test_billboard_rejects_empty():
    with pytest.raises(bitmap.AssetError):
        billboard.render("   ")


def test_asset_library_crud_and_presets(tmp_path):
    lib = AssetLibrary(tmp_path / "assets")
    assert lib.list_items() == []

    asset = billboard.render("HELLO", motion="static")
    stream = bitmap.encode(asset)
    saved = lib.save_asset(asset, stream, name="我的招牌", kind="billboard")
    assert saved["name"] == "我的招牌"
    assert saved["preview"].startswith("data:image/png;base64,")

    items = lib.list_items()
    assert len(items) == 1
    item, payload = lib.load_payload(saved["id"])
    assert item.name == "我的招牌"
    assert payload == stream

    renamed = lib.rename(saved["id"], "深夜模式")
    assert renamed["name"] == "深夜模式"
    assert lib.list_items()[0]["name"] == "深夜模式"

    # Persistence across reload
    reloaded = AssetLibrary(tmp_path / "assets")
    assert len(reloaded.list_items()) == 1
    assert reloaded.list_items()[0]["name"] == "深夜模式"

    assert reloaded.delete(saved["id"]) is True
    assert reloaded.list_items() == []
    assert reloaded.delete(saved["id"]) is False


def test_builtin_presets_encode_cleanly():
    presets = builtin_presets()
    assert len(presets) >= 4
    for p in presets:
        asset = build_preset(p["id"])
        stream = bitmap.encode(asset)
        assert len(stream) == len(asset.frames) * 8 * 133


def test_server_studio_endpoints(tmp_path):
    svc = LyricsService(ServiceConfig())
    lib = AssetLibrary(tmp_path / "lib")
    srv = ConsoleServer(svc, port=0, token="tok", library=lib)

    # 1. Multi-frame GIF inspect (12 -> 8 sampled)
    code, insp = srv.handle_asset_inspect({"filename": "demo.gif", "data": _gif_b64(12)})
    assert code == 200
    assert insp["source_frames"] == 12
    assert insp["sampled"] is True
    assert len(insp["frames"]) == 8

    # 2. Upload with frame subset + save to library only
    code, up = srv.handle_asset_upload({
        "filename": "demo.gif",
        "data": _gif_b64(12),
        "selected_indices": [0, 2, 4, 6],
        "speed": 1.5,
        "upload_to_device": False,
        "save_to_library": True,
    })
    assert code == 200
    assert up["frames"] == 4
    assert up["uploaded"] is False
    assert up["library_item"] is not None
    item_id = up["library_item"]["id"]

    # 3. Billboard render & upload
    code, bb = srv.handle_billboard_render({"text": "HALO", "motion": "cursor"})
    assert code == 200
    assert bb["animated"] is True
    assert len(bb["frames"]) == 4

    code, bb_up = srv.handle_billboard_upload({
        "text": "HALO",
        "motion": "static",
        "upload_to_device": True,
        "save_to_library": True,
    })
    assert code == 200
    assert bb_up["library_item"] is not None

    # 4. Library list, push, rename, preset, delete
    code, listing = srv.handle_library_list()
    assert code == 200
    assert len(listing["items"]) == 2
    assert len(listing["presets"]) >= 4

    code, pushed = srv.handle_library_push({"id": item_id})
    assert code == 200
    assert pushed["frames"] == 4

    code, ren = srv.handle_library_rename({"id": item_id, "name": "四帧动图"})
    assert code == 200
    assert ren["item"]["name"] == "四帧动图"

    code, pr = srv.handle_library_preset({"preset_id": "eq-wave", "save_to_library": True, "upload_to_device": False})
    assert code == 200
    assert pr["frames"] == 6

    code, dele = srv.handle_library_delete({"id": item_id})
    assert code == 200
    assert dele["ok"] is True
