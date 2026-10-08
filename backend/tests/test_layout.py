import math

import pytest

from backend.store import LayoutError, LayoutStore, validate_layout

KNOWN = {"light.a", "binary_sensor.door"}


def good():
    return {"unit": "m", "rooms": [{"id": "r1", "name": "Kitchen", "x": 0, "y": 0, "w": 4, "h": 3}],
            "placements": [{"entity_id": "light.a", "x": 1.2, "y": 0.8}]}


def fancy():
    d = good()
    d["rooms"][0]["cut"] = {"corner": "ne", "w": 2, "h": 1.5}
    d["openings"] = [{"id": "o1", "type": "door", "x": 0, "y": 1, "len": 0.9, "orient": "v",
                      "entity_id": "binary_sensor.door"},
                     {"id": "o2", "type": "window", "x": 1, "y": 3, "len": 1.2, "orient": "h"}]
    return d


def test_valid():
    out = validate_layout(good(), KNOWN)
    assert out["rooms"][0]["w"] == 4.0 and "cut" not in out["rooms"][0] and out["openings"] == []


def test_valid_cut_and_openings():
    out = validate_layout(fancy(), KNOWN)
    assert out["rooms"][0]["cut"] == {"corner": "ne", "w": 2.0, "h": 1.5}
    assert out["openings"][0]["entity_id"] == "binary_sensor.door" and "entity_id" not in out["openings"][1]
    assert validate_layout(out, KNOWN) == out


def test_old_layout_roundtrip(tmp_path):
    s = LayoutStore(str(tmp_path / "x.db"))
    s.put(good())
    assert validate_layout(s.get(), KNOWN) == {**good(), "openings": []}


@pytest.mark.parametrize("mutate", [
    lambda d: d["rooms"][0].update(name="  "),
    lambda d: d["rooms"][0].update(w=0),
    lambda d: d["rooms"][0].update(h=-1),
    lambda d: d["rooms"][0].update(x=math.inf),
    lambda d: d["rooms"][0].update(x="1"),
    lambda d: d["rooms"][0].update(x=True),
    lambda d: d["placements"][0].update(entity_id="light.nope"),
    lambda d: d["placements"][0].update(y=math.nan),
    lambda d: d["placements"].append({"entity_id": "light.a", "x": 0, "y": 0}),
    lambda d: d["rooms"].append(dict(d["rooms"][0])),
    lambda d: d.update(unit="yards"),
])
def test_invalid(mutate):
    d = good()
    mutate(d)
    with pytest.raises(LayoutError):
        validate_layout(d, KNOWN)


@pytest.mark.parametrize("mutate", [
    lambda d: d["rooms"][0]["cut"].update(corner="n"),
    lambda d: d["rooms"][0]["cut"].update(w=4),
    lambda d: d["rooms"][0]["cut"].update(h=0),
    lambda d: d["rooms"][0]["cut"].update(w=math.nan),
    lambda d: d["rooms"][0]["cut"].update(h="1"),
    lambda d: d["rooms"][0].update(cut="ne"),
    lambda d: d.update(openings={}),
    lambda d: d["openings"].append("x"),
    lambda d: d["openings"][1].update(id="o1"),
    lambda d: d["openings"][1].update(id=""),
    lambda d: d["openings"][0].update(type="arch"),
    lambda d: d["openings"][0].update(orient="d"),
    lambda d: d["openings"][0].update(len=0.1),
    lambda d: d["openings"][0].update(len=6),
    lambda d: d["openings"][0].update(x=math.inf),
    lambda d: d["openings"][0].update(y=None),
    lambda d: d["openings"][0].update(entity_id="binary_sensor.nope"),
])
def test_invalid_cut_openings(mutate):
    d = fancy()
    mutate(d)
    with pytest.raises(LayoutError):
        validate_layout(d, KNOWN)


def test_store_roundtrip(tmp_path):
    s = LayoutStore(str(tmp_path / "sub" / "x.db"))
    assert s.get()["rooms"] == []
    s.put(good())
    s.put(good())
    assert s.get() == good()
