"""Wall tablet mode, against the real app and the fake Home Assistant (fixtures in conftest.py)."""
import json
import re

import pytest
from playwright.sync_api import expect

from conftest import PASSWORD, USER, hold, marker, marker_center, shot, toggles

SIZES = ["tablet", "portrait", "phone"]


@pytest.fixture
def new_page(stack, ha, open_page):
    """Signed in, wall-test layout saved, not navigated yet."""
    return lambda size="tablet", clock=None, **kw: open_page(stack, size, goto=None, clock=clock, **kw)


def set_settings(page, **vals):
    hold(page, page.locator("#wallClock"))
    expect(page.locator("#wallPop")).to_be_visible()
    page.click("#wallSettings")
    for k, v in vals.items():
        page.fill(f"#wallForm [name={k}]", str(v))
    page.click("#wallForm button[value=ok]")
    expect(page.locator("#wallDialog")).not_to_be_visible()


# ---------------------------------------------------------------------------------------------------------------------

def test_enter_from_menu_and_exit(new_page, stack, ha):
    page = new_page()
    page.goto(stack.url + "/")
    expect(marker(page, "light.lounge")).to_be_visible()
    r_normal = float(marker(page, "light.lounge").locator("circle").get_attribute("r"))
    page.click("#moreBtn"); page.click("#wallBtn")
    expect(page.locator("body")).to_have_class(re.compile(r"\bwall\b"))
    assert page.url.endswith("/?wall")
    expect(page.locator("header")).to_be_hidden()
    expect(page.locator("#side")).to_be_hidden()
    expect(page.locator("#wallTime")).to_have_text(re.compile(r"^\d\d:\d\d$"))
    expect(page.locator("#wallDate")).to_have_text(re.compile(r"\w+day \d+ \w+"))
    expect(page.locator("#wallPower")).to_be_visible()
    expect(page.locator("#wallPower b")).to_have_text("86.4 W")
    expect(page.locator("#wallTemp b")).to_have_text("19.3°")  # (20.5 + 18.0) / 2
    r_wall = float(marker(page, "light.lounge").locator("circle").get_attribute("r"))
    assert abs(r_wall / r_normal - 1.5) < 1e-6
    # Escape on desktop exits and restores the normal UI and URL.
    page.keyboard.press("Escape")
    expect(page.locator("body")).not_to_have_class(re.compile(r"\bwall\b"))
    assert page.url == stack.url + "/"
    expect(page.locator("header")).to_be_visible()
    expect(page.locator("#side")).to_be_visible()
    assert float(marker(page, "light.lounge").locator("circle").get_attribute("r")) == r_normal
    assert not page.errors


@pytest.mark.parametrize("size", SIZES)
def test_wall_url_tap_longpress_and_screens(new_page, stack, ha, size):
    page = new_page(size)
    page.goto(stack.url + "/?wall")
    expect(page.locator("body")).to_have_class(re.compile(r"\bwall\b"))
    expect(page.locator("header")).to_be_hidden()
    expect(marker(page, "light.kitchen")).to_be_visible()
    # Everything fits: no scrolling, the plan sits below the bar.
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight")
    shot(page, f"wall-{size}")
    # Tap toggles the light through HA.
    x, y = marker_center(page, "light.kitchen")
    (page.touchscreen.tap if size != "tablet" else page.mouse.click)(x, y)
    ha.wait_call(lambda c: c["service"] == "toggle" and c["data"]["entity_id"] == "light.kitchen")
    # Long-press opens the sheet instead of toggling.
    n = len(toggles(ha, "light.lounge"))
    hold(page, marker(page, "light.lounge").locator("circle"), 700)
    expect(page.locator("#sheet")).to_be_visible()
    expect(page.locator("#sheetContent h3")).to_have_text("Lounge lamp")
    shot(page, f"wall-{size}-sheet")
    assert len(toggles(ha, "light.lounge")) == n
    page.click("#sheetClose")
    # Dim with a short idle timeout set through the settings dialog.
    set_settings(page, idle=3)
    expect(page.locator("#wallDim")).to_be_visible(timeout=6000)
    expect(page.locator("#wallDimTime")).to_have_text(re.compile(r"^\d\d:\d\d$"))
    expect(page.locator("#wallDimInfo")).to_contain_text("86.4 W")
    expect(page.locator("#wallPower")).to_be_visible()
    page.wait_for_timeout(900)  # fade-in
    shot(page, f"wall-{size}-dim")
    # The waking touch lands on a light but must not toggle it.
    before = len(toggles(ha, "light.kitchen"))
    x, y = marker_center(page, "light.kitchen")
    (page.touchscreen.tap if size != "tablet" else page.mouse.click)(x, y)
    expect(page.locator("#wallDim")).to_be_hidden()
    page.wait_for_timeout(700)
    assert len(toggles(ha, "light.kitchen")) == before
    # ... the next one does.
    (page.touchscreen.tap if size != "tablet" else page.mouse.click)(x, y)
    ha.wait_call(lambda c: len(toggles(ha, "light.kitchen")) > before)
    assert not page.errors


def test_away_home_and_all_off(new_page, stack, ha):
    page = new_page()
    page.goto(stack.url + "/?wall")
    expect(page.locator("#wallAway .st")).to_have_text("Home")
    page.click("#wallAway")
    expect(page.locator("#modeDialog")).to_be_visible()
    shot(page, "wall-tablet-away-confirm")
    page.click("#modeOk")
    ha.wait_call(lambda c: c["service"] == "set_temperature")
    ha.wait_call(lambda c: c["service"] == "turn_off")
    expect(page.locator("#wallAway .st")).to_have_text("Away", timeout=3000)
    expect(page.locator("#wallAway")).to_have_class(re.compile("away"))
    shot(page, "wall-tablet-away")
    # Back home, then All off with its confirmation.
    page.click("#wallAway"); page.click("#modeOk")
    expect(page.locator("#wallAway .st")).to_have_text("Home", timeout=3000)
    ha.reset(); page.wait_for_timeout(500)
    asked = []
    page.once("dialog", lambda d: (asked.append(d.message), d.accept()))
    page.click("#wallAllOff")
    calls = ha.wait_call(lambda c: c["service"] == "turn_off")
    assert asked and asked[0].startswith("Turn off 3 devices")
    off = {e for c in calls for e in c["data"]["entity_id"]}
    assert {"light.lounge", "light.strip", "switch.tv"} <= off
    assert not page.errors


def test_exit_via_clock_hold(new_page, stack, ha):
    page = new_page("portrait")
    page.goto(stack.url + "/?wall")
    hold(page, page.locator("#wallClock"))
    expect(page.locator("#wallExit")).to_be_visible()
    shot(page, "wall-portrait-exit-pop")
    page.click("#wallExit")
    expect(page.locator("body")).not_to_have_class(re.compile(r"\bwall\b"))
    expect(page.locator("header")).to_be_visible()
    assert "wall" not in page.url
    assert page.evaluate("localStorage.getItem('hc.wall.on')") is None


def test_settings_persist_in_local_storage(new_page, stack, ha):
    page = new_page()
    page.goto(stack.url + "/?wall")
    set_settings(page, start="22:30", end="06:45", idle=600, nightIdle=20)
    stored = json.loads(page.evaluate("localStorage.getItem('hc.wall.settings')"))
    assert stored == {"start": "22:30", "end": "06:45", "idle": 600, "nightIdle": 20}
    page.reload()
    hold(page, page.locator("#wallClock")); page.click("#wallSettings")
    expect(page.locator("#wallForm [name=start]")).to_have_value("22:30")
    shot(page, "wall-tablet-settings")


def test_night_dimming(new_page, stack, ha):
    # Local (London) 22:59:20; idle timeout stays at its 2 min default.
    page = new_page(clock="2026-10-09T22:59:20+01:00")
    page.goto(stack.url + "/?wall")
    expect(page.locator("#wallTime")).to_have_text("22:59")
    page.clock.run_for(20_000)
    expect(page.locator("#wallDim")).to_be_hidden()          # 22:59:40, not night yet
    page.clock.run_for(32_000)                                # 23:00:12, idle 52 s >= 30 s at night
    expect(page.locator("#wallDim")).to_be_visible()
    expect(page.locator("#wallDimTime")).to_have_text("23:00")
    pos1 = page.locator("#wallDimIn").evaluate("e => e.style.transform")
    page.locator("#wallDim").click()
    expect(page.locator("#wallDim")).to_be_hidden()
    page.clock.run_for(20_000)
    expect(page.locator("#wallDim")).to_be_hidden()
    page.clock.run_for(12_000)                                # 30 s after the wake touch
    expect(page.locator("#wallDim")).to_be_visible()
    # Burn-in shift: position changes as the minutes pass.
    seen = {pos1}
    for _ in range(4):
        page.clock.run_for(60_000)
        seen.add(page.locator("#wallDimIn").evaluate("e => e.style.transform"))
    assert len(seen) >= 3
    # Morning: after 07:00 it no longer dims on its own after 30 s.
    page.clock.set_system_time("2026-10-10T07:05:00+01:00")
    page.locator("#wallDim").click()
    page.clock.run_for(40_000)
    expect(page.locator("#wallDim")).to_be_hidden()
    assert not page.errors


def test_daily_reload_during_dim(new_page, stack, ha):
    page = new_page(clock="2026-10-09T12:00:00+01:00")
    page.goto(stack.url + "/?wall")
    expect(page.locator("#wallTime")).to_have_text("12:00")
    page.evaluate("window.__marker = 1")
    page.clock.run_for(130_000)  # dims after 2 min, but the page is young: no reload
    expect(page.locator("#wallDim")).to_be_visible()
    assert page.evaluate("window.__marker") == 1
    page.locator("#wallDim").click()
    page.clock.fast_forward("25:00:00")
    page.clock.run_for(130_000)
    page.wait_for_function("window.__marker === undefined", timeout=10000)
    expect(page.locator("body")).to_have_class(re.compile(r"\bwall\b"))
    assert "?wall" in page.url


def test_offline_reload_from_service_worker(new_page, stack, ha):
    page = new_page(service_workers="allow")
    page.goto(stack.url + "/?wall")
    page.evaluate("navigator.serviceWorker.ready")
    page.reload()  # now controlled: the SW caches the page and the API responses
    page.wait_for_function("!!navigator.serviceWorker.controller")
    expect(marker(page, "light.lounge")).to_be_visible()
    page.wait_for_timeout(500)
    page.context.set_offline(True)
    page.reload()
    expect(page.locator("body")).to_have_class(re.compile(r"\bwall\b"))
    expect(marker(page, "light.lounge")).to_be_visible()
    expect(page.locator("#wallConn")).to_have_text("Offline")
    shot(page, "wall-tablet-offline")
    page.context.set_offline(False)


def test_signed_out_wall_tablet_returns_to_wall(new_page, stack, ha):
    page = new_page()
    page.goto(stack.url + "/?wall")
    expect(page.locator("body")).to_have_class(re.compile(r"\bwall\b"))
    page.context.clear_cookies()
    page.goto(stack.url + "/login.html")
    page.fill("[name=username]", USER); page.fill("[name=password]", PASSWORD); page.click("button[type=submit]")
    page.wait_for_url(re.compile(r"/\?wall$"))
    expect(page.locator("body")).to_have_class(re.compile(r"\bwall\b"))
