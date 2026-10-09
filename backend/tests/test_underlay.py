import base64
import os
import struct
import zlib

import pytest

from backend.underlay import MAX_BYTES, UnderlayError, sniff, validate_placement


def png(w=40, h=20) -> bytes:
    raw = b"".join(b"\x00" + b"\xff\xff\xff" * w for _ in range(h))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def jpeg(w=300, h=200) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def webp_vp8x(w=640, h=480) -> bytes:
    body = b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00" + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little")
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


def upload(client, data, ctype="image/png"):
    return client.post("/api/underlay/image", content=data, headers={"Content-Type": ctype})


def test_sniff_types():
    assert sniff(png(40, 20)) == ("png", 40, 20)
    assert sniff(jpeg(300, 200)) == ("jpeg", 300, 200)
    assert sniff(webp_vp8x(640, 480)) == ("webp", 640, 480)
    for bad in (b"", b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"GIF89a\x01\x00\x01\x00", b"\xff\xd8\xff", b"RIFF"):
        with pytest.raises(UnderlayError):
            sniff(bad)
    with pytest.raises(UnderlayError, match="pixels"):
        sniff(png(20000, 10))


def test_validate_placement():
    assert validate_placement({}) == {}
    assert validate_placement({"x": 1.23456, "rot": 270, "opacity": 1}) == {"x": 1.235, "rot": -90.0, "opacity": 1.0}
    assert validate_placement({"rot": -190})["rot"] == 170.0
    assert validate_placement({"show_view": True, "invert_dark": False}) == {"show_view": True, "invert_dark": False}
    for bad in ({"width": 0.1}, {"opacity": 0}, {"x": float("nan")}, {"x": "1"}, {"show_view": 1}, {"zoom": 2}, [1],
                {"y": 1e6}, {"width": True}):
        with pytest.raises(UnderlayError):
            validate_placement(bad)


def test_none_until_uploaded(client):
    assert client.get("/api/underlay").json() == {"image": None}
    assert client.get("/api/underlay/image").status_code == 404
    assert client.put("/api/underlay", json={"opacity": 0.3}).status_code == 404


def test_upload_serve_place_delete(client, tmp_path):
    data = png(40, 20)
    r = upload(client, data)
    assert r.status_code == 200, r.text
    u = r.json()
    assert u["fresh"] is True and u["image"]["type"] == "png" and (u["image"]["w"], u["image"]["h"]) == (40, 20)
    assert u["opacity"] == 0.5 and u["show_view"] is False and u["invert_dark"] is True
    assert (tmp_path / "underlay.img").read_bytes() == data  # stored next to DB_PATH

    img = client.get("/api/underlay/image")
    assert img.status_code == 200 and img.content == data
    assert img.headers["content-type"] == "image/png" and img.headers["x-content-type-options"] == "nosniff"
    assert img.headers["cache-control"] == "private, no-cache"
    v = u["image"]["version"]
    assert "immutable" in client.get(f"/api/underlay/image?v={v}").headers["cache-control"]

    r = client.put("/api/underlay", json={"x": 2, "y": 3.5, "width": 8.25, "rot": 3, "opacity": 0.7, "show_view": True})
    assert r.status_code == 200
    assert {k: r.json()[k] for k in ("x", "y", "width", "rot", "opacity", "show_view")} == \
        {"x": 2, "y": 3.5, "width": 8.25, "rot": 3, "opacity": 0.7, "show_view": True}
    r = client.put("/api/underlay", json={"opacity": 0.4})  # partial: the rest stays
    assert r.json()["x"] == 2 and r.json()["width"] == 8.25 and r.json()["opacity"] == 0.4
    assert client.put("/api/underlay", json={"width": 900}).status_code == 400
    assert client.put("/api/underlay", content=b"{", headers={"Content-Type": "application/json"}).status_code == 400

    # Replacing keeps the placement, new size and version.
    r = upload(client, jpeg(300, 200), "image/jpeg").json()
    assert r["fresh"] is False and r["x"] == 2 and r["image"]["type"] == "jpeg" and r["image"]["version"] != v
    assert client.get("/api/underlay/image").headers["content-type"] == "image/jpeg"

    assert client.delete("/api/underlay").json() == {"removed": True}
    assert not (tmp_path / "underlay.img").exists()
    assert client.get("/api/underlay").json() == {"image": None}
    assert client.delete("/api/underlay").json() == {"removed": False}
    assert upload(client, data).json()["fresh"] is True  # a new photo after removing starts from the defaults
    assert client.get("/api/underlay").json()["x"] == 5.0


def test_upload_rejects(client, tmp_path):
    assert upload(client, b"<svg/>", "image/svg+xml").status_code == 415
    assert upload(client, b"<svg/>", "image/png").status_code == 415  # sniffed, not trusted
    assert upload(client, png(), "text/html").status_code == 415
    assert upload(client, b"", "image/png").status_code == 400
    big = png() + b"\x00" * (MAX_BYTES + 1)
    assert upload(client, big).status_code == 413
    assert not os.path.exists(tmp_path / "underlay.img")


def test_upload_streamed_without_length_is_capped(client):
    def chunks():
        yield png()
        for _ in range(11):
            yield b"\x00" * (1024 * 1024)
    r = client.post("/api/underlay/image", content=chunks(), headers={"Content-Type": "image/png"})
    assert r.status_code == 413


def test_layout_untouched(client):
    upload(client, png())
    client.put("/api/underlay", json={"x": 1})
    layout = client.get("/api/layout").json()
    assert "underlay" not in layout and set(layout) == {"unit", "rooms", "placements"}
    client.put("/api/layout", json={"unit": "m", "rooms": [], "placements": [], "furniture": []})
    assert client.get("/api/underlay").json()["x"] == 1  # a layout PUT never touches the photo


def test_needs_auth(client):
    upload(client, png())
    bad = "Basic " + base64.b64encode(b"aaron:nope").decode()
    for method, path in (("get", "/api/underlay"), ("get", "/api/underlay/image"), ("put", "/api/underlay"),
                         ("post", "/api/underlay/image"), ("delete", "/api/underlay")):
        assert getattr(client, method)(path, headers={"Authorization": bad}).status_code == 401


def test_admin_dependency_guards_writes(tmp_path):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from backend.underlay import router

    def deny():
        raise HTTPException(403, "admins only")
    app = FastAPI()
    app.include_router(router(str(tmp_path / "x.db"), admin=deny))
    c = TestClient(app)
    assert c.get("/api/underlay").status_code == 200
    assert c.post("/api/underlay/image", content=png(), headers={"Content-Type": "image/png"}).status_code == 403
    assert c.put("/api/underlay", json={"x": 1}).status_code == 403
    assert c.delete("/api/underlay").status_code == 403
