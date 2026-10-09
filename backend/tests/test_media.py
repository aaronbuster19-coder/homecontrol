"""TVs / media players: discovery, supported_features, control endpoint, artwork proxy, furniture links, Away and All off."""
import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import media
from backend.app import create_app
from backend.config import Settings
from backend.discovery import classify, parse_template_output
from backend.ha import HAClient
from backend.live import Live, build_device
from backend.store import LayoutError, carry_settings, stored_refs, validate_layout
from backend.tests.conftest import STATES, TEMPLATE_OUTPUT, FakeHA

TV, ST, SONOS, SPOT = "media_player.samsung_tv", "media_player.samsung_tv_smartthings", "media_player.kitchen", "media_player.spotify"
# Samsung Smart TV integration: PAUSE | VOLUME_SET | VOLUME_MUTE | PREVIOUS | NEXT | TURN_ON | TURN_OFF | VOLUME_STEP
# | SELECT_SOURCE | PLAY | SELECT_SOUND_MODE
TV_SF = 1 | 4 | 8 | 16 | 32 | 128 | 256 | 1024 | 2048 | 16384 | 65536
ST_SF = 8 | 256 | 1024 | 2048  # SmartThings' entity for the same TV can do less
SONOS_SF = 1 | 4 | 8 | 16 | 32 | 16384
# Both integrations put a media player on one HA device (merged by MAC); SmartThings adds a power switch and sensors.
MEDIA_TEMPLATE = f"""media|{TV}|Samsung TV|Samsung|QE55Q80A
dc|{TV}|tv
sf|{TV}|{TV_SF}
rel|{TV}|sensor.samsung_tv_power|power|W|measurement|Samsung TV Power
media|{ST}|Samsung TV|Samsung|QE55Q80A
dc|{ST}|tv
sf|{ST}|{ST_SF}
switch|switch.samsung_tv|Samsung TV|Samsung|QE55Q80A
binary|binary_sensor.samsung_tv_door|Samsung TV|Samsung|QE55Q80A
media|{SONOS}|Kitchen|Sonos|One
dc|{SONOS}|speaker
sf|{SONOS}|{SONOS_SF}
media|{SPOT}|||
dc|{SPOT}|
sf|{SPOT}|x
"""
PICTURE = f"/api/media_player_proxy/{TV}?token=SECRETPROXYTOKEN&cache=abc"


def tv_state(**over):
    attrs = {"device_class": "tv", "supported_features": TV_SF, "volume_level": 0.24, "is_volume_muted": False,
             "source": "TV", "source_list": ["TV", "HDMI1", "HDMI2", "Netflix", "YouTube"], "app_name": "Netflix",
             "media_title": "The Crown", "media_series_title": "Season 2", "media_content_type": "tvshow",
             "entity_picture": PICTURE, "sound_mode": "Standard", "sound_mode_list": ["Standard", "Movie", "Music"],
             "friendly_name": "Samsung TV"}
    attrs.update(over)
    return {"entity_id": TV, "state": "on", "attributes": attrs}


class MediaHA(FakeHA):
    def __init__(self):
        super().__init__()
        self.states = STATES + [tv_state(), {"entity_id": ST, "state": "on", "attributes": {"supported_features": ST_SF}},
                                {"entity_id": SONOS, "state": "playing", "attributes": {"device_class": "speaker", "supported_features": SONOS_SF}},
                                {"entity_id": SPOT, "state": "idle", "attributes": {"supported_features": 0}},
                                {"entity_id": "sensor.samsung_tv_power", "state": "92", "attributes": {"unit_of_measurement": "W"}}]
        self.image = (200, "image/jpeg", b"\xff\xd8JPEGDATA")
        self.external: list[httpx.Request] = []

    def set_tv(self, state="on", **over):
        self.states = [tv_state(**over) | {"state": state} if s["entity_id"] == TV else s for s in self.states]

    def handler(self, request):
        if request.url.host != "ha.test":  # artwork from some CDN: must never carry the HA token
            self.external.append(request)
            return httpx.Response(200, headers={"content-type": "image/png"}, content=b"PNGDATA")
        if request.url.path == "/api/template":
            return httpx.Response(200, text=TEMPLATE_OUTPUT + MEDIA_TEMPLATE)
        if request.url.path == "/api/states":
            return httpx.Response(200, json=self.states)
        if request.url.path.startswith("/api/media_player_proxy/"):
            assert request.headers["authorization"] == "Bearer test-token"
            self.calls.append((request.method, str(request.url), None))
            code, ctype, body = self.image
            return httpx.Response(code, headers={"content-type": ctype}, content=body)
        return super().handler(request)


@pytest.fixture
def mha():
    return MediaHA()


@pytest.fixture
def mclient(tmp_path, mha, monkeypatch):
    monkeypatch.setattr("backend.live.FRESH_FOR", -1)  # every request reads the fake's current states
    settings = Settings("http://ha.test", "test-token", "aaron", "s3cret", str(tmp_path / "layout.db"))
    ha = HAClient(settings.ha_url, settings.ha_token, transport=httpx.MockTransport(mha.handler))
    live = Live(ha, "ws://ha.test/api/websocket", settings.ha_token, use_ws=False)
    with TestClient(create_app(settings, ha, live, push_sender=lambda sub, p: 201)) as c:
        c.headers["Authorization"] = "Basic " + base64.b64encode(b"aaron:s3cret").decode()
        yield c


def devices(c):
    return {d["entity_id"]: d for d in c.get("/api/devices").json()}


def svc(ha):
    return [(p, b) for p, b in ha.service_calls()]


def post(c, body, eid=TV):
    return c.post(f"/api/devices/{eid}/media", json=body)


# ---------------- discovery ----------------
def test_classify_media_players():
    assert classify("media", TV, "Samsung", "QE55", "Samsung TV", "tv") == "media"
    assert classify("media", SONOS, "Sonos", "One", "Kitchen", "speaker") == "media"
    assert classify("media", SPOT, "", "", "") == "media"
    assert classify("switch", "switch.samsung_tv", "Samsung", "QE55", "Samsung TV") is None  # SmartThings power switch
    assert classify("media", "switch.not_a_player", "Samsung", "QE55", "TV") is None


def test_parse_one_media_player_per_device_and_related():
    devs = {d.entity_id: d for d in parse_template_output(TEMPLATE_OUTPUT + MEDIA_TEMPLATE)}
    media_devs = sorted(e for e, d in devs.items() if d.kind == "media")
    # The SmartThings duplicate (fewer features, same HA device) and its switch / sensor don't become devices.
    assert media_devs == [SONOS, TV, SPOT]
    assert "switch.samsung_tv" not in devs and "binary_sensor.samsung_tv_door" not in devs
    assert devs[TV].model == "Samsung QE55Q80A" and devs[TV].name == "Samsung TV"
    assert devs[TV].related == {"power": "sensor.samsung_tv_power"}
    assert devs[SPOT].name == SPOT and devs[SPOT].model == ""
    # everything else exactly as before
    base = {d.entity_id: d.kind for d in parse_template_output(TEMPLATE_OUTPUT)}
    assert {e: k for e, k in ((e, d.kind) for e, d in devs.items()) if k != "media"} == base


def test_parse_duplicate_with_more_features_wins():
    swapped = MEDIA_TEMPLATE.replace(f"sf|{TV}|{TV_SF}", f"sf|{TV}|8").replace(f"sf|{ST}|{ST_SF}", f"sf|{ST}|{TV_SF}")
    media_devs = sorted(d.entity_id for d in parse_template_output(swapped) if d.kind == "media")
    assert media_devs == [SONOS, ST, SPOT]


def test_media_type_tv_vs_speaker():
    assert media.media_type({"device_class": "tv"}) == "tv"
    assert media.media_type({"device_class": "speaker"}, "Samsung QE55") == "speaker"   # HA's class wins
    assert media.media_type({"device_class": "receiver"}) == "speaker"
    assert media.media_type({}, "Samsung Electronics QE55Q80A", "Living room") == "tv"
    assert media.media_type({}, "LG webOS TV OLED55C1", "") == "tv"
    assert media.media_type({}, "Sonos One", "Kitchen") == "speaker"
    assert media.media_type({}, "Google Chromecast Audio", "") == "speaker"
    assert media.media_type({}, "Google Chromecast", "") == "tv"
    assert media.media_type({}, "", "Spotify", "media_player.spotify") == "other"


def test_decode_supported_features():
    assert media.decode(0) == {k: False for k in media.CAPS}
    assert media.decode(TV_SF) == {k: True for k in media.CAPS}
    caps = media.decode(ST_SF)
    assert caps == {"turn_on": False, "turn_off": True, "volume_set": False, "volume_step": True, "mute": True,
                    "select_source": True, "play_pause": False, "next": False, "previous": False, "sound_mode": False}
    assert media.decode(media.PLAY)["play_pause"] and media.decode(media.PAUSE)["play_pause"]
    assert media.decode(media.VOLUME_SET)["volume_step"]  # volume_up/down work with VOLUME_SET alone
    assert media.features({"supported_features": True}) == 0 and media.features({"supported_features": "7"}) == 0


def test_device_item_fields(mclient):
    d = devices(mclient)[TV]
    assert d["kind"] == "media" and d["media_type"] == "tv" and d["state"] == "on"
    assert d["volume_level"] == 0.24 and d["is_volume_muted"] is False and d["source"] == "TV"
    assert d["source_list"] == ["TV", "HDMI1", "HDMI2", "Netflix", "YouTube"]
    assert d["app_name"] == "Netflix" and d["media_title"] == "The Crown" and d["media_series_title"] == "Season 2"
    assert d["sound_mode"] == "Standard" and d["sound_mode_list"] == ["Standard", "Movie", "Music"]
    assert d["supports"]["turn_on"] and d["power"] == 92.0
    assert d["picture"] == f"/api/media/{TV}/artwork?v={media.picture_version(PICTURE)}"
    body = json.dumps(devices(mclient))
    assert "SECRETPROXYTOKEN" not in body and "media_player_proxy" not in body and "entity_picture" not in body
    sonos = devices(mclient)[SONOS]
    assert sonos["media_type"] == "speaker" and sonos["picture"] is None and not sonos["supports"]["turn_on"]


def test_picture_only_while_on_and_cache_busted(mha, mclient):
    v1 = devices(mclient)[TV]["picture"]
    mha.set_tv(entity_picture=PICTURE.replace("abc", "def"))
    v2 = devices(mclient)[TV]["picture"]
    assert v1 != v2 and v2.endswith(media.picture_version(PICTURE.replace("abc", "def")))
    mha.set_tv(state="off")
    assert devices(mclient)[TV]["picture"] is None


# ---------------- control ----------------
@pytest.mark.parametrize("body, service, data", [
    ({"action": "power", "on": False}, "turn_off", {}),
    ({"action": "power", "on": True}, "turn_on", {}),
    ({"action": "volume", "level": 0.35}, "volume_set", {"volume_level": 0.35}),
    ({"action": "volume", "level": 0}, "volume_set", {"volume_level": 0.0}),
    ({"action": "volume", "step": "up"}, "volume_up", {}),
    ({"action": "volume", "step": "down"}, "volume_down", {}),
    ({"action": "mute", "muted": True}, "volume_mute", {"is_volume_muted": True}),
    ({"action": "source", "source": "HDMI1"}, "select_source", {"source": "HDMI1"}),
    ({"action": "play_pause"}, "media_play_pause", {}),
    ({"action": "next"}, "media_next_track", {}),
    ({"action": "previous"}, "media_previous_track", {}),
    ({"action": "sound_mode", "sound_mode": "Movie"}, "select_sound_mode", {"sound_mode": "Movie"}),
])
def test_media_service_calls(mclient, mha, body, service, data):
    r = post(mclient, body)
    assert r.status_code == 200, r.text
    assert svc(mha) == [(f"/api/services/media_player/{service}", {"entity_id": TV, **data})]


@pytest.mark.parametrize("body", [
    {}, [], {"action": "explode"}, {"action": "power"}, {"action": "power", "on": "yes"}, {"action": "power", "on": 1},
    {"action": "power", "on": True, "extra": 1}, {"action": "volume"}, {"action": "volume", "level": 1.01},
    {"action": "volume", "level": -0.1}, {"action": "volume", "level": "0.5"}, {"action": "volume", "level": True},
    {"action": "volume", "step": "sideways"}, {"action": "volume", "level": 0.5, "step": "up"},
    {"action": "mute"}, {"action": "mute", "muted": "true"}, {"action": "source", "source": "HDMI9"},
    {"action": "source", "source": 1}, {"action": "sound_mode", "sound_mode": "Disco"}, {"action": "next", "x": 1},
])
def test_media_invalid(mclient, mha, body):
    r = post(mclient, body)
    assert r.status_code == 400, (body, r.text)
    assert svc(mha) == []


def test_media_invalid_json(mclient, mha):
    r = mclient.post(f"/api/devices/{TV}/media", content="{nope", headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and svc(mha) == []


def test_unsupported_features_rejected(mclient, mha):
    mha.set_tv(state="off", supported_features=TV_SF & ~media.TURN_ON)  # no Wake-on-LAN / turn-on configured
    r = post(mclient, {"action": "power", "on": True})
    assert r.status_code == 400 and "turned on" in r.json()["detail"]
    for body in ({"action": "volume", "level": 0.5}, {"action": "play_pause"}, {"action": "next"},
                 {"action": "sound_mode", "sound_mode": "Movie"}, {"action": "source", "source": "TV"}):
        assert post(mclient, body, ST).status_code == 404, body  # the SmartThings duplicate isn't a device at all
    # Spotify: no features at all
    for body in ({"action": "power", "on": False}, {"action": "mute", "muted": True}, {"action": "volume", "step": "up"},
                 {"action": "play_pause"}, {"action": "previous"}):
        assert post(mclient, body, SPOT).status_code == 400, body
    assert svc(mha) == []


def test_unavailable_tv_only_power(mclient, mha):
    mha.set_tv(state="unavailable")
    assert post(mclient, {"action": "volume", "step": "up"}).status_code == 400
    assert post(mclient, {"action": "power", "on": True}).status_code == 200
    assert svc(mha) == [("/api/services/media_player/turn_on", {"entity_id": TV})]


def test_media_endpoint_kinds(mclient, mha):
    assert post(mclient, {"action": "play_pause"}, "media_player.nope").status_code == 404
    assert post(mclient, {"action": "play_pause"}, "light.kitchen_1").status_code == 400
    assert mclient.post(f"/api/devices/{TV}/toggle").status_code == 400  # never a blind toggle
    assert svc(mha) == []


# ---------------- artwork proxy ----------------
def test_artwork_proxy(mclient, mha):
    url = devices(mclient)[TV]["picture"]
    r = mclient.get(url)
    assert r.status_code == 200 and r.content == b"\xff\xd8JPEGDATA" and r.headers["content-type"] == "image/jpeg"
    assert "max-age=86400" in r.headers["cache-control"] and r.headers["x-content-type-options"] == "nosniff"
    assert "test-token" not in r.text and "test-token" not in json.dumps(dict(r.headers))
    assert [c[1] for c in mha.calls if "media_player_proxy" in c[1]] == [f"http://ha.test{PICTURE}"]
    stale = mclient.get(f"/api/media/{TV}/artwork?v=old")  # an old version: served, but not cached for long
    assert stale.status_code == 200 and "no-cache" in stale.headers["cache-control"]


def test_artwork_needs_login(mclient):
    url = devices(mclient)[TV]["picture"]
    anon = mclient.get(url, headers={"Authorization": ""}, follow_redirects=False)
    assert anon.status_code in (401, 303, 307) and b"JPEG" not in anon.content


def test_artwork_absolute_ha_url_and_external(mclient, mha):
    mha.set_tv(entity_picture=f"http://ha.test{PICTURE}")
    assert mclient.get(f"/api/media/{TV}/artwork").content == b"\xff\xd8JPEGDATA"
    mha.set_tv(entity_picture="https://cdn.example/art/1.png")
    r = mclient.get(f"/api/media/{TV}/artwork")
    assert r.status_code == 200 and r.content == b"PNGDATA"
    assert [str(q.url) for q in mha.external] == ["https://cdn.example/art/1.png"]
    assert "authorization" not in {k.lower() for k in mha.external[0].headers}  # the HA token stays with HA


@pytest.mark.parametrize("pic, image", [
    (None, None), ("javascript:alert(1)", None), ("//evil.example/x.png", None),
    (PICTURE, (200, "image/svg+xml", b"<svg onload=alert(1)>")), (PICTURE, (200, "text/html", b"<html>")),
    (PICTURE, (200, "image/jpeg", b"x" * (media.ARTWORK_MAX + 1))),
])
def test_artwork_rejected(mclient, mha, pic, image):
    mha.set_tv(entity_picture=pic)
    if image:
        mha.image = image
    assert mclient.get(f"/api/media/{TV}/artwork").status_code == 404


def test_artwork_ha_error_is_502_without_details(mclient, mha):
    mha.image = (500, "text/plain", b"boom")
    r = mclient.get(f"/api/media/{TV}/artwork")
    assert r.status_code == 502 and "SECRETPROXYTOKEN" not in r.text


def test_artwork_only_for_media(mclient):
    assert mclient.get("/api/media/light.kitchen_1/artwork").status_code == 400
    assert mclient.get("/api/media/media_player.nope/artwork").status_code == 404


# ---------------- furniture links ----------------
def tv_piece(**kw):
    return {"id": "f1", "type": "tv", "x": 1, "y": 1, "w": 1.25, "h": 0.25, "rot": 0, **kw}


def test_furniture_media_validation():
    known = {TV, SONOS, "switch.fan"}
    lay = validate_layout({"furniture": [tv_piece(media=TV, plug="switch.fan")]}, known, {"switch.fan"}, set(), {TV, SONOS})
    assert lay["furniture"][0]["media"] == TV and lay["furniture"][0]["plug"] == "switch.fan"
    assert "media" not in validate_layout({"furniture": [tv_piece(media=None)]}, known, set(), set(), {TV})["furniture"][0]
    bad = [
        [tv_piece(media="media_player.gone")],
        [tv_piece(media="switch.fan")],
        [tv_piece(media=7)],
        [{**tv_piece(media=TV), "type": "sofa", "w": 2, "h": 0.9}],
        [tv_piece(media=TV), tv_piece(id="f2", media=TV)],
    ]
    for furniture in bad:
        with pytest.raises(LayoutError):
            validate_layout({"furniture": furniture}, known, set(), set(), {TV, SONOS})


def test_furniture_media_carried_and_unlinked():
    old = {"unit": "m", "rooms": [], "placements": [], "furniture": [tv_piece(media=TV)]}
    raw = {"furniture": [tv_piece()]}  # an older app: no "media" key at all
    new = carry_settings(validate_layout(raw, {TV}, set(), set(), {TV}), old, raw)
    assert new["furniture"][0]["media"] == TV
    raw = {"furniture": [tv_piece(media=None)]}  # unlinking says so
    assert "media" not in carry_settings(validate_layout(raw, {TV}, set(), set(), {TV}), old, raw)["furniture"][0]
    raw = {"furniture": [{**tv_piece(), "type": "unit"}]}  # became something else: link dropped
    assert "media" not in carry_settings(validate_layout(raw, {TV}, set(), set(), {TV}), old, raw)["furniture"][0]
    assert TV in stored_refs(old)


def test_layout_put_media_link(mclient):
    lay = {"unit": "m", "rooms": [], "placements": [], "furniture": [tv_piece(media=TV)]}
    assert mclient.put("/api/layout", json=lay).json()["furniture"][0]["media"] == TV
    # a PUT without the key keeps it; with another type's link it's refused
    assert mclient.put("/api/layout", json={**lay, "furniture": [tv_piece()]}).json()["furniture"][0]["media"] == TV
    assert mclient.put("/api/layout", json={**lay, "furniture": [tv_piece(media="light.kitchen_1")]}).status_code == 400
    assert mclient.put("/api/layout", json={**lay, "furniture": [tv_piece(media=None)]}).json()["furniture"][0].get("media") is None


# ---------------- All off / Away ----------------
def test_all_off_include_tv(mclient, mha):
    r = mclient.put("/api/layout", json={"unit": "m", "rooms": [], "placements": [], "settings": {"keep_on": [], "all_off_include": [TV]}})
    assert r.status_code == 200 and r.json()["settings"]["all_off_include"] == [TV]
    assert mclient.post("/api/bulk", json={"action": "turn_off", "entity_ids": ["light.kitchen_1", TV]}).status_code == 200
    assert ("/api/services/media_player/turn_off", {"entity_id": [TV]}) in svc(mha)
    n = len(svc(mha))
    assert mclient.post("/api/bulk", json={"action": "turn_on", "entity_ids": [TV]}).status_code == 400
    assert len(svc(mha)) == n


def test_away_turns_tvs_off(mclient, mha):
    r = mclient.post("/api/mode", json={"mode": "away"}).json()
    assert ("/api/services/media_player/turn_off", {"entity_id": [TV]}) in svc(mha)  # the speaker can't turn off
    assert TV in r["turned_off"] and r["tv_off"] is True


def test_away_leaves_off_tvs_and_respects_setting(mclient, mha):
    mha.set_tv(state="standby")
    mclient.post("/api/mode", json={"mode": "away"})
    assert not [c for c in svc(mha) if "media_player" in c[0]]
    mclient.post("/api/mode", json={"mode": "home"})
    mha.set_tv(state="on")
    assert mclient.put("/api/mode/settings", json={"tv_off": False}).json()["tv_off"] is False
    assert mclient.get("/api/mode").json()["tv_off"] is False
    assert mclient.get("/api/mode").json()["away_temp"] == 16.0  # untouched
    mclient.post("/api/mode", json={"mode": "away"})
    assert not [c for c in svc(mha) if "media_player" in c[0]]


@pytest.mark.parametrize("body", [{"tv_off": "no"}, {"tv_off": 0}, {"tv_off": True, "party": 1}])
def test_mode_settings_tv_off_invalid(mclient, body):
    assert mclient.put("/api/mode/settings", json=body).status_code == 400


def test_build_device_without_states():
    dev = next(d for d in parse_template_output(MEDIA_TEMPLATE) if d.entity_id == TV)
    item = build_device(dev, {})
    assert item["state"] == "unavailable" and item["volume_level"] is None and item["source_list"] == [] and item["picture"] is None
