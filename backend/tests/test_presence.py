"""Auto Away (backend/presence.py): discovery of people, the state machine, and the real Away / Home via the API."""
import asyncio
import base64
import copy
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.automations import AutoStore
from backend.config import Settings
from backend.discovery import Device, parse_template_output
from backend.ha import DISCOVERY_TEMPLATE, HAClient
from backend.live import Live
from backend.presence import ARRIVE_CONFIRM, MANUAL_WINS, Presence, PresenceError, is_home, validate_settings

from .conftest import STATES, TEMPLATE_OUTPUT, FakeHA

LON = ZoneInfo("Europe/London")
A, B = "person.alex", "person.sam"


def ts(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=LON).timestamp()


def run(coro):
    return asyncio.run(coro)


# ---------------- discovery ----------------
def test_template_lists_people_and_trackers():
    assert "states.person" in DISCOVERY_TEMPLATE and "states.device_tracker" in DISCOVERY_TEMPLATE
    assert "source_type" in DISCOVERY_TEMPLATE


def test_people_discovered_trackers_only_as_fallback():
    out = TEMPLATE_OUTPUT + ("person|person.alex|Alex||\n"
                             "tracker|device_tracker.alex_phone|Alex's phone|gps|\n"
                             "tracker|device_tracker.router_tv|TV|router|\n")
    devs = {d.entity_id: d for d in parse_template_output(out)}
    assert devs[A].kind == "person" and devs[A].name == "Alex" and devs[A].model == "Person"
    assert "device_tracker.alex_phone" not in devs and "device_tracker.router_tv" not in devs
    assert devs["light.kitchen_1"].kind == "light"  # everything else as before
    # no person entities: GPS / router trackers count instead; bluetooth etc. never get here (template filters)
    out = TEMPLATE_OUTPUT + "tracker|device_tracker.alex_phone|Alex's phone|gps|\ntracker|device_tracker.bt|BT|bluetooth|\n"
    devs = {d.entity_id: d for d in parse_template_output(out)}
    assert devs["device_tracker.alex_phone"].kind == "person" and devs["device_tracker.alex_phone"].model == "Phone (GPS)"
    assert "device_tracker.bt" not in devs
    # nothing at all: no people
    assert not [d for d in parse_template_output(TEMPLATE_OUTPUT) if d.kind == "person"]


def test_is_home_and_settings_validation():
    assert is_home({"state": "home"}) is True
    assert is_home({"state": "not_home"}) is False and is_home({"state": "Work"}) is False
    assert is_home({"state": "unavailable"}) is None and is_home({"state": "unknown"}) is None and is_home(None) is None
    from backend.presence import DEFAULT_SETTINGS as D
    assert D["enabled"] is False and D["away_minutes"] == 10 and D["come_home"] is True and D["only_between"] is False
    v = validate_settings({"enabled": True, "away_minutes": 2, "people": [B, A, A], "from": "07:30", "to": "22:00"}, D)
    assert v["people"] == [A, B] and v["away_minutes"] == 2 and v["from"] == "07:30"
    assert validate_settings({"people": None}, v)["people"] is None
    for bad in ({"away_minutes": 1}, {"away_minutes": 121}, {"away_minutes": 5.5}, {"away_minutes": True},
                {"enabled": "yes"}, {"from": "7:30"}, {"to": "24:00"}, {"people": ["light.x"]}, {"people": "person.a"}, []):
        with pytest.raises(PresenceError):
            validate_settings(bad, D)


# ---------------- state machine ----------------
class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Rig:
    """Presence with fake go_away / go_home (they flip the mode and count calls) and recorded pushes."""

    def __init__(self, tmp_path, t=None, enabled=True, **settings):
        self.store = AutoStore(str(tmp_path / "db.sqlite"))
        self.clock = Clock(t or ts(2026, 10, 9, 12))
        self.mode = {"mode": "home"}
        self.calls, self.pushes, self.fail = [], [], False
        self.devices = {A: Device(A, "person", "Alex", "Person"), B: Device(B, "person", "Sam", "Person"),
                        "light.x": Device("light.x", "light", "X", "L530")}
        self.states = {A: {"state": "home"}, B: {"state": "home"}}
        self.p = self.make()
        if enabled:
            self.p.put_settings(validate_settings({"enabled": True, **settings}, self.p.settings()))

    def make(self):
        p = Presence(self.store, self._push, lambda: self.mode, self.clock, LON)

        async def go_away():
            self.calls.append("away")
            if self.fail:
                raise RuntimeError("HA down")
            self.mode = {"mode": "away"}
            return {"turned_off": ["light.x"], "valves": {"climate.v": 16.0}, "away_temp": 16.0}

        async def go_home():
            self.calls.append("home")
            if self.fail:
                raise RuntimeError("HA down")
            self.mode = {"mode": "home"}
            return {"valves": {}}
        p.actions = (go_away, go_home)
        return p

    async def _push(self, payload):
        self.pushes.append(payload)

    def set(self, **people):
        for k, v in people.items():
            self.states[{"alex": A, "sam": B}[k]] = {"state": v}

    def tick(self, advance=0):
        self.clock.t += advance
        return run(self.p.tick(self.devices, self.states))


def test_everyone_leaves_then_away_after_n_minutes(tmp_path):
    r = Rig(tmp_path)
    assert r.tick() is None
    r.set(alex="not_home")
    assert r.tick(60) is None  # Sam is still home
    r.set(sam="Work")          # a zone name counts as out
    assert r.tick(60) is None
    out_at = r.clock.t
    assert r.p.status(r.devices, r.states)["away_at"] == int((out_at + 600) * 1000)
    assert r.p.next_due() == out_at + 600  # the loop wakes up right on time
    assert r.tick(599) is None and r.calls == []
    assert r.tick(1) == "away" and r.calls == ["away"]
    assert r.pushes == [{"title": "Switched to Away — everyone left", "body": "1 light and plugs off, radiators 16°.",
                         "tag": "presence", "url": "/"}]
    for _ in range(5):  # stays Away: never a second call
        assert r.tick(600) is None
    assert r.calls == ["away"]
    s = r.p.status(r.devices, r.states)
    assert s["everyone_out"] and s["away_at"] is None and s["last"]["type"] == "away" and s["last"]["status"] == "done"
    assert s["log"][0]["text"].startswith("Switched to Away — everyone out since 12:")


def test_arrival_cancels_pending_away(tmp_path):
    r = Rig(tmp_path)
    r.set(alex="not_home", sam="not_home")
    r.tick()
    r.set(sam="home")
    assert r.tick(300) is None
    r.set(sam="not_home")  # left again: the N minutes start over
    r.tick(60)
    assert r.tick(599) is None and r.calls == []
    assert r.tick(1) == "away"
    assert any("cancelled" in x["text"] for x in r.p.status(r.devices, r.states)["log"])


def test_arrival_returns_home_after_confirm(tmp_path):
    r = Rig(tmp_path)
    r.set(alex="not_home", sam="not_home")
    r.tick()
    assert r.tick(600) == "away"
    r.set(alex="home")
    assert r.tick(15) is None  # seen, not yet held
    assert r.p.status(r.devices, r.states)["arrival_at"] is not None
    assert r.tick(ARRIVE_CONFIRM - 1) is None
    assert r.tick(1) == "home" and r.calls == ["away", "home"]
    assert r.pushes[-1]["title"] == "Welcome home" and "Alex arrived" in r.pushes[-1]["body"]
    assert r.tick(60) is None and r.calls == ["away", "home"]


def test_gps_blip_home_does_not_flap(tmp_path):
    r = Rig(tmp_path)
    r.set(alex="not_home", sam="not_home")
    r.tick()
    assert r.tick(600) == "away"
    r.set(alex="home")
    r.tick(10)
    r.set(alex="not_home")  # one blip, gone before it held
    for _ in range(4):
        assert r.tick(30) is None
    assert r.calls == ["away"] and r.mode["mode"] == "away"


def test_come_home_off(tmp_path):
    r = Rig(tmp_path, come_home=False)
    r.set(alex="not_home", sam="not_home")
    r.tick()
    r.tick(600)
    r.set(alex="home")
    r.tick(10)
    assert r.tick(120) is None and r.calls == ["away"]


def test_arriving_when_already_home_does_nothing(tmp_path):
    r = Rig(tmp_path)
    r.set(alex="not_home")
    r.tick()
    r.set(alex="home")
    r.tick(10)
    assert r.tick(120) is None and r.calls == []


def test_manual_override_window(tmp_path):
    r = Rig(tmp_path)
    r.p.manual("home")  # pressed Home by hand at 12:00
    r.set(alex="not_home", sam="not_home")
    r.tick(60)
    assert r.tick(600) is None and r.calls == []  # due, but set by hand 11 min ago
    s = r.p.status(r.devices, r.states)
    assert s["blocked"] == "manual" and s["manual_until"] == int((ts(2026, 10, 9, 12) + MANUAL_WINS) * 1000)
    r.clock.t = ts(2026, 10, 9, 12) + MANUAL_WINS - 1
    assert r.tick() is None
    assert r.tick(1) == "away"


def test_manual_change_during_a_stretch_wins(tmp_path):
    r = Rig(tmp_path)
    r.set(alex="not_home", sam="not_home")
    r.tick()
    assert r.tick(600) == "away"
    r.p.manual("home")  # phones say out, the user says home
    r.mode = {"mode": "home"}
    for _ in range(10):
        assert r.tick(600) is None  # never Away again in this stretch, not even after 30 min
    r.set(alex="home")
    r.tick(60)
    r.set(alex="not_home")  # a new stretch counts again
    r.tick(60)
    assert r.tick(600) == "away"


def test_unavailable_never_triggers(tmp_path):
    r = Rig(tmp_path)
    r.set(alex="unavailable", sam="unknown")
    for _ in range(5):
        assert r.tick(600) is None  # unknown from the start: never "out"
    r.set(alex="not_home", sam="not_home")
    r.tick(1)
    r.set(alex="unavailable")  # Alex was out: still counts as out (unchanged)
    assert r.tick(600) == "away"
    r.set(alex="unavailable", sam="unavailable")
    for _ in range(3):
        assert r.tick(120) is None  # Away stays; unavailable is no arrival
    assert r.calls == ["away"]


def test_only_selected_people(tmp_path):
    r = Rig(tmp_path, people=[A])
    r.set(alex="not_home")  # Sam is home but isn't counted
    r.tick()
    assert r.tick(600) == "away"
    r2 = Rig(tmp_path / "x", people=[])
    r2.set(alex="not_home", sam="not_home")
    r2.tick()
    assert r2.tick(3600) is None  # nobody chosen: never


def test_time_window(tmp_path):
    t = ts(2026, 10, 9, 22, 50)
    r = Rig(tmp_path, t=t, only_between=True, **{"from": "08:00", "to": "23:00"})
    r.set(alex="not_home", sam="not_home")
    r.tick()
    assert r.tick(600) is None and r.calls == []  # due 23:00: outside the hours
    assert r.p.status(r.devices, r.states)["blocked"] == "hours"
    r.clock.t = ts(2026, 10, 10, 7, 59)
    assert r.tick() is None
    r.clock.t = ts(2026, 10, 10, 8, 0)
    assert r.tick() == "away"  # still everyone out when the hours start
    r.clock.t = ts(2026, 10, 10, 23, 30)
    r.set(alex="home")
    r.tick()
    assert r.tick(ARRIVE_CONFIRM) is None and r.calls == ["away"]  # arrival outside the hours: stays Away


def test_time_window_across_dst(tmp_path):
    # 25 Oct 2026: clocks go back at 02:00 BST. Window 00:30–01:30 local; wall clock decides, both 01:xx count.
    r = Rig(tmp_path, t=ts(2026, 10, 25, 0, 0), only_between=True, away_minutes=2, **{"from": "00:30", "to": "01:30"})
    r.set(alex="not_home", sam="not_home")
    r.tick()
    assert r.tick(200) is None  # 00:03, before the window
    r.clock.t = datetime(2026, 10, 25, 1, 10, tzinfo=LON, fold=1).timestamp()  # 01:10 GMT, the second one
    assert r.tick() == "away"


def test_failure_backs_off_without_replay(tmp_path):
    r = Rig(tmp_path)
    r.fail = True
    r.set(alex="not_home", sam="not_home")
    r.tick()
    assert r.tick(600) is None and r.calls == ["away"]
    for _ in range(4):
        assert r.tick(60) is None  # within the 5 min back-off
    assert r.calls == ["away"]
    assert r.tick(60) is None and r.calls == ["away", "away"]  # 5 min later: one more try, fails again -> 10 min
    assert r.tick(300) is None and r.calls == ["away", "away"]
    r.fail = False
    assert r.tick(300) == "away" and r.calls == ["away", "away", "away"]
    assert any("failed" in x["text"] for x in r.p.status(r.devices, r.states)["log"])


def test_restart_keeps_state_and_never_replays(tmp_path):
    r = Rig(tmp_path)
    r.set(alex="not_home", sam="not_home")
    r.tick()
    r.tick(300)
    r.p = r.make()  # restart half-way: the stretch keeps its start
    assert r.tick(299) is None
    assert r.tick(1) == "away"
    r.p = r.make()  # restart after: nothing again
    for _ in range(3):
        assert r.tick(600) is None
    assert r.calls == ["away"]
    # crash in the middle of the action: written before it ran, so a restart doesn't run it again
    r2 = Rig(tmp_path / "crash")
    r2.set(alex="not_home", sam="not_home")
    r2.tick()

    async def crash():
        r2.calls.append("away")
        raise KeyboardInterrupt  # stands in for the process dying mid-call
    r2.p.actions = (crash, r2.p.actions[1])
    with pytest.raises(KeyboardInterrupt):
        r2.tick(600)
    r2.p = r2.make()
    for _ in range(3):
        assert r2.tick(600) is None
    assert r2.calls == ["away"]


def test_disabled_does_nothing_and_enabling_starts_the_clock(tmp_path):
    r = Rig(tmp_path, enabled=False)
    r.set(alex="not_home", sam="not_home")
    r.tick()
    assert r.tick(3600) is None and r.calls == []
    r.p.put_settings(validate_settings({"enabled": True}, r.p.settings()))
    assert r.tick(599) is None  # N minutes from switching on, not from when they left
    assert r.tick(1) == "away"


def test_notify_off(tmp_path):
    r = Rig(tmp_path, notify=False)
    r.set(alex="not_home", sam="not_home")
    r.tick()
    assert r.tick(600) == "away" and r.pushes == []


# ---------------- through the real app: go_away / go_home as the ⋯ menu does them ----------------
PEOPLE = "person|person.alex|Alex||\nperson|person.sam|Sam||\n"


class PeopleHA(FakeHA):
    def __init__(self):
        super().__init__()
        self.states = copy.deepcopy(STATES) + [{"entity_id": A, "state": "home", "attributes": {}},
                                              {"entity_id": B, "state": "home", "attributes": {}}]

    def handler(self, request):
        if request.url.path == "/api/template":
            return httpx.Response(200, text=TEMPLATE_OUTPUT + PEOPLE)
        if request.url.path == "/api/states":
            return httpx.Response(200, json=self.states)
        return super().handler(request)

    def put(self, eid, state):
        next(s for s in self.states if s["entity_id"] == eid)["state"] = state


@pytest.fixture
def app_rig(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ_NAME", "Europe/London")
    fake = PeopleHA()
    clock = Clock(ts(2026, 10, 9, 12))
    settings = Settings("http://ha.test", "test-token", "aaron", "s3cret", str(tmp_path / "layout.db"))
    ha = HAClient(settings.ha_url, settings.ha_token, transport=httpx.MockTransport(fake.handler))
    live = Live(ha, "ws://ha.test/api/websocket", settings.ha_token, use_ws=False)
    with TestClient(create_app(settings, ha, live, clock=clock)) as c:
        c.headers["Authorization"] = "Basic " + base64.b64encode(b"aaron:s3cret").decode()
        c.fake, c.clock, c.live = fake, clock, live
        yield c


def tick(c, advance=0):
    c.clock.t += advance
    c.live.load_states(c.fake.states)
    c.portal.call(c.app.state.automations.tick)


def test_api_auto_away_uses_the_real_away_and_home(app_rig):
    c, fake = app_rig, app_rig.fake
    s = c.get("/api/presence").json()
    assert s["enabled"] is False and [p["entity_id"] for p in s["people"]] == [A, B]
    assert s["people"][0] == {"entity_id": A, "name": "Alex", "model": "Person", "state": "home", "home": True,
                              "known_home": None, "tracked": True}
    assert not [d for d in c.get("/api/devices").json() if d["kind"] == "person" and d["entity_id"] not in (A, B)]
    pushes = []

    async def push(p):
        pushes.append(p)
    c.app.state.automations.presence.notify = push
    tick(c)
    fake.put(A, "not_home"), fake.put(B, "not_home")
    tick(c, 60)
    tick(c, 3600)
    assert not fake.service_calls()  # off by default
    assert c.put("/api/presence/settings", json={"away_minutes": 1}).status_code == 400
    s = c.put("/api/presence/settings", json={"enabled": True, "away_minutes": 5}).json()
    assert s["enabled"] and s["everyone_out"] and s["away_at"] == int((c.clock.t + 300) * 1000)
    tick(c, 299)
    assert not fake.service_calls()
    tick(c, 1)
    calls = fake.service_calls()
    # exactly what Away in the ⋯ menu does: lights + plugs off (one call per domain), radiators to 16°
    assert calls == [("/api/services/light/turn_off", {"entity_id": ["light.kitchen_1", "light.strip"]}),
                     ("/api/services/switch/turn_off", {"entity_id": ["switch.fan", "switch.kettle"]}),
                     ("/api/services/climate/set_temperature", {"entity_id": ["climate.lounge_valve"], "temperature": 16.0})]
    assert c.get("/api/mode").json()["mode"] == "away"
    assert c.get("/api/alerts/settings").json()["enabled"] is True
    assert [p["title"] for p in pushes] == ["Switched to Away — everyone left"]
    tick(c, 600)
    assert len(fake.service_calls()) == 3  # no duplicates
    fake.calls.clear()
    fake.put(A, "home")
    tick(c, 10)
    assert not fake.service_calls()
    assert c.get("/api/presence").json()["arrival_at"] is not None
    tick(c, ARRIVE_CONFIRM)
    assert fake.service_calls() == [("/api/services/climate/set_temperature", {"entity_id": ["climate.lounge_valve"], "temperature": 21.0})]
    assert c.get("/api/mode").json()["mode"] == "home"
    assert [p["title"] for p in pushes] == ["Switched to Away — everyone left", "Welcome home"]
    tick(c, 600)
    assert len(fake.service_calls()) == 1


def test_api_manual_mode_wins(app_rig):
    c, fake = app_rig, app_rig.fake
    c.put("/api/presence/settings", json={"enabled": True, "away_minutes": 2})
    tick(c)
    c.post("/api/mode", json={"mode": "away"})
    c.post("/api/mode", json={"mode": "home"})  # by hand, at 12:00
    fake.calls.clear()
    fake.put(A, "not_home"), fake.put(B, "not_home")
    tick(c, 60)
    tick(c, 600)
    s = c.get("/api/presence").json()
    assert not fake.service_calls() and s["blocked"] == "manual"
    tick(c, MANUAL_WINS - 660)
    assert [p for p, _ in fake.service_calls()][-1] == "/api/services/climate/set_temperature"
    assert c.get("/api/mode").json()["mode"] == "away"


def test_api_people_setting_and_no_people(app_rig):
    c = app_rig
    s = c.put("/api/presence/settings", json={"people": [B]}).json()
    assert [p["tracked"] for p in s["people"]] == [False, True]
    assert c.put("/api/presence/settings", json={"people": ["switch.fan"]}).status_code == 400
