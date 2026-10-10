"""Outdoor weather: the status-line chip, the weather sheet (now, 12 h, 5 days), the cold-night hint with the heating
schedule, wall mode's weather panel and dim screen, settings and a failing forecast — desktop and a 390px touch phone."""
import json
import urllib.request

import pytest
from playwright.sync_api import expect

from conftest import WAIT, shot

JSON = {"Content-Type": "application/json"}


def forecast(stack, **body):
    req = urllib.request.Request(stack.ha.base + "/fake/forecast", data=json.dumps(body).encode(), method="POST", headers=JSON)
    urllib.request.urlopen(req, timeout=WAIT).read()


def refresh(page, stack):
    r = page.request.post(stack.url + "/api/weather/refresh")
    assert r.ok, r.text()
    return r.json()


def no_hscroll(page):
    return page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def tap(page, sel):
    (page.tap if page.size == "phone" else page.click)(sel)


@pytest.mark.parametrize("size", ["desktop", "phone"])
def test_chip_and_sheet(stack, ha, open_page, size):
    page = open_page(stack, size=size)
    refresh(page, stack)
    page.reload()
    chip = page.locator("#wxChip")
    expect(chip).to_be_visible()
    expect(chip).to_have_text("12°")
    expect(chip.locator("svg.wx-ic")).to_have_count(1)
    assert no_hscroll(page)
    tap(page, "#wxChip")
    box = page.locator("#weather")
    expect(box).to_be_visible()
    expect(box.locator("h3")).to_have_text("Weather")
    expect(box.locator(".wx-temp")).to_have_text("12°")
    expect(box.locator(".wx-cond")).to_have_text("Partly cloudy")
    facts = box.locator(".wx-facts")
    for t in ("Feels like 10°", "Humidity 71 %", "Wind 10 mph SW"):
        expect(facts).to_contain_text(t)
    expect(facts).to_contain_text("Today ")
    expect(box.locator(".wx-hour")).to_have_count(12)
    expect(box.locator(".wx-day")).to_have_count(5)
    expect(box.locator(".wx-day").first.locator(".wx-d-n")).to_have_text("Today")
    expect(box.locator(".wx-cold")).to_have_count(0)  # nights at 7°: no hint
    assert no_hscroll(page)
    shot(page, f"weather-{size}")
    calls = json.load(urllib.request.urlopen(stack.ha.base + "/fake/forecast_calls"))
    assert {c["data"]["type"] for c in calls} >= {"hourly", "daily"}
    assert all("return_response" in c["query"] and c["data"]["entity_id"] == "weather.forecast_home" for c in calls)


@pytest.mark.parametrize("size", ["desktop", "phone"])
def test_cold_night_hint_wall_panel_and_dim(stack, ha, open_page, size):
    page = open_page(stack, size=size)
    forecast(stack, cold=True)
    r = page.request.post(stack.url + "/api/schedules", headers=JSON, data=json.dumps({
        "name": "Morning warm", "days": [0, 1, 2, 3, 4, 5, 6], "time": {"type": "fixed", "at": "06:30"},
        "action": {"type": "temperature", "value": 20}, "target": {"entity_ids": ["climate.lounge_valve"]}}))
    assert r.ok, r.text()
    sid = r.json()["id"]
    try:
        w = refresh(page, stack)
        assert w["tonight"] == {"low": 1.0, "cold": True, "heating": "06:30",
                                "text": "Cold night ahead (1°) — heating comes on at 06:30"}
        page.reload()
        tap(page, "#wxChip")
        expect(page.locator("#weather .wx-cold")).to_have_text("Cold night ahead (1°) — heating comes on at 06:30")
        shot(page, f"weather-{size}-cold")
        page.click("#sheetClose")

        # Wall mode: the weather panel in the bar, and the outdoor temperature on the dim screen
        # Long idle while the panel is checked (a 3 s one could dim over it on a slow runner), then 3 s for the dim.
        wall = "localStorage.setItem('hc.wall.settings', JSON.stringify({start: '23:00', end: '07:00', idle: %d, nightIdle: %d}))"
        page.evaluate(wall % (3600, 3600))
        page.goto(stack.url + "/?wall")
        panel = page.locator("#wallWx")
        expect(panel).to_be_visible()
        expect(panel.locator("b")).to_have_text("12°")
        expect(panel.locator(".wx-cold-tag")).to_contain_text("Cold night ahead (1°)")
        assert no_hscroll(page)
        shot(page, f"weather-{size}-wall")
        panel.click()
        expect(page.locator("#weather")).to_be_visible()
        page.click("#sheetClose")
        expect(page.locator("#wallDim")).to_be_hidden()
        page.evaluate(wall % (3, 3))
        page.reload()
        expect(page.locator("#wallDim")).to_be_visible(timeout=WAIT * 1000)
        expect(page.locator("#wallDimInfo")).to_contain_text("12°")
        expect(page.locator("#wallDimInfo svg.wx-ic")).to_have_count(1)
        page.wait_for_timeout(900)  # fade-in
        shot(page, f"weather-{size}-dim")
    finally:
        page.request.delete(stack.url + f"/api/schedules/{sid}")
        page.evaluate("localStorage.removeItem('hc.wall.settings'); localStorage.removeItem('hc.wall.on')")


def test_settings_and_forecast_errors(stack, ha, open_page):
    page = open_page(stack)
    refresh(page, stack)
    page.reload()
    page.click("#moreBtn")
    page.click("#weatherBtn")
    det = page.locator("#weatherSettings")
    expect(det).to_have_attribute("open", "")
    expect(page.locator("#wxEntity")).to_have_value("weather.forecast_home")
    frost = page.locator("#wxFrost")
    expect(frost).not_to_be_checked()
    frost.check()
    expect(page.locator("#status")).to_have_text("Frost push on")
    assert page.request.get(stack.url + "/api/weather").json()["settings"]["frost_push"] is True
    page.locator("#wxFrost").uncheck()
    expect(page.locator("#status")).to_have_text("Frost push off")
    page.click("#sheetClose")
    assert page.request.put(stack.url + "/api/weather/settings", headers=JSON,
                            data=json.dumps({"entity_id": "weather.nope"})).status == 400

    # HA's forecast service fails: the current conditions still show, the forecast says why it's missing
    forecast(stack, error=500)
    w = refresh(page, stack)
    assert w["available"] and w["hourly"] == [] and w["forecast_error"]
    page.reload()
    expect(page.locator("#wxChip")).to_have_text("12°")
    page.click("#wxChip")
    expect(page.locator("#weather .wx-temp")).to_have_text("12°")
    expect(page.locator("#weather .hist-msg")).to_contain_text("Forecast not available")
    expect(page.locator("#weather .wx-hour")).to_have_count(0)
    forecast(stack)
    refresh(page, stack)
