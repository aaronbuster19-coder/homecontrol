import copy
import math
import re
from pathlib import Path

import pytest

from backend.store import FURNITURE_TYPES, LayoutError, LayoutStore, validate_layout

KNOWN = {"light.a"}


def layout(*furniture):
    return {"unit": "m", "rooms": [{"id": "r1", "name": "Bedroom", "x": 0, "y": 0, "w": 4, "h": 3}],
            "placements": [{"entity_id": "light.a", "x": 1, "y": 1}], "furniture": list(furniture)}


def bed(**kw):
    return {"id": "f1", "type": "bed", "x": 1.0, "y": 1.2, "w": 1.4, "h": 2.0, "rot": 90, **kw}


def test_valid_furniture():
    out = validate_layout(layout(bed(label="Our bed"), {"id": "f2", "type": "desk", "x": 3, "y": 0.3, "w": 1.2, "h": 0.6}),
                          KNOWN)
    assert out["furniture"] == [
        {"id": "f1", "type": "bed", "x": 1.0, "y": 1.2, "w": 1.4, "h": 2.0, "rot": 90, "label": "Our bed"},
        {"id": "f2", "type": "desk", "x": 3.0, "y": 0.3, "w": 1.2, "h": 0.6, "rot": 0}]
    assert validate_layout(out, KNOWN) == out


@pytest.mark.parametrize("rot, want", [(0, 0), (90, 90), (359.6, 0), (360, 0), (-90, 270), (450, 90), (14.6, 15), (721, 1)])
def test_rotation_normalised(rot, want):
    assert validate_layout(layout(bed(rot=rot)), KNOWN)["furniture"][0]["rot"] == want


def test_label_tidied_and_empty_dropped():
    assert validate_layout(layout(bed(label="  big   bed ")), KNOWN)["furniture"][0]["label"] == "big bed"
    assert "label" not in validate_layout(layout(bed(label="   ")), KNOWN)["furniture"][0]
    assert "label" not in validate_layout(layout(bed(label=None)), KNOWN)["furniture"][0]
    assert validate_layout(layout(bed(label="x" * 30)), KNOWN)["furniture"][0]["label"] == "x" * 30


def test_size_limits():
    assert validate_layout(layout(bed(w=0.1, h=10)), KNOWN)["furniture"][0]["w"] == 0.1
    for w in (0.09, 10.01, 0, -1):
        with pytest.raises(LayoutError):
            validate_layout(layout(bed(w=w)), KNOWN)


def test_at_most_200():
    many = [bed(id=f"f{i}") for i in range(200)]
    assert len(validate_layout(layout(*many), KNOWN)["furniture"]) == 200
    with pytest.raises(LayoutError, match="200"):
        validate_layout(layout(*many, bed(id="one-more")), KNOWN)


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(furniture={}),
    lambda d: d["furniture"].append("x"),
    lambda d: d["furniture"].append(dict(d["furniture"][0])),  # duplicate id
    lambda d: d["furniture"][0].update(id=""),
    lambda d: d["furniture"][0].update(id=7),
    lambda d: d["furniture"][0].update(id="x" * 41),
    lambda d: d["furniture"][0].update(type="spaceship"),
    lambda d: d["furniture"][0].pop("type"),
    lambda d: d["furniture"][0].update(x=math.inf),
    lambda d: d["furniture"][0].update(y=math.nan),
    lambda d: d["furniture"][0].update(x="1"),
    lambda d: d["furniture"][0].update(w=True),
    lambda d: d["furniture"][0].pop("h"),
    lambda d: d["furniture"][0].update(rot=math.inf),
    lambda d: d["furniture"][0].update(rot="90"),
    lambda d: d["furniture"][0].update(label=5),
    lambda d: d["furniture"][0].update(label="x" * 31),
])
def test_invalid_furniture(mutate):
    d = layout(bed())
    mutate(d)
    with pytest.raises(LayoutError):
        validate_layout(d, KNOWN)


def test_old_layouts_unchanged(tmp_path):
    """A layout from before furniture loads, validates and saves exactly as before: no furniture key appears."""
    old = layout()
    del old["furniture"]
    s = LayoutStore(str(tmp_path / "x.db"))
    s.put(old)
    out = validate_layout(s.get(), KNOWN)
    assert "furniture" not in out and out == {**old, "openings": []}
    assert "furniture" not in validate_layout({**old, "furniture": None}, KNOWN)
    assert validate_layout({**old, "furniture": []}, KNOWN)["furniture"] == []


def test_types_match_the_frontend_catalogue():
    js = (Path(__file__).resolve().parents[2] / "frontend" / "furniture.js").read_text()
    block = js[js.index("const FURNITURE = {"):]
    block = block[:block.index("\n};")]
    assert re.findall(r"^  (\w+): \{ name:", block, re.M) == list(FURNITURE_TYPES)


# ---------- API: save, export, import ----------
def api_layout(*furniture):
    return {"unit": "m", "rooms": [{"id": "r1", "name": "Bedroom", "x": 0, "y": 0, "w": 4, "h": 3}],
            "placements": [{"entity_id": "light.kitchen_1", "x": 1, "y": 1}], "furniture": list(furniture)}


def test_api_roundtrip_and_rejects(client):
    r = client.put("/api/layout", json=api_layout(bed(label="Ours")))
    assert r.status_code == 200, r.text
    got = client.get("/api/layout").json()
    assert got["furniture"] == [{**bed(), "label": "Ours"}]
    assert client.put("/api/layout", json=api_layout(bed(type="hammock"))).status_code == 400
    assert client.get("/api/layout").json()["furniture"] == got["furniture"]  # a rejected save changes nothing


def test_api_export_import_carries_furniture(client):
    """Export is GET /api/layout; import PUTs that file back. Furniture survives both, settings ride along."""
    assert client.put("/api/layout", json={**api_layout(bed(), {**bed(id="f2", type="sofa", w=2, h=0.9, rot=180)}),
                                           "settings": {"keep_on": []}}).status_code == 200
    exported = client.get("/api/layout").json()
    assert [f["id"] for f in exported["furniture"]] == ["f1", "f2"]
    assert client.put("/api/layout", json={"unit": "m", "rooms": [], "placements": [], "furniture": []}).status_code == 200
    assert not client.get("/api/layout").json().get("furniture")  # cleared explicitly (leaving the key out keeps it)
    r = client.put("/api/layout", json=copy.deepcopy(exported))
    assert r.status_code == 200, r.text
    assert client.get("/api/layout").json()["furniture"] == exported["furniture"]


def test_put_without_furniture_key_keeps_stored_furniture(client):
    # An older cached app (from before furniture) saves layouts without the key; it must not wipe the furniture.
    base = {"unit": "m", "rooms": [{"id": "r1", "name": "Bedroom", "x": 0, "y": 0, "w": 4, "h": 3}], "placements": []}
    bed = {"id": "f1", "type": "bed", "x": 2, "y": 1.5, "w": 1.4, "h": 2.0, "rot": 0}
    assert client.put("/api/layout", json={**base, "furniture": [bed]}).status_code == 200
    r = client.put("/api/layout", json=base)
    assert r.status_code == 200 and [f["id"] for f in r.json()["furniture"]] == ["f1"]
    r = client.put("/api/layout", json={**base, "furniture": []})
    assert r.status_code == 200 and not r.json().get("furniture")
