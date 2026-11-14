"""Security hardening found in the pen test: request bodies are capped before they are read
(a 30 MB JSON body used to be read whole, ~150 MB of memory per request), and the data
folder is private to the AURA user."""

import stat
import struct

from conftest import make_client


def png(extra: int = 0) -> bytes:
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 1920, 1080)
            + b"\x08\x02\x00\x00\x00" + b"\x00" * (40 + extra))


def lan(tmp_path):
    return make_client(tmp_path, edit_access="lan")


def test_oversized_json_is_refused_before_it_is_read(tmp_path):
    c = lan(tmp_path)
    big = b'{"x":"' + b"a" * (2 * 1024 * 1024) + b'"}'
    r = c.patch("/api/settings/display", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and "too large" in r.text.lower()
    r = c.post("/api/groceries", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_oversized_body_without_a_length_is_cut_off(tmp_path):
    c = lan(tmp_path)

    def chunks():
        for _ in range(40):
            yield b"a" * 65536                           # 2.5 MB, sent chunked (no Content-Length)
    r = c.post("/api/groceries", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_normal_requests_and_wallpapers_still_work(tmp_path):
    c = lan(tmp_path)
    assert c.post("/api/groceries", json={"name": "Milk"}).status_code == 201
    r = c.put("/api/display/background", content=png(5 * 1024 * 1024), headers={"Content-Type": "image/png"})
    assert r.status_code == 200, r.text                   # pictures keep their own 12 MB limit
    r = c.put("/api/display/background", content=png(13 * 1024 * 1024), headers={"Content-Type": "image/png"})
    assert r.status_code == 413


def test_data_folder_is_private(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data").chmod(0o775)
    make_client(tmp_path / "data")
    assert stat.S_IMODE((tmp_path / "data").stat().st_mode) == 0o700
