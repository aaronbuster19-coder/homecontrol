"""Appliance run log (backend/runlog.py): the live energy meter, busy runs with an injected clock, cycles and charges
followed from the live trackers, costs at half-hourly or flat rates, restarts, notes and roles."""
import base64

import pytest

from backend import runlog as R
from backend.automations import AutoStore

T0 = 1_791_500_000.0  # a fixed epoch second (Oct 2026)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class FakeAppliances:
    def __init__(self):
        self.cycles, self.charges = {}, {}

    @staticmethod
    def key(f):
        return f"{f['id']}|{f['plug']}"


def flat(rate):
    return lambda p, a, b: (p * (b - a) / 3.6e6, p * (b - a) / 3.6e6 * rate if rate is not None else None)


def fur(fid, type_, plug, **kw):
    return {"id": fid, "type": type_, "x": 1, "y": 1, "w": 0.5, "h": 0.5, "plug": plug, **kw}


def plug(eid, power, state="on"):
    return {"entity_id": eid, "kind": "plug", "state": state, "power": power}


class Rig:
    def __init__(self, tmp_path, furniture, rate=30.0):
        self.clock, self.appl = Clock(), FakeAppliances()
        self.kv, self.path = AutoStore(str(tmp_path / "r.db")), str(tmp_path / "r.db")
        self.layout = {"furniture": furniture}
        self.rate = rate
        self.log = self.make()

    def make(self):
        return R.RunLog(R.RunStore(self.path), self.kv, lambda: self.layout, self.appl, flat(self.rate), self.clock)

    def at(self, minutes, item=None, items=None):
        self.clock.t = T0 + minutes * 60
        if item is not None:
            self.log.observe(item)
        if items is not None:
            self.log.tick(items)

    def runs(self, fid):
        return self.log.store.page(fid)


# ---------------- meter ----------------
def test_meter_integrates_steps_and_prices():
    m = R.Meter(flat(20.0))
    m.feed("p", 0, 1000)
    m.feed("p", 1800, 2000)       # 1 kW for 30 min
    m.feed("p", 3600, 0)          # 2 kW for 30 min
    assert m.between("p", 0, 3600) == (1.5, 30.0, False)
    assert m.between("p", 900, 2700) == (0.75, 15.0, False)  # interpolated inside steps
    assert m.between("p", -100, 1800) == (0.5, 10.0, True)  # started before the meter: partial
    assert m.between("q", 0, 10) == (None, None, True)
    m2 = R.Meter(flat(None))
    m2.feed("p", 0, 1000)
    m2.feed("p", 3600, 1000)
    assert m2.between("p", 0, 3600) == (1.0, None, False)  # no rate: kWh only
    m.feed("p", 3600 + 10, 0, force=False)  # a tick within 30 s with no change adds no sample
    assert len(m.samples["p"]) == 3
    m.feed("p", 3600 + 26 * 3600 + 60, 0)
    assert m.samples["p"][0][0] > 0  # old samples pruned


# ---------------- busy runs ----------------
def test_kettle_boil_logged_once(tmp_path):
    k = fur("k1", "kettle", "switch.kettle")
    r = Rig(tmp_path, [k])
    r.at(0, plug("switch.kettle", 0.0))
    r.at(1, plug("switch.kettle", 2400.0))
    r.at(4, plug("switch.kettle", 0.5))   # boiled for 3 min
    r.at(4.5, items={"switch.kettle": plug("switch.kettle", 0.5)})
    assert r.runs("k1") == []             # within the 1 min grace
    r.at(5.1, items={"switch.kettle": plug("switch.kettle", 0.5)})
    [run] = r.runs("k1")
    assert (run["start"], run["end"], run["minutes"], run["outcome"]) == (int((T0 + 60) * 1000), int((T0 + 240) * 1000), 3, "finished")
    assert run["kwh"] == pytest.approx(0.12, abs=1e-3) and run["cost_p"] == pytest.approx(3.6, abs=0.01) and not run["partial"]
    r.at(10, items={"switch.kettle": plug("switch.kettle", 0.5)})
    assert len(r.runs("k1")) == 1


def test_spikes_dropped_heater_pauses_merged_off_ends(tmp_path):
    k, h = fur("k1", "kettle", "switch.kettle"), fur("h1", "heater", "switch.heater")
    r = Rig(tmp_path, [k, h])
    r.at(0, plug("switch.kettle", 1500.0))
    r.at(0.2, plug("switch.kettle", 0.0))  # 12 s spike
    r.at(2, items={"switch.kettle": plug("switch.kettle", 0.0)})
    assert r.runs("k1") == []
    r.at(0, plug("switch.heater", 1500.0))
    r.at(20, plug("switch.heater", 1.0))   # thermostat pause, 8 min
    r.at(28, plug("switch.heater", 1500.0))
    r.at(40, items={"switch.heater": plug("switch.heater", 1500.0)})
    assert r.runs("h1") == []
    r.at(60, plug("switch.heater", 0.0, state="off"))  # switched off: the run ends now
    [run] = r.runs("h1")
    assert run["minutes"] == 60 and run["kwh"] == pytest.approx(1.5 * (52 / 60), abs=0.01)


def test_protected_appliances_have_no_runs(tmp_path):
    r = Rig(tmp_path, [fur("f1", "fridge", "switch.fridge"), fur("s1", "home_server", "switch.server")])
    r.at(0, plug("switch.fridge", 80.0))
    r.at(60, plug("switch.fridge", 2.0, state="off"))
    r.at(0, plug("switch.server", 35.0))
    r.at(60, plug("switch.server", 0.0, state="off"))
    assert r.runs("f1") == [] and r.runs("s1") == [] and r.log.meter.samples == {}


# ---------------- cycles and charges (from the live trackers) ----------------
def test_washer_cycle_finished_and_stopped(tmp_path):
    w = fur("w1", "washer", "switch.washer")
    r = Rig(tmp_path, [w])
    key = "w1|switch.washer"
    r.at(0, plug("switch.washer", 2000.0))
    r.appl.cycles[key] = {"phase": "running", "run_start": T0, "finished_at": None}
    r.at(2, plug("switch.washer", 2000.0))
    assert r.log.open_run(w)["start"] == int(T0 * 1000)
    r.at(60, plug("switch.washer", 1.0))
    r.appl.cycles[key] = {"phase": "finished", "run_start": T0, "finished_at": T0 + 3600}
    r.at(63, items={"switch.washer": plug("switch.washer", 1.0)})
    [run] = r.runs("w1")
    assert (run["outcome"], run["minutes"]) == ("finished", 60)
    assert run["kwh"] == pytest.approx(2.0, abs=1e-3) and run["cost_p"] == pytest.approx(60.0, abs=0.01)
    r.at(70, items={"switch.washer": plug("switch.washer", 1.0)})
    assert len(r.runs("w1")) == 1 and r.log.open_run(w) is None
    # the next one is switched off mid-cycle
    r.appl.cycles[key] = {"phase": "running", "run_start": T0 + 100 * 60, "finished_at": None}
    r.at(101, items={"switch.washer": plug("switch.washer", 500.0)})
    r.appl.cycles[key] = {"phase": "idle", "run_start": T0 + 100 * 60, "finished_at": None}
    r.at(130, plug("switch.washer", 0.0, state="off"))
    runs = r.runs("w1")
    assert [x["outcome"] for x in runs] == ["stopped", "finished"] and runs[0]["minutes"] == 30


def test_restart_mid_cycle_is_partial(tmp_path):
    w = fur("w1", "washer", "switch.washer")
    r = Rig(tmp_path, [w])
    key = "w1|switch.washer"
    r.appl.cycles[key] = {"phase": "running", "run_start": T0, "finished_at": None}
    r.at(1, plug("switch.washer", 1000.0))
    r.log = r.make()  # restart: the meter starts again, the open cycle is remembered
    r.at(30, plug("switch.washer", 1000.0))
    r.appl.cycles[key] = {"phase": "finished", "run_start": T0, "finished_at": T0 + 90 * 60}
    r.at(93, items={"switch.washer": plug("switch.washer", 1.0)})
    [run] = r.runs("w1")
    assert run["partial"] and run["minutes"] == 90 and run["kwh"] == pytest.approx(1.0, abs=1e-3)


def test_hoover_charge(tmp_path):
    hv = fur("hv", "hoover", "switch.hoover")
    r = Rig(tmp_path, [hv], rate=None)
    r.at(0, plug("switch.hoover", 45.0))
    r.appl.charges["hv|switch.hoover"] = {"phase": "charging", "charge_start": T0, "charged_at": None}
    r.at(1, items={"switch.hoover": plug("switch.hoover", 45.0)})
    r.appl.charges["hv|switch.hoover"] = {"phase": "charged", "charge_start": T0, "charged_at": T0 + 3000}
    r.at(60, items={"switch.hoover": plug("switch.hoover", 1.0)})
    [run] = r.runs("hv")
    assert run["outcome"] == "charged" and run["minutes"] == 50 and run["cost_p"] is None and run["kwh"] > 0


def test_unlinked_pieces_are_forgotten(tmp_path):
    k = fur("k1", "kettle", "switch.kettle")
    r = Rig(tmp_path, [k])
    r.at(0, plug("switch.kettle", 2000.0))
    assert r.log.busy
    r.layout["furniture"] = []
    r.at(1, items={"switch.kettle": plug("switch.kettle", 2000.0)})
    assert r.log.busy == {} and r.log.meter.samples == {} and r.kv.get(R.STATE_KEY)["busy"] == {}


def test_note_cleaning():
    assert R.clean_note("  Wool   wash  ") == "Wool wash" and R.clean_note(None) == ""
    with pytest.raises(ValueError):
        R.clean_note("x" * 201)
    with pytest.raises(ValueError):
        R.clean_note(5)


# ---------------- API ----------------
def basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_runs_api_notes_and_roles(client):
    lay = {"rooms": [], "placements": [], "furniture": [fur("w1", "washer", "switch.fan")]}
    assert client.put("/api/layout", json=lay).status_code == 200
    for name, role in (("mia", "member"), ("gus", "guest")):
        assert client.post("/api/users", json={"username": name, "password": "pass-word-1", "role": role}).status_code == 200
    runs = client.app.state.tariff.runlog
    ids = [runs.store.add({"fid": "w1", "plug": "switch.fan", "type": "washer", "start": T0 + i * 7200,
                           "end": T0 + i * 7200 + 3600, "kwh": 1.1, "cost_p": 30.2, "outcome": "finished"}) for i in range(3)]
    r = client.get("/api/appliances/w1/runs?limit=2")
    assert r.status_code == 200
    d = r.json()
    assert d["tracked"] and [x["id"] for x in d["runs"]] == ids[:0:-1] and d["next_before"] == d["runs"][-1]["start"]
    assert d["runs"][0]["minutes"] == 60 and d["runs"][0]["note"] == ""
    d2 = client.get(f"/api/appliances/w1/runs?before={d['next_before']}").json()
    assert [x["id"] for x in d2["runs"]] == [ids[0]] and d2["next_before"] is None
    mia = basic("mia", "pass-word-1")
    r = client.put(f"/api/appliances/w1/runs/{ids[0]}", json={"note": " Wool  wash, 30° "}, headers=mia)
    assert r.status_code == 200 and r.json()["note"] == "Wool wash, 30°"
    assert client.get("/api/appliances/w1/runs", headers=mia).json()["runs"][-1]["note"] == "Wool wash, 30°"
    assert client.put(f"/api/appliances/w1/runs/{ids[0]}", json={"note": "x" * 201}).status_code == 400
    assert client.put(f"/api/appliances/w1/runs/{ids[0]}", json={"note": "a", "x": 1}).status_code == 400
    assert client.put("/api/appliances/w1/runs/9999", json={"note": "a"}).status_code == 404
    assert client.put(f"/api/appliances/other/runs/{ids[0]}", json={"note": "a"}).status_code == 404
    gus = basic("gus", "pass-word-1")
    assert client.get("/api/appliances/w1/runs", headers=gus).status_code == 403
    assert client.put(f"/api/appliances/w1/runs/{ids[0]}", json={"note": "a"}, headers=gus).status_code == 403
