import math

import pytest

from backend.store import LayoutError, LayoutStore, validate_layout

KNOWN = {"light.a"}


def good():
    return {"unit": "m", "rooms": [{"id": "r1", "name": "Kitchen", "x": 0, "y": 0, "w": 4, "h": 3}],
            "placements": [{"entity_id": "light.a", "x": 1.2, "y": 0.8}]}


def test_valid():
    assert validate_layout(good(), KNOWN)["rooms"][0]["w"] == 4.0


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


def test_store_roundtrip(tmp_path):
    s = LayoutStore(str(tmp_path / "sub" / "x.db"))
    assert s.get()["rooms"] == []
    s.put(good())
    s.put(good())
    assert s.get() == good()
