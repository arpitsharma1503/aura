"""Background picture: format and size checked from the bytes, one file kept, served back
with the right type, edit access needed, and Display settings follow it."""

import struct

import pytest

from app.background import BackgroundError, inspect
from conftest import make_client


def png(w=1920, h=1080) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00" + b"\x00" * 40


def jpeg(w=1600, h=900) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", h, w) + b"\x03" + b"\x00" * 9
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def webp(w=1280, h=720) -> bytes:
    body = b"VP8X" + struct.pack("<I", 10) + b"\x00" * 4 + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little")
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + body


@pytest.mark.parametrize("data, kind, size", [(png(), "png", (1920, 1080)), (jpeg(), "jpeg", (1600, 900)), (webp(), "webp", (1280, 720))])
def test_formats_and_sizes_read_from_the_bytes(data, kind, size):
    assert inspect(data) == (kind, *size)


@pytest.mark.parametrize("data, says", [
    (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "JPEG, PNG or WebP"),     # not a raster picture (could carry script)
    (b"GIF89a" + b"\x00" * 50, "JPEG, PNG or WebP"),
    (png(100, 80), "between"),
    (png(20000, 1000), "between"),
    (png() + b"\x00" * (13 * 1024 * 1024), "too large"),
    (b"\xff\xd8\xff" + b"\x00" * 30, "couldn't be read"),
])
def test_bad_pictures_are_refused(data, says):
    with pytest.raises(BackgroundError) as e:
        inspect(data)
    assert says in str(e.value)


def lan(tmp_path):
    return make_client(tmp_path, edit_access="lan")


def test_upload_serve_replace_and_remove(tmp_path):
    c = lan(tmp_path)
    assert c.get("/api/display/background").status_code == 404
    r = c.put("/api/display/background", content=png(), headers={"Content-Type": "image/png"})
    assert r.status_code == 200, r.text
    d = r.json()["display"]
    assert d["background"] == "image" and d["background_version"] and r.json()["picture"]["width"] == 1920
    got = c.get("/api/display/background")
    assert got.status_code == 200 and got.headers["content-type"] == "image/png" and got.content == png()
    assert c.get("/api/config").json()["display"]["background"] == "image"
    r2 = c.put("/api/display/background", content=jpeg(), headers={"Content-Type": "image/jpeg"})   # replaces, one file kept
    assert r2.json()["display"]["background_version"] != d["background_version"]
    assert sorted(p.name for p in tmp_path.glob("background.*")) == ["background.jpg"]
    assert c.get("/api/display/background").headers["content-type"] == "image/jpeg"
    gone = c.delete("/api/display/background")
    assert gone.status_code == 200 and gone.json()["display"]["background"] == "aura"
    assert c.get("/api/display/background").status_code == 404 and not list(tmp_path.glob("background.*"))


def test_refused_upload_changes_nothing(tmp_path):
    c = lan(tmp_path)
    c.put("/api/display/background", content=png(), headers={"Content-Type": "image/png"})
    r = c.put("/api/display/background", content=b"<html>", headers={"Content-Type": "image/png"})
    assert r.status_code == 422 and "JPEG, PNG or WebP" in r.json()["detail"]
    assert c.get("/api/display/background").content == png()


def test_choosing_aura_light_keeps_the_picture_for_later(tmp_path):
    c = lan(tmp_path)
    c.put("/api/display/background", content=png(), headers={"Content-Type": "image/png"})
    assert c.patch("/api/settings/display", json={"background": "aura"}).status_code == 200
    assert c.get("/api/display/background").status_code == 200          # still there to switch back to
    assert c.patch("/api/settings/display", json={"background_dim": 90}).status_code == 422


def test_image_without_a_picture_is_refused(tmp_path):
    r = lan(tmp_path).patch("/api/settings/display", json={"background": "image"})
    assert r.status_code == 422 and "Upload a picture first" in r.text


def test_changes_need_edit_access(tmp_path):
    c = make_client(tmp_path, client_ip="192.168.1.50")                 # pin mode, not unlocked
    assert c.put("/api/display/background", content=png(), headers={"Content-Type": "image/png"}).status_code == 401
    assert c.delete("/api/display/background").status_code == 401
