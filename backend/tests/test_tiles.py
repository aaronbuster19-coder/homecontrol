"""Quick tiles (backend/tiles.py): pinned favourites in their own table, validated against discovered devices."""
import pytest

from backend.discovery import Device
from backend.tiles import MAX_PINS, TilesError, TileStore, validate_pins


def dev(eid, kind):
    return Device(entity_id=eid, kind=kind, name=eid, model=None)


KNOWN = {"light.a": dev("light.a", "light"), "switch.b": dev("switch.b", "plug"), "person.me": dev("person.me", "person")}


def test_validate_keeps_order_and_drops_duplicates():
    assert validate_pins(["switch.b", "light.a", "switch.b"], KNOWN, []) == ["switch.b", "light.a"]


def test_validate_rejects_unknown_and_non_controls():
    with pytest.raises(TilesError, match="unknown"):
        validate_pins(["light.nope"], KNOWN, [])
    with pytest.raises(TilesError, match="can't be a tile"):
        validate_pins(["person.me"], KNOWN, [])
    with pytest.raises(TilesError):
        validate_pins("light.a", KNOWN, [])
    with pytest.raises(TilesError):
        validate_pins([1], KNOWN, [])


def test_validate_keeps_a_stored_pin_that_is_missing_now():
    assert validate_pins(["light.gone", "light.a"], KNOWN, ["light.gone"]) == ["light.gone", "light.a"]


def test_validate_limit():
    known = {f"light.l{i}": dev(f"light.l{i}", "light") for i in range(MAX_PINS + 1)}
    assert len(validate_pins(list(known)[:MAX_PINS], known, [])) == MAX_PINS
    with pytest.raises(TilesError, match="at most"):
        validate_pins(list(known), known, [])


def test_store_round_trip_and_bad_data(tmp_path):
    s = TileStore(str(tmp_path / "t.db"))
    assert s.pins() == []
    s.save(["light.a"])
    assert TileStore(str(tmp_path / "t.db")).pins() == ["light.a"]
    with s._conn() as c:
        c.execute("UPDATE tiles SET data = 'nonsense' WHERE id = 1")
    assert s.pins() == []


def test_api_pin_order_unpin(client, fake_ha):
    assert client.get("/api/tiles").json() == {"pins": []}
    assert client.post("/api/tiles/switch.fan").json() == {"pins": ["switch.fan"]}
    assert client.post("/api/tiles/light.kitchen_1").json() == {"pins": ["switch.fan", "light.kitchen_1"]}
    assert client.post("/api/tiles/switch.fan").json() == {"pins": ["switch.fan", "light.kitchen_1"]}  # idempotent
    r = client.put("/api/tiles", json={"pins": ["light.kitchen_1", "switch.fan", "climate.lounge_valve"]})
    assert r.status_code == 200 and r.json()["pins"] == ["light.kitchen_1", "switch.fan", "climate.lounge_valve"]
    assert client.delete("/api/tiles/switch.fan").json() == {"pins": ["light.kitchen_1", "climate.lounge_valve"]}
    assert client.get("/api/tiles").json() == {"pins": ["light.kitchen_1", "climate.lounge_valve"]}
    assert fake_ha.service_calls() == []  # pinning never switches anything


def test_api_rejects_bad_bodies(client):
    assert client.post("/api/tiles/light.nope").status_code == 400
    assert client.put("/api/tiles", json=["light.kitchen_1"]).status_code == 400
    assert client.put("/api/tiles", json={"pins": "light.kitchen_1"}).status_code == 400
    assert client.put("/api/tiles", content=b"{", headers={"Content-Type": "application/json"}).status_code == 400
    assert client.get("/api/tiles").json() == {"pins": []}


def test_api_layout_untouched(client):
    before = client.get("/api/layout").json()
    client.post("/api/tiles/switch.fan")
    assert client.get("/api/layout").json() == before


def test_api_needs_sign_in(client):
    del client.headers["Authorization"]
    assert client.get("/api/tiles").status_code == 401
    assert client.post("/api/tiles/switch.fan").status_code == 401
