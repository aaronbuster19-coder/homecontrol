"""Octopus tariff (backend/tariff.py): parsing rates, cheapest windows, half-hourly pricing, the fetch schedule, the
offline cache, the reminder (opt-in, once, never in quiet hours) and the API with roles. Octopus is always faked."""
import asyncio
import base64
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest

from backend import tariff as T
from backend.automations import AutoStore
from backend.energy import Energy
from backend.history import ms

LON = ZoneInfo("Europe/London")
SLOT = T.SLOT


def at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=LON)


def z(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def agile(day: date, price=lambda h, m: 20.0, tz=LON):
    """Octopus-shaped results for every half hour of a local day (newest first, like the API)."""
    out = []
    for s in T.day_slots(day, tz):
        loc = datetime.fromtimestamp(s / 1000, tz)
        out.append({"value_exc_vat": 0, "value_inc_vat": price(loc.hour, loc.minute),
                    "valid_from": z(datetime.fromtimestamp(s / 1000, timezone.utc)),
                    "valid_to": z(datetime.fromtimestamp((s + SLOT) / 1000, timezone.utc)), "payment_method": None})
    return out[::-1]


def cheap_night(h, m):  # 7.5p 01:00–04:00, 30p 16:00–19:00, else 20p
    return 7.5 if 1 <= h < 4 else 30.0 if 16 <= h < 19 else 20.0


# ---------------- pure helpers ----------------
def test_parse_rates_agile_go_fixed_and_payment_method():
    a, b = ms(at(2026, 10, 9, 0)), ms(at(2026, 10, 10, 0))
    rates = T.parse_rates(agile(date(2026, 10, 9), cheap_night), a, b)
    assert len(rates) == 48 and rates[ms(at(2026, 10, 9, 1))] == 7.5 and rates[ms(at(2026, 10, 9, 16, 30))] == 30.0
    go = [{"value_inc_vat": 8.5, "valid_from": z(at(2026, 10, 9, 0, 30)), "valid_to": z(at(2026, 10, 9, 5, 30)), "payment_method": None},
          {"value_inc_vat": 27.0, "valid_from": z(at(2026, 10, 9, 5, 30)), "valid_to": z(at(2026, 10, 10, 0, 30)), "payment_method": None}]
    r = T.parse_rates(go, a, b)
    assert r[ms(at(2026, 10, 9, 0, 30))] == 8.5 and r[ms(at(2026, 10, 9, 5))] == 8.5 and r[ms(at(2026, 10, 9, 5, 30))] == 27.0
    assert a not in r and len(r) == 47  # 00:00–00:30 isn't covered
    fixed = [{"value_inc_vat": 24.5, "valid_from": "2024-01-01T00:00:00Z", "valid_to": None},
             {"value_inc_vat": 26.0, "valid_from": "2024-01-01T00:00:00Z", "valid_to": None, "payment_method": "NON_DIRECT_DEBIT"},
             {"value_inc_vat": "x", "valid_from": "2024-01-01T00:00:00Z"}, {"value_inc_vat": 3}, "junk"]
    r = T.parse_rates(fixed, a, b)
    assert len(r) == 48 and set(r.values()) == {24.5}  # direct debit beats non-direct-debit, junk skipped


def test_negative_prices_and_cheapest_window():
    d = date(2026, 10, 9)
    a, b = ms(at(2026, 10, 9, 0)), ms(at(2026, 10, 10, 0))
    rates = T.parse_rates(agile(d, lambda h, m: -2.0 if (h, m) == (13, 0) else cheap_night(h, m)), a, b)
    now = ms(at(2026, 10, 9, 0, 10))
    w = T.cheapest(rates, now, 120)
    assert (w["start"], w["end"], w["avg_p"]) == (ms(at(2026, 10, 9, 1)), ms(at(2026, 10, 9, 3)), 7.5)
    w = T.cheapest(rates, now, 30)
    assert w["start"] == ms(at(2026, 10, 9, 13)) and w["avg_p"] == -2.0
    # from 02:20 a 2 h run: start now (02:20–04:20) beats any later half hour
    w = T.cheapest(rates, ms(at(2026, 10, 9, 2, 20)), 120)
    assert w["start"] == ms(at(2026, 10, 9, 2, 20)) and w["avg_p"] == round((100 * 7.5 + 20 * 20) / 120, 2)
    # not enough known ahead
    assert T.cheapest(rates, ms(at(2026, 10, 9, 23)), 120) is None
    assert T.cheapest({}, now, 60) is None
    # equal prices: the earliest
    flat = T.parse_rates(agile(d), a, b)
    assert T.cheapest(flat, a, 60)["start"] == a


def test_window_avg_and_price_power_split_by_half_hour():
    a = ms(at(2026, 10, 9, 0))
    rates = {a: 10.0, a + SLOT: 30.0}
    assert T.window_avg(rates, a + 15 * 60_000, a + 45 * 60_000) == 20.0
    assert T.window_avg(rates, a, a + 3 * SLOT) is None
    kwh, p = T.price_power(rates, 2000, a + 15 * 60_000, a + 45 * 60_000, None)  # 2 kW for 30 min, half each rate
    assert kwh == pytest.approx(1.0) and p == pytest.approx(0.5 * 10 + 0.5 * 30)
    kwh, p = T.price_power(rates, 1000, a + SLOT, a + 3 * SLOT, None)
    assert kwh == pytest.approx(1.0) and p is None  # the second hour has no rate and no fallback
    assert T.price_power(rates, 1000, a + SLOT, a + 3 * SLOT, 20.0)[1] == pytest.approx(0.5 * 30 + 0.5 * 20)
    kwh, p = T.price_segs(rates, [(a, a + SLOT, 1000), (a + SLOT, a + 2 * SLOT, None), (a + SLOT, a + 2 * SLOT, 2000)], a, a + 2 * SLOT, None)
    assert kwh == pytest.approx(1.5) and p == pytest.approx(0.5 * 10 + 1.0 * 30)


def test_day_summary_dst_and_missing():
    rates = T.parse_rates(agile(date(2026, 10, 25)), ms(at(2026, 10, 25, 0)), ms(at(2026, 10, 26, 0)))
    s = T.day_summary(rates, date(2026, 10, 25), LON, "Today")
    assert len(s["slots"]) == 50 and s["complete"] and s["avg"] == 20.0  # clocks go back: 25 hours
    assert len(T.day_slots(date(2026, 3, 29), LON)) == 46
    s = T.day_summary({}, date(2026, 10, 26), LON, "Tomorrow")
    assert not s["complete"] and s["known"] == 0 and s["min"] is None and s["cheapest"] is None


def test_fetch_schedule():
    morning = at(2026, 10, 9, 9).timestamp()
    assert T.next_fetch(morning, LON, False) == morning + 6 * 3600  # 15:00, at most 6 h on
    assert T.next_fetch(at(2026, 10, 9, 11).timestamp(), LON, False) == at(2026, 10, 9, 15, 45).timestamp()
    assert T.next_fetch(morning, LON, True) == morning + 6 * 3600
    late = at(2026, 10, 9, 16, 5).timestamp()
    assert T.next_fetch(late, LON, False) == late + 15 * 60
    early = at(2026, 10, 9, 2).timestamp()
    assert T.next_fetch(early, LON, False) == early + 6 * 3600
    assert [T.backoff(n) for n in (1, 2, 3, 4, 5, 9)] == [300, 600, 1200, 2400, 3600, 3600]


def test_validate_settings():
    s = T.validate_settings({"enabled": True, "product": " go-var-22-10-14 ", "region": "j", "remind_lead_min": 30}, {})
    assert s == {**T.DEFAULTS, "enabled": True, "product": "GO-VAR-22-10-14", "region": "J", "remind_lead_min": 30}
    assert T.validate_settings({}, {"remind": True})["remind"] is True
    for bad in ({"enabled": 1}, {"product": "agile/../x"}, {"product": ""}, {"region": "I"}, {"region": 3},
                {"remind_lead_min": 2}, {"remind_lead_min": True}, {"remind_lead_min": 15.5}, {"colour": "red"}, []):
        with pytest.raises(T.TariffError):
            T.validate_settings(bad, {})
    assert T.DEFAULTS["enabled"] is False and T.DEFAULTS["remind"] is False  # off by default


# ---------------- the service, with a fake Octopus and an injected clock ----------------
class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakeOctopus:
    def __init__(self, price=cheap_night):
        self.urls, self.fail, self.price, self.days = [], None, price, None

    async def __call__(self, url):
        self.urls.append(url)
        if self.fail == 404:
            raise httpx.HTTPStatusError("nope", request=httpx.Request("GET", url), response=httpx.Response(404))
        if self.fail:
            raise httpx.ConnectError("offline")
        q = parse_qs(urlparse(url).query)
        a = datetime.fromisoformat(q["period_from"][0].replace("Z", "+00:00")).astimezone(LON).date()
        b = datetime.fromisoformat(q["period_to"][0].replace("Z", "+00:00")).astimezone(LON).date()
        days = [a + timedelta(days=i) for i in range((b - a).days)]
        if self.days is not None:
            days = [d for d in days if d in self.days]
        return {"count": 1, "next": None, "previous": None, "results": [r for d in days for r in agile(d, self.price)]}


class FakeAppliances:
    def __init__(self):
        self.cycles, self.charges = {}, {}

    @staticmethod
    def key(f):
        return f"{f['id']}|{f['plug']}"


class FakeStats:
    def __init__(self, cycles=None, fail=False):
        self.c, self.fail, self.calls = cycles, fail, 0

    async def stats(self, f, dev, layout):
        self.calls += 1
        if self.fail:
            raise RuntimeError("HA down")
        return {"cycles": self.c} if self.c else {}


WASHER = {"id": "w1", "type": "washer", "x": 1, "y": 1, "w": 0.6, "h": 0.6, "plug": "switch.washer"}


def sub(tmp_path, name):
    (tmp_path / name).mkdir()
    return tmp_path / name


class Rig:
    def __init__(self, tmp_path, now, layout=None, stats=None, quiet=False):
        self.clock, self.octo, self.pushes = Clock(now.timestamp()), FakeOctopus(), []
        self.kv, self.path = AutoStore(str(tmp_path / "t.db")), str(tmp_path / "t.db")
        self.layout = layout or {"furniture": [WASHER], "settings": {}}
        self.appl, self.stats, self.quiet = FakeAppliances(), stats or FakeStats({"avg_min": 120, "kwh_per_cycle": 1.2}), quiet
        self.t = self.make()

    def make(self):
        async def notify(p):
            self.pushes.append(p)

        async def devices():
            return {"switch.washer": object()}
        live = type("L", (), {"devices": {}, "states": {}})()
        return T.Tariff(self.kv, T.RateStore(self.path), lambda: self.layout, live, None, self.appl, self.stats, devices,
                        notify, lambda: self.quiet, self.clock, LON, fetch=self.octo, base="https://octo.test/v1")

    def run(self, coro):
        return asyncio.run(coro)


def test_disabled_never_fetches(tmp_path):
    r = Rig(tmp_path, at(2026, 10, 9, 9))
    r.run(r.t.tick())
    r.run(r.t.force_refresh())
    assert r.octo.urls == [] and r.run(r.t.status())["suggestions"] == []


def test_fetch_store_offline_and_schedule(tmp_path):
    r = Rig(tmp_path, at(2026, 10, 9, 9))
    r.t.put_settings({"enabled": True})
    r.run(r.t.tick())
    assert len(r.octo.urls) == 1
    u = urlparse(r.octo.urls[0])
    assert u.netloc == "octo.test" and u.path == "/v1/products/AGILE-24-10-01/electricity-tariffs/E-1R-AGILE-24-10-01-C/standard-unit-rates/"
    q = parse_qs(u.query)
    # first fetch: from the 1st of the month (before this week's Monday) to the end of tomorrow
    assert q["period_from"] == [z(at(2026, 10, 1, 0))] and q["period_to"] == [z(at(2026, 10, 11, 0))] and q["page_size"] == ["1500"]
    st = r.run(r.t.status())
    assert st["current"]["p"] == 20.0 and st["today"]["complete"] and st["tomorrow"]["complete"] and st["error"] is None
    assert st["today"]["cheapest"] == {"start": ms(at(2026, 10, 9, 1)), "p": 7.5}
    assert st["next_fetch"] == int((at(2026, 10, 9, 9).timestamp() + 6 * 3600) * 1000)
    r.run(r.t.tick())
    assert len(r.octo.urls) == 1  # not due yet
    # next due fetch only asks for today and tomorrow (the past is stored)
    r.clock.t += 6 * 3600 + 1
    r.run(r.t.tick())
    assert parse_qs(urlparse(r.octo.urls[1]).query)["period_from"] == [z(at(2026, 10, 9, 0))]
    # Octopus down: rates kept (stale), error shown, backoff
    r.octo.fail = True
    r.clock.t += 7 * 3600
    r.run(r.t.tick())
    st = r.run(r.t.status())
    assert st["stale"] and "unreachable" in st["error"] and st["current"]["p"] == 20.0
    assert st["next_fetch"] == int((r.clock.t + 300) * 1000)
    # a restart without the network still has the rates
    r2 = r.make()
    assert r2.rates == r.t.rates and r.run(r2.status())["current"] is not None


def test_tomorrow_late_polls_until_published(tmp_path):
    r = Rig(tmp_path, at(2026, 10, 9, 15, 50))
    r.octo.days = {date(2026, 10, d) for d in range(1, 10)}  # tomorrow not out yet
    r.t.put_settings({"enabled": True})
    r.run(r.t.tick())
    st = r.run(r.t.status())
    assert not st["tomorrow"]["complete"] and st["next_fetch"] == int((r.clock.t + 15 * 60) * 1000)
    r.octo.days = None
    r.clock.t += 15 * 60
    r.run(r.t.tick())
    assert r.run(r.t.status())["tomorrow"]["complete"] and len(r.octo.urls) == 2


def test_bad_product_404_and_switching(tmp_path):
    r = Rig(tmp_path, at(2026, 10, 9, 9))
    r.octo.fail = 404
    r.t.put_settings({"enabled": True, "product": "NOPE-1"})
    r.run(r.t.tick())
    st = r.run(r.t.status())
    assert "check the product code" in st["error"] and st["current"] is None and not st["stale"]
    r.octo.fail = None
    r.t.put_settings({"product": "AGILE-24-10-01"})  # a different code fetches straight away
    r.run(r.t.tick())
    assert r.run(r.t.status())["error"] is None and "E-1R-AGILE-24-10-01-C" in r.octo.urls[-1]


def test_pagination_only_on_the_configured_host(tmp_path):
    r = Rig(tmp_path, at(2026, 10, 9, 9))
    pages = []

    async def fetch(url):
        pages.append(url)
        if len(pages) == 1:
            return {"results": agile(date(2026, 10, 9))[:10], "next": "https://octo.test/v1/page2"}
        if len(pages) == 2:
            return {"results": agile(date(2026, 10, 9))[10:], "next": "https://evil.test/v1/page3"}
        raise AssertionError("followed a foreign next link")
    r.t.fetch = fetch
    r.t.put_settings({"enabled": True})
    r.run(r.t.tick())
    assert len(pages) == 2 and len(r.t.rates) == 48


def test_suggestions_typical_length(tmp_path):
    r = Rig(tmp_path, at(2026, 10, 9, 22, 10))
    r.t.put_settings({"enabled": True})
    r.run(r.t.tick())
    g = r.run(r.t.suggestions())[0]
    assert (g["fid"], g["name"], g["minutes"], g["source"]) == ("w1", "Washing machine", 120, "history")
    assert (g["start"], g["end"], g["avg_p"]) == (ms(at(2026, 10, 10, 1)), ms(at(2026, 10, 10, 3)), 7.5)
    assert g["now_avg_p"] == 20.0 and g["est_p"] == 9.0 and g["saving_p"] == 15.0 and not g["starts_now"]
    r.run(r.t.suggestions())
    assert r.stats.calls == 1  # typical length cached
    # no history: a default, labelled so
    r2 = Rig(sub(tmp_path, "b"), at(2026, 10, 9, 22, 10), stats=FakeStats(fail=True))
    r2.t.put_settings({"enabled": True})
    r2.run(r2.t.tick())
    g = r2.run(r2.t.suggestions())[0]
    assert (g["minutes"], g["source"], g["kwh"], g["est_p"]) == (120, "default", None, None)


def test_reminder_opt_in_once_and_quiet(tmp_path):
    r = Rig(tmp_path, at(2026, 10, 9, 22, 10))
    r.t.put_settings({"enabled": True})
    r.run(r.t.tick())
    r.clock.t = at(2026, 10, 10, 0, 50).timestamp()
    r.run(r.t.tick())
    assert r.pushes == []  # reminder is off by default
    r.t.put_settings({"remind": True, "remind_lead_min": 15})
    r.clock.t = at(2026, 10, 10, 0, 40).timestamp()
    r.run(r.t.tick())
    assert r.pushes == []  # not yet: 20 min before
    r.clock.t = at(2026, 10, 10, 0, 46).timestamp()
    r.run(r.t.tick())
    r.run(r.t.tick())
    r.t = r.make()  # a restart doesn't repeat it either
    r.run(r.t.tick())
    assert len(r.pushes) == 1
    p = r.pushes[0]
    assert p["title"] == "Cheap electricity from 01:00" and p["url"] == "/?tariff" and p["tag"] == "tariff-w1"
    # now (00:46–02:46) is mostly cheap already: 14 min at 20p, the rest at 7.5p
    assert "Washing machine: 01:00–03:00 averages 7.5p/kWh (≈9p a run, 2p less than now)" in p["body"]
    assert "Nothing is switched on automatically" in p["body"]


def test_reminder_skipped_in_quiet_hours_or_running(tmp_path):
    r = Rig(tmp_path, at(2026, 10, 9, 22, 10), quiet=True)
    r.t.put_settings({"enabled": True, "remind": True})
    r.run(r.t.tick())
    r.clock.t = at(2026, 10, 10, 0, 50).timestamp()
    r.run(r.t.tick())
    r.quiet = False
    r.run(r.t.tick())
    assert r.pushes == []  # skipped, not held for later
    r2 = Rig(sub(tmp_path, "x"), at(2026, 10, 9, 22, 10))
    r2.appl.cycles["w1|switch.washer"] = {"phase": "running", "run_start": 1}
    r2.t.put_settings({"enabled": True, "remind": True})
    r2.run(r2.t.tick())
    r2.clock.t = at(2026, 10, 10, 0, 50).timestamp()
    r2.run(r2.t.tick())
    assert r2.pushes == []


def test_pricer_for_energy_costs(tmp_path):
    r = Rig(tmp_path, at(2026, 10, 9, 9))
    a = ms(at(2026, 10, 9, 0))
    segs = [(a, ms(at(2026, 10, 9, 2)), 1000.0)]  # 1 kW 00:00–02:00: 1 h at 20p, 1 h at 7.5p
    assert r.t.pricer(segs, a, ms(at(2026, 10, 9, 2)), 2.0, 25.0) is None  # disabled: the flat rate
    r.t.put_settings({"enabled": True})
    r.run(r.t.tick())
    assert r.t.pricer(segs, a, ms(at(2026, 10, 9, 2)), 2.0, 25.0) == 27.5
    assert r.t.pricer(segs, a, ms(at(2026, 10, 9, 2)), 4.0, 25.0) == 55.0  # meter kWh, history's price shape
    assert r.t.info()["name"] == "Agile Octopus"
    r.t.put_settings({"use_for_costs": False})
    assert r.t.pricer(segs, a, ms(at(2026, 10, 9, 2)), 2.0, 25.0) is None and r.t.info() is None


def test_energy_totals_with_half_hourly_rates():
    class D:
        def __init__(self):
            self.entity_id, self.name, self.hidden, self.related = "switch.k", "Kettle", False, {"power": "sensor.k"}

    class HA:
        async def history(self, a, b, ids):
            t = at(2026, 10, 9, 1).astimezone(timezone.utc)
            return [[{"entity_id": "sensor.k", "state": "0", "last_changed": a.isoformat()},
                     {"entity_id": "sensor.k", "state": "2000", "last_changed": t.isoformat()},
                     {"entity_id": "sensor.k", "state": "0", "last_changed": (t + timedelta(hours=1)).isoformat()}]]
    e = Energy(HA(), tz=LON)
    now = at(2026, 10, 9, 9)
    layout = {"settings": {"energy": {"rate_p": 25.0}}}
    flat = asyncio.run(e.totals([D()], {}, layout, "today", now))
    assert flat["total_kwh"] == 2.0 and flat["total_p"] == 50.0 and flat["tariff"] is None
    rates = {s: 7.5 for s in range(ms(at(2026, 10, 9, 0)), ms(at(2026, 10, 10, 0)), SLOT)}
    e.pricer = lambda segs, a, b, kwh, flat_p: round(T.price_segs(rates, segs or [], a, b, flat_p)[1], 2)
    e.tariff_info = lambda: {"name": "Agile Octopus"}
    r = asyncio.run(e.totals([D()], {}, layout, "today", now))
    assert r["total_p"] == 15.0 and r["plugs"][0]["cost_p"] == 15.0 and r["tariff"] == {"name": "Agile Octopus"}


# ---------------- API + roles ----------------
def basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_api_settings_refresh_and_roles(client):
    for name, role in (("mia", "member"), ("gus", "guest")):
        assert client.post("/api/users", json={"username": name, "password": "pass-word-1", "role": role}).status_code == 200
    t = client.app.state.tariff
    octo = FakeOctopus()
    t.fetch = octo
    r = client.get("/api/tariff")
    assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["region_name"] == "London"
    assert client.post("/api/tariff/refresh").json()["error"] is None and octo.urls == []  # disabled: nothing fetched
    assert client.put("/api/tariff/settings", json={"region": "Z"}).status_code == 400
    assert client.put("/api/tariff/settings", json={"enabled": True}, headers=basic("mia", "pass-word-1")).status_code == 403
    r = client.put("/api/tariff/settings", json={"enabled": True, "region": "A"})
    assert r.status_code == 200 and r.json()["settings"]["region"] == "A"
    r = client.post("/api/tariff/refresh", headers=basic("mia", "pass-word-1"))  # members may refresh
    assert r.status_code == 200 and len(octo.urls) == 1 and "E-1R-AGILE-24-10-01-A" in octo.urls[0]
    assert r.json()["today"]["known"] > 0
    client.post("/api/tariff/refresh")
    assert len(octo.urls) == 1  # forced refreshes are rate-limited
    for method, path in (("get", "/api/tariff"), ("post", "/api/tariff/refresh"), ("put", "/api/tariff/settings")):
        assert getattr(client, method)(path, headers=basic("gus", "pass-word-1"), **({"json": {}} if method == "put" else {})).status_code == 403
