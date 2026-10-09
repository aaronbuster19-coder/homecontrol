import asyncio
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from backend.automations import AutoStore
from backend.tests.conftest import STATES, FakeHA
from backend.weather import (Weather, WeatherError, compass, current, items, parse_forecasts, summarize,
                             twice_daily_to_daily)

LONDON = ZoneInfo("Europe/London")
NOW = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc).timestamp()  # 16:00 BST
WX = {"entity_id": "weather.forecast_home", "state": "partlycloudy",
      "attributes": {"temperature": 12.3, "apparent_temperature": 10.1, "humidity": 71, "wind_speed": 16.1,
                     "wind_speed_unit": "km/h", "wind_bearing": 225, "temperature_unit": "°C", "friendly_name": "Forecast Home"}}


def hourly(start: float, temps):
    return [{"datetime": datetime.fromtimestamp(start + i * 3600, timezone.utc).isoformat(), "condition": "cloudy",
             "temperature": t, "precipitation": 0.1, "precipitation_probability": 20} for i, t in enumerate(temps)]


def daily(start_day, rows):
    return [{"datetime": datetime.combine(start_day + timedelta(days=i), datetime.min.time(), LONDON).replace(hour=12).isoformat(),
             "condition": c, "temperature": hi, "templow": lo, "precipitation": 0.0} for i, (c, hi, lo) in enumerate(rows)]


# ---------------- parsing ----------------
def test_parse_forecast_response_shapes():
    fc = [{"datetime": "2026-10-09T16:00:00+00:00", "temperature": 11}]
    assert parse_forecasts({"changed_states": [], "service_response": {"weather.x": {"forecast": fc}}}, "weather.x") == fc
    assert parse_forecasts({"weather.x": {"forecast": fc}}, "weather.x") == fc  # websocket / script response
    assert parse_forecasts([], "weather.x") is None                          # HA without return_response: changed states
    assert parse_forecasts({"service_response": {}}, "weather.x") is None


def test_current_conditions_units():
    c = current(WX)
    assert c["text"] == "Partly cloudy" and c["temperature"] == 12.3 and c["apparent_temperature"] == 10.1
    assert c["humidity"] == 71 and c["wind_mph"] == 10.0 and c["wind_dir"] == "SW"
    f = current({"state": "sunny", "attributes": {"temperature": 50, "temperature_unit": "°F", "wind_speed": 5, "wind_speed_unit": "m/s"}})
    assert f["temperature"] == 10.0 and f["wind_mph"] == 11.2 and f["apparent_temperature"] is None
    assert current({"state": "unavailable"}) is None
    assert compass(350) == "N" and compass(None) is None


def test_items_skip_bad_entries_and_sort():
    out = items([{"datetime": "2026-10-09T18:00:00Z", "temperature": 9}, {"datetime": "bad", "temperature": 1},
                 {"datetime": "2026-10-09T17:00:00+00:00", "temperature": "x"},
                 {"datetime": "2026-10-09T16:00:00+00:00", "temperature": 10, "condition": "rainy"}], {})
    assert [i["temperature"] for i in out] == [10, 9]


def test_summary_cold_night_from_hourly():
    hrs = items(hourly(NOW, [12, 11, 9, 7, 5, 4, 3, 2, 1.4, 1, 1.2, 2, 3, 4, 6, 8, 9, 10]), {})
    days = items(daily(datetime(2026, 10, 9).date(), [("sunny", 14, 6), ("rainy", 11, 1), ("cloudy", 12, 5),
                                                      ("cloudy", 13, 6), ("snowy", 4, -1), ("fog", 8, 2)]), {})
    s = summarize(current(WX), hrs, days, NOW, LONDON)
    assert len(s["hourly"]) == 12 and len(s["daily"]) == 5
    assert s["today"] == {"high": 14, "low": 6}
    assert s["tonight"] == {"low": 1, "cold": True}
    warm = summarize(None, items(hourly(NOW, [12] * 20), {}), days, NOW, LONDON)
    assert warm["tonight"] == {"low": 12, "cold": False}
    no_hourly = summarize(None, [], days, NOW, LONDON)  # tomorrow's low stands in for tonight
    assert no_hourly["tonight"] == {"low": 1, "cold": True}


def test_twice_daily_folds_into_days():
    fc = items([{"datetime": "2026-10-09T11:00:00+00:00", "temperature": 13, "condition": "sunny", "is_daytime": True},
                {"datetime": "2026-10-09T20:00:00+00:00", "temperature": 4, "condition": "clear-night", "is_daytime": False}], {})
    d = twice_daily_to_daily(fc, LONDON)
    assert len(d) == 1 and d[0]["temperature"] == 13 and d[0]["templow"] == 4 and d[0]["condition"] == "sunny"


# ---------------- Weather with a fake HA ----------------
class Live:
    def __init__(self, states):
        self.states = {s["entity_id"]: s for s in states}


class ServiceHA:
    def __init__(self, hourly_fc=None, daily_fc=None, fail=None):
        self.hourly, self.daily, self.fail, self.calls = hourly_fc or [], daily_fc or [], fail or set(), []

    async def call_service_response(self, domain, service, data):
        self.calls.append((domain, service, data["type"]))
        if data["type"] in self.fail:
            raise RuntimeError("Home Assistant returned 400")
        fc = self.hourly if data["type"] == "hourly" else self.daily
        return {"changed_states": [], "service_response": {data["entity_id"]: {"forecast": fc}}}


def make(tmp_path, ha, states=(WX,), heating=None, notified=None, now=NOW):
    clock = {"t": now}

    async def notify(p, cat):
        notified.append((p, cat))
    w = Weather(ha, Live(list(states)), AutoStore(str(tmp_path / "w.db")), notify if notified is not None else None,
                (lambda: heating), lambda: clock["t"], LONDON)
    return w, clock


def run(c):
    return asyncio.run(c)


def test_get_caches_forecasts_15_min(tmp_path):
    ha = ServiceHA(hourly(NOW, [10] * 24), daily(datetime(2026, 10, 9).date(), [("sunny", 14, 6)] * 6))
    w, _ = make(tmp_path, ha)
    r = run(w.get())
    assert r["available"] and r["entity_id"] == "weather.forecast_home" and r["forecast_error"] is None
    assert r["current"]["temperature"] == 12.3 and len(r["hourly"]) == 12 and len(r["daily"]) == 5
    run(w.get())
    assert ha.calls == [("weather", "get_forecasts", "hourly"), ("weather", "get_forecasts", "daily")]
    w.cache["weather.forecast_home"] = (w.cache["weather.forecast_home"][0] - 901, w.cache["weather.forecast_home"][1])
    run(w.get())
    assert len(ha.calls) == 4


def test_errors_keep_current_conditions_and_fall_back(tmp_path):
    ha = ServiceHA(fail={"hourly", "daily", "twice_daily"})
    w, _ = make(tmp_path, ha)
    r = run(w.get())
    assert r["available"] and r["current"]["text"] == "Partly cloudy"
    assert r["hourly"] == [] and "hourly: Home Assistant returned 400" in r["forecast_error"]
    assert ("weather", "get_forecasts", "twice_daily") in ha.calls
    # an old HA with the forecast attribute
    legacy = {**WX, "attributes": {**WX["attributes"], "forecast": hourly(NOW, [9, 8, 7, 6])}}
    w2, _ = make(tmp_path, ServiceHA(fail={"hourly", "daily", "twice_daily"}), states=(legacy,))
    r2 = run(w2.get())
    assert r2["forecast_error"] is None and [h["temperature"] for h in r2["hourly"]] == [9, 8, 7, 6]


def test_no_weather_entity(tmp_path):
    w, _ = make(tmp_path, ServiceHA(), states=())
    assert run(w.get()) == {"entities": [], "settings": {"entity_id": None, "frost_push": False}, "entity_id": None,
                            "available": False}


def test_entity_choice_and_settings(tmp_path):
    other = {**WX, "entity_id": "weather.aaa", "attributes": {**WX["attributes"], "friendly_name": "Other"}}
    w, _ = make(tmp_path, ServiceHA(), states=(other, WX))
    assert w.entity() == "weather.forecast_home"  # HA's default wins over alphabetical order
    w.put_settings({"entity_id": "weather.aaa"})
    assert w.entity() == "weather.aaa"
    for bad in ({"entity_id": "light.x"}, {"entity_id": "weather.gone"}, {"frost_push": "yes"}, {"nope": 1}, []):
        with pytest.raises(WeatherError):
            w.put_settings(bad)
    w3, _ = make(tmp_path / "x", ServiceHA(), states=(other,))
    assert w3.entity() == "weather.aaa"  # the first one when there's no forecast_home


def test_cold_night_hint_mentions_heating_and_frost_push_once(tmp_path):
    cold = hourly(NOW, [8, 6, 4, 2, 1, 0.6, 1, 2, 2, 3, 3, 4, 5, 6, 7, 8, 9])
    heating = datetime(2026, 10, 10, 6, 30, tzinfo=LONDON).timestamp()
    sent = []
    w, clock = make(tmp_path, ServiceHA(cold, []), heating=heating, notified=sent)
    r = run(w.get())
    assert r["tonight"]["text"] == "Cold night ahead (1°) — heating comes on at 06:30"
    assert run(w.tick()) is None  # frost push is off by default
    w.put_settings({"frost_push": True})
    assert run(w.tick()) is None and not sent  # 16:00: too early
    clock["t"] = NOW + 3600
    p = run(w.tick())
    assert p["title"] == "Frost tonight" and sent == [(p, "weather")]
    assert p["body"] == "Low of 1° tonight. Heating comes on at 06:30."
    assert run(w.tick()) is None and len(sent) == 1


# ---------------- API ----------------
class WeatherFakeHA(FakeHA):
    def __init__(self):
        super().__init__()
        self.forecast_status = 200

    def handler(self, request):
        if request.url.path == "/api/states":
            self.calls.append((request.method, request.url.path, None))
            return httpx.Response(200, json=STATES + [WX])
        if request.url.path == "/api/services/weather/get_forecasts":
            body = json.loads(request.content)
            self.calls.append((request.method, request.url.path, body))
            assert "return_response" in request.url.params
            if self.forecast_status != 200:
                return httpx.Response(self.forecast_status)
            now = datetime.now(timezone.utc).timestamp()
            fc = hourly(now, [3, 2, 1, 0.4, 0, 1, 2] * 4) if body["type"] == "hourly" else \
                daily(datetime.now(LONDON).date(), [("snowy", 4, -1)] * 6)
            return httpx.Response(200, json={"changed_states": [], "service_response": {body["entity_id"]: {"forecast": fc}}})
        return super().handler(request)


@pytest.fixture
def fake_ha():
    return WeatherFakeHA()


def test_api_weather_and_settings(client, fake_ha):
    r = client.get("/api/weather").json()
    assert r["available"] and r["current"]["text"] == "Partly cloudy" and r["tonight"]["cold"]
    assert r["entities"] == [{"entity_id": "weather.forecast_home", "name": "Forecast Home"}]
    n = len([c for c in fake_ha.calls if c[1].endswith("get_forecasts")])
    client.get("/api/weather")
    assert len([c for c in fake_ha.calls if c[1].endswith("get_forecasts")]) == n == 2  # cached
    fake_ha.forecast_status = 500
    r = client.post("/api/weather/refresh").json()
    assert r["available"] and r["hourly"] == [] and r["forecast_error"]
    s = client.put("/api/weather/settings", json={"frost_push": True}).json()
    assert s["settings"]["frost_push"] is True
    assert client.put("/api/weather/settings", json={"entity_id": "weather.nope"}).status_code == 400
    # forecast fetches never land in the activity log
    assert not [e for e in client.get("/api/activity").json()["entries"] if "weather" in (e["entity_id"] or "")]
