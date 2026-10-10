"""Climate sheet (⋯ → Climate…): smart preheat per room and damp / mould warnings (backend/climate.py), on a fake HA
with a movable server clock (`clock_stack`, e2e/clock_app.py) — desktop and a 390px touch phone, dark and light.

    python -m pytest -q e2e/test_climate.py      # SHOTS=dir to keep screenshots
"""
import json
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from conftest import LAYOUT, WAIT, shot

LONDON = ZoneInfo("Europe/London")
JSON = {"Content-Type": "application/json"}
BED_VALVE, DEHUM = "climate.bedroom_valve", "humidifier.dehumidifier"
# the dehumidifier stands in the bedroom: its humidity is the bedroom's
CLIMATE_LAYOUT = {**LAYOUT, "furniture": [],
                  "placements": LAYOUT["placements"] + [{"entity_id": DEHUM, "x": 3.6, "y": 5.0}]}


def tomorrow(h, m=0):
    d = datetime.now(LONDON).date() + timedelta(days=1)
    return datetime.combine(d, dtime(h, m), tzinfo=LONDON).timestamp()


def tap(page, sel):
    (page.tap if page.size in ("phone", "mobile") else page.click)(sel)


def no_hscroll(page):
    return page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def climate(page, stack):
    r = page.request.get(stack.url + "/api/climate")
    assert r.ok, r.text()
    return r.json()


def put(page, stack, path, body):
    r = page.request.put(stack.url + path, data=json.dumps(body), headers=JSON)
    assert r.ok, r.text()
    return r.json()


def wait_for(fn, what, timeout=WAIT):
    end = time.time() + timeout
    while True:
        got = fn()
        if got:
            return got
        if time.time() > end:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.1)


def open_climate(page):
    tap(page, "#moreBtn")
    expect(page.locator("#climateBtn")).to_be_visible()
    tap(page, "#climateBtn")
    expect(page.locator("#moreMenu")).to_be_hidden()
    sheet = page.locator("#climateSheet")
    expect(sheet).to_be_visible()
    expect(sheet.locator("#cmPreheatSec")).to_be_visible()
    return sheet


def temp_calls(ha):
    return [c for c in ha.calls() if c["service"] == "set_temperature"]


@pytest.mark.parametrize("size,scheme", [("desktop", "dark"), ("desktop", "light"), ("phone", "dark"), ("phone", "light")])
def test_sheet_opt_in_and_layout(stack, ha, open_page, size, scheme):
    page = open_page(stack, size=size, layout=CLIMATE_LAYOUT, color_scheme=scheme)
    put(page, stack, "/api/climate/settings", {"preheat_enabled": False, "damp_push": False})
    for room in ("lounge", "bed"):
        put(page, stack, f"/api/climate/rooms/{room}", {"preheat": False})
    sheet = open_climate(page)
    # everything off by default; rooms with a valve are listed, the others named once
    expect(sheet.locator("#cmPreheat")).not_to_be_checked()
    rooms = sheet.locator("#cmRooms > li")
    expect(rooms).to_have_count(2)
    expect(rooms.nth(0)).to_contain_text("Lounge")
    expect(rooms.nth(1)).to_contain_text("Bedroom")
    expect(sheet.locator("[data-preheat]:checked")).to_have_count(0)
    expect(sheet.locator("#cmPreheatSec")).to_contain_text("No radiator valve in: Kitchen, Hall, Bathroom.")
    expect(rooms.nth(1).locator(".cm-next")).to_contain_text("No heating schedule")
    # damp: the bedroom's humidity comes from the dehumidifier standing in it
    bed = sheet.locator('#cmDamp li[data-room="bed"]')
    expect(bed).to_contain_text("62 %")
    expect(bed).to_contain_text("18°")
    expect(bed).to_contain_text("Humidity from Dehumidifier")
    expect(bed).to_have_class("ok")
    expect(sheet.locator("#cmDampPush")).not_to_be_checked()

    # opt in: master switch, then the bedroom (learns its rate from HA history the first time)
    tap(page, "#cmPreheat")
    expect(sheet.locator("#cmPreheat")).to_be_checked()
    tap(page, '[data-preheat="bed"]')
    expect(sheet.locator('[data-preheat="bed"]')).to_be_checked()
    expect(page.locator("#status")).to_contain_text("Preheat on for Bedroom")
    expect(rooms.nth(1).locator(".cm-rate")).to_contain_text("°/h")
    expect(rooms.nth(1).locator('[data-learn="bed"]')).to_be_visible()
    st = climate(page, stack)
    assert st["settings"]["preheat_enabled"] and next(r for r in st["rooms"] if r["id"] == "bed")["preheat"]
    # threshold inputs save
    sheet.locator("#cmHum").fill("75")
    sheet.locator("#cmHum").press("Enter" if size == "desktop" else "Tab")
    wait_for(lambda: climate(page, stack)["settings"]["damp_humidity"] == 75, "damp_humidity saved")
    sheet.locator("#cmLead").select_option("90")
    wait_for(lambda: climate(page, stack)["settings"]["max_lead_min"] == 90, "max lead saved")

    # fits the screen: nothing sideways, the sheet within the viewport, nothing switched by any of this
    assert no_hscroll(page)
    body = sheet.locator(".sheet-body").bounding_box()
    vw = page.viewport_size["width"]
    assert body["x"] >= 0 and body["x"] + body["width"] <= vw + 0.5
    for sel in ("#cmLead", "#cmHum", "#cmTemp", "#cmMins", '[data-learn="bed"]'):
        b = sheet.locator(sel).bounding_box()
        assert b and b["x"] >= body["x"] and b["x"] + b["width"] <= body["x"] + body["width"] + 0.5, sel
    shot(page, f"climate-{size}-{scheme}")
    assert temp_calls(ha) == []
    # closes with × (and the scrim on desktop)
    tap(page, "#climateClose")
    expect(sheet).to_be_hidden()
    put(page, stack, "/api/climate/settings", {"preheat_enabled": False, "damp_humidity": 70, "max_lead_min": 120})


@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_preheat_starts_early_once(clock_stack, open_page, size):
    stack, ha = clock_stack, clock_stack.ha
    stack.set_clock(tomorrow(3, 0))  # far from 07:00: nothing due while we set things up
    page = open_page(stack, size=size, layout=CLIMATE_LAYOUT)
    r = page.request.post(stack.url + "/api/schedules", headers=JSON, data=json.dumps({
        "name": "Bedroom warm", "days": list(range(7)), "time": {"type": "fixed", "at": "07:00"},
        "action": {"type": "temperature", "value": 21}, "target": {"entity_ids": [BED_VALVE]}}))
    assert r.ok, r.text()
    sheet = open_climate(page)
    tap(page, "#cmPreheat")
    expect(sheet.locator("#cmPreheat")).to_be_checked()
    tap(page, '[data-preheat="bed"]')
    expect(sheet.locator('[data-preheat="bed"]')).to_be_checked()
    nxt = sheet.locator('#cmRooms li[data-room="bed"] .cm-next')
    expect(nxt).to_contain_text("“Bedroom warm” 07:00 → 21°")
    expect(nxt).to_contain_text("min early")
    page.wait_for_timeout(1500)
    assert temp_calls(ha) == []  # 4 h before: too early
    # 06:45: the bedroom (18°, set to 19°) needs to warm by 3° — at most 8°/h, so it's started by now
    stack.set_clock(tomorrow(6, 45))
    calls = wait_for(lambda: temp_calls(ha), "preheat call")
    assert [c["data"] for c in calls] == [{"entity_id": [BED_VALVE], "temperature": 21}]
    # never again for this occurrence, however often it checks
    stack.set_clock(tomorrow(6, 47))
    page.wait_for_timeout(2000)
    assert len(temp_calls(ha)) == 1
    tap(page, "#climateClose")
    sheet = open_climate(page)
    log = sheet.locator("#cmLog")
    expect(log).to_be_visible()
    tap(page, "#cmLog summary")
    expect(log).to_contain_text("Bedroom: Preheat started")
    expect(log).to_contain_text("“Bedroom warm” at 07:00")
    expect(sheet.locator('#cmRooms li[data-room="bed"] .cm-next')).to_contain_text("preheating since 06:45")
    shot(page, f"climate-preheat-{size}")
    assert no_hscroll(page)


@pytest.mark.parametrize("size,scheme", [("desktop", "light"), ("mobile", "dark"), ("mobile", "light")])
def test_damp_warning_and_dehumidifier(clock_stack, open_page, size, scheme):
    stack, ha = clock_stack, clock_stack.ha
    t0 = tomorrow(12, 0)  # midday: outside quiet hours
    stack.set_clock(t0)
    page = open_page(stack, size=size, layout=CLIMATE_LAYOUT, color_scheme=scheme)
    put(page, stack, "/api/climate/settings", {"damp_push": True, "damp_minutes": 15})
    ha.set(DEHUM, "off", current_humidity=82)
    ha.set(BED_VALVE, current_temperature=15.0)
    stack.set_clock(t0 + 60)
    bed = lambda: next(r for r in climate(page, stack)["rooms"] if r["id"] == "bed")["damp"]
    wait_for(lambda: bed()["at_risk"], "damp watch")
    sheet = open_climate(page)
    li = sheet.locator('#cmDamp li[data-room="bed"]')
    expect(li).to_have_class("watch")
    expect(li).to_contain_text("82 % · 15°")
    expect(li).to_contain_text("warning after 15 min")
    # 16 minutes later: sustained -> one push, the card turns red and offers the dehumidifier
    stack.set_clock(t0 + 17 * 60)
    wait_for(lambda: bed()["pushed"], "damp push")
    stack.set_clock(t0 + 20 * 60)
    page.wait_for_timeout(1500)
    log = climate(page, stack)["log"]
    assert [x["action"] for x in log].count("Damp warning sent") == 1
    assert "Run the Dehumidifier — it's off." in log[0]["note"]
    tap(page, "#climateClose")
    sheet = open_climate(page)
    li = sheet.locator('#cmDamp li[data-room="bed"]')
    expect(li).to_have_class("risk")
    expect(li.locator(".cm-state")).to_contain_text("Damp risk since")
    btn = li.locator("[data-dehum]")
    expect(btn).to_have_text("Dehumidifier (off) →")
    assert no_hscroll(page)
    shot(page, f"climate-damp-{size}-{scheme}")
    assert [c for c in ha.calls() if c["domain"] in ("humidifier", "switch")] == []  # a suggestion, never switched for you
    tap(page, "[data-dehum]")
    expect(page.locator("#climateSheet")).to_be_hidden()
    expect(page.locator("#sheet")).to_be_visible()
    expect(page.locator("#sheetContent h3")).to_have_text("Dehumidifier")


def test_push_link_opens_sheet(stack, ha, open_page):
    page = open_page(stack, size="phone", layout=CLIMATE_LAYOUT, goto="/?climate")
    expect(page.locator("#climateSheet")).to_be_visible()
    expect(page.locator("#cmDampSec")).to_be_visible()
    assert page.evaluate("location.search") == ""
