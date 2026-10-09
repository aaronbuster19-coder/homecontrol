"""Live updates over HA's websocket -> server -> SSE -> page: no polling, power and battery on the plan."""
import re

import pytest
from playwright.sync_api import expect

from conftest import marker, shot


def watch_polls(page):
    """Requests that would mean the page polls instead of getting pushed updates."""
    seen = []
    page.on("request", lambda r: seen.append(r.url) if re.search(r"/api/(devices|layout)$", r.url) else None)
    return seen


@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_state_change_in_ha_shows_up_without_polling(stack, ha, open_page, size):
    page = open_page(stack, size)
    expect(page.locator("#status")).to_contain_text("● live")
    expect(page.locator("#status")).to_have_class(re.compile(r"\blive\b"))
    circle = marker(page, "light.kitchen").locator("circle").first
    expect(circle).to_have_attribute("fill", "var(--off)")
    polls = watch_polls(page)
    ha.set("light.kitchen", "on")  # e.g. the wall switch
    expect(circle).to_have_attribute("fill", "var(--on)", timeout=3000)
    expect(page.locator('#list li[data-dev="light.kitchen"] .val')).to_have_text("on")
    ha.set("binary_sensor.contact_sensor_door", "on")
    expect(marker(page, "binary_sensor.contact_sensor_door").locator("circle").first).to_have_attribute("fill", "var(--open)")
    # The linked door on the plan turns red too.
    expect(page.locator('.opening[data-open="o1"] .swing')).to_have_attribute("style", re.compile(r"var\(--open\)"))
    ha.set("light.kitchen", "off")
    expect(circle).to_have_attribute("fill", "var(--off)", timeout=3000)
    assert polls == [], f"page polled: {polls}"
    shot(page, f"live-{size}")


def test_plug_watts_and_total_power(stack, ha, open_page):
    page = open_page(stack)
    tv = marker(page, "switch.tv")
    expect(tv.locator("text")).to_have_text("86.4 W")
    expect(page.locator("#status")).to_contain_text("⚡ 86.4 W")
    expect(page.locator('#list li[data-dev="switch.tv"] .val')).to_have_text("on · 86.4 W")
    expect(marker(page, "switch.kettle").locator("text")).to_have_count(0)  # off: no watts
    ha.set("sensor.tv_power", "120.4")
    expect(tv.locator("text")).to_have_text("120 W")
    ha.set("switch.kettle", "on")
    ha.set("sensor.kettle_power", "1980")
    expect(marker(page, "switch.kettle").locator("text")).to_have_text("1980 W")
    expect(page.locator("#status")).to_contain_text("⚡ 2100 W")
    ha.set("switch.tv", "off")
    expect(tv.locator("text")).to_have_count(0)


def test_battery_dot(stack, ha, open_page):
    page = open_page(stack)
    door = marker(page, "binary_sensor.contact_sensor_door")
    expect(door.locator("circle.batwarn")).to_have_count(1)  # 15 %
    expect(marker(page, "climate.lounge_valve").locator("circle.batwarn")).to_have_count(0)  # 80 %
    expect(page.locator('#list li[data-dev="binary_sensor.contact_sensor_door"] .val')).to_have_text("closed · 🔋 15 %")
    ha.set("sensor.front_door_battery", "60")
    expect(door.locator("circle.batwarn")).to_have_count(0)
    ha.set("sensor.lounge_valve_battery", "12")
    expect(marker(page, "climate.lounge_valve").locator("circle.batwarn")).to_have_count(1)
    marker(page, "climate.lounge_valve").locator("circle").first.click()
    expect(page.locator("#sheetContent .sub.warn")).to_have_text("🔋 Battery 12 % — low")
