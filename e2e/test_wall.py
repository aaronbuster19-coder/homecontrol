"""Browser tests for wall tablet mode, against the real app and a fake Home Assistant (e2e/fake_ha.py).

    pip install -r requirements-dev.txt playwright
    python -m pytest -q e2e        # SHOTS=dir to keep screenshots (default: e2e/screenshots, git-ignored)
Not run by the Docker test stage (needs a browser).
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOTS = Path(os.environ.get("SHOTS", ROOT / "e2e" / "screenshots"))
USER, PASSWORD = "me", "pw-for-tests"
SIZES = {"tablet": (1280, 800), "portrait": (768, 1024), "phone": (390, 844)}

LAYOUT = {
    "unit": "m",
    "rooms": [
        {"id": "lounge", "name": "Lounge", "x": 0, "y": 0, "w": 5.2, "h": 4.2},
        {"id": "kitchen", "name": "Kitchen", "x": 5.2, "y": 0, "w": 3.2, "h": 2.6},
        {"id": "hall", "name": "Hall", "x": 5.2, "y": 2.6, "w": 3.2, "h": 1.6},
        {"id": "bed", "name": "Bedroom", "x": 0, "y": 4.2, "w": 4.4, "h": 3.4},
        {"id": "bath", "name": "Bathroom", "x": 4.4, "y": 4.2, "w": 2.4, "h": 2.2},
    ],
    "placements": [
        {"entity_id": "light.lounge", "x": 2.4, "y": 2.0},
        {"entity_id": "light.strip", "x": 1.0, "y": 3.5},
        {"entity_id": "switch.tv", "x": 4.4, "y": 3.4},
        {"entity_id": "climate.lounge_valve", "x": 0.5, "y": 0.9},
        {"entity_id": "light.kitchen", "x": 6.8, "y": 1.3},
        {"entity_id": "switch.kettle", "x": 7.8, "y": 0.6},
        {"entity_id": "light.bedroom", "x": 2.2, "y": 5.9},
        {"entity_id": "climate.bedroom_valve", "x": 0.5, "y": 7.0},
        {"entity_id": "binary_sensor.contact_sensor_door", "x": 7.9, "y": 3.6},
    ],
    "openings": [
        {"id": "o1", "type": "door", "x": 8.4, "y": 3.0, "len": 0.9, "orient": "v",
         "entity_id": "binary_sensor.contact_sensor_door"},
        {"id": "o2", "type": "window", "x": 1.4, "y": 0, "len": 1.6, "orient": "h"},
    ],
}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except urllib.error.HTTPError:
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"{url} did not come up")


@pytest.fixture(scope="session")
def servers(tmp_path_factory):
    ha_port, app_port = free_port(), free_port()
    ha = subprocess.Popen([sys.executable, str(ROOT / "e2e" / "fake_ha.py"), str(ha_port)], cwd=ROOT)
    wait_http(f"http://127.0.0.1:{ha_port}/_calls")
    env = {**os.environ, "HA_URL": f"http://127.0.0.1:{ha_port}", "HA_TOKEN": "test-token", "APP_USER": USER,
           "APP_PASSWORD": PASSWORD, "DB_PATH": str(tmp_path_factory.mktemp("db") / "layout.db")}
    app = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.app:create_app", "--factory", "--host", "127.0.0.1",
                            "--port", str(app_port), "--log-level", "warning"], cwd=ROOT, env=env)
    wait_http(f"http://127.0.0.1:{app_port}/healthz")
    yield {"ha": f"http://127.0.0.1:{ha_port}", "app": f"http://localhost:{app_port}"}
    for proc in (app, ha):  # open SSE streams keep uvicorn from a graceful exit
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait()


class HA:
    def __init__(self, base):
        self.base = base

    def calls(self):
        return json.load(urllib.request.urlopen(f"{self.base}/_calls"))

    def reset(self):
        urllib.request.urlopen(urllib.request.Request(f"{self.base}/_reset", method="POST"))

    def wait_call(self, pred, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            hit = [c for c in self.calls() if pred(c)]
            if hit:
                return hit
            time.sleep(0.1)
        raise AssertionError(f"no matching call in {self.calls()}")


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        exe = "/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").is_file() else None
        b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def ha(servers):
    h = HA(servers["ha"]); h.reset()
    return h


def new_page(browser, servers, size="tablet", clock=None, **kw):
    w, h = SIZES[size]
    ctx = browser.new_context(viewport={"width": w, "height": h}, timezone_id="Europe/London", locale="en-GB",
                              has_touch=size != "tablet", **kw)
    page = ctx.new_page()
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.errors = []
    if clock:
        page.clock.install(time=clock)
    page.goto(servers["app"] + "/login.html")
    page.fill("[name=username]", USER)
    page.fill("[name=password]", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(re.compile(r"/(\?.*)?$"))
    r = page.request.put(servers["app"] + "/api/layout", data=json.dumps(LAYOUT), headers={"Content-Type": "application/json"})
    assert r.ok, r.text()
    return page


def shot(page, name):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(250)
    page.screenshot(path=str(SHOTS / f"{name}.png"))


def marker(page, eid):
    return page.locator(f'#plan .marker[data-dev="{eid}"]')


def marker_center(page, eid):
    b = marker(page, eid).locator("circle").bounding_box()
    return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2


def hold(page, locator, ms=1200):
    b = locator.bounding_box()
    page.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
    page.mouse.down(); page.wait_for_timeout(ms); page.mouse.up()


def toggles(ha, eid):
    return [c for c in ha.calls() if c["service"] in ("toggle", "turn_on", "turn_off") and eid in json.dumps(c["data"])]


def set_settings(page, **vals):
    hold(page, page.locator("#wallClock"))
    expect(page.locator("#wallPop")).to_be_visible()
    page.click("#wallSettings")
    for k, v in vals.items():
        page.fill(f"#wallForm [name={k}]", str(v))
    page.click("#wallForm button[value=ok]")
    expect(page.locator("#wallDialog")).not_to_be_visible()


# ---------------------------------------------------------------------------------------------------------------------

def test_enter_from_menu_and_exit(browser, servers, ha):
    page = new_page(browser, servers)
    page.goto(servers["app"] + "/")
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
    assert page.url == servers["app"] + "/"
    expect(page.locator("header")).to_be_visible()
    expect(page.locator("#side")).to_be_visible()
    assert float(marker(page, "light.lounge").locator("circle").get_attribute("r")) == r_normal
    assert not page.errors


@pytest.mark.parametrize("size", list(SIZES))
def test_wall_url_tap_longpress_and_screens(browser, servers, ha, size):
    page = new_page(browser, servers, size)
    page.goto(servers["app"] + "/?wall")
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


def test_away_home_and_all_off(browser, servers, ha):
    page = new_page(browser, servers)
    page.goto(servers["app"] + "/?wall")
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


def test_exit_via_clock_hold(browser, servers, ha):
    page = new_page(browser, servers, "portrait")
    page.goto(servers["app"] + "/?wall")
    hold(page, page.locator("#wallClock"))
    expect(page.locator("#wallExit")).to_be_visible()
    shot(page, "wall-portrait-exit-pop")
    page.click("#wallExit")
    expect(page.locator("body")).not_to_have_class(re.compile(r"\bwall\b"))
    expect(page.locator("header")).to_be_visible()
    assert "wall" not in page.url
    assert page.evaluate("localStorage.getItem('hc.wall.on')") is None


def test_settings_persist_in_local_storage(browser, servers, ha):
    page = new_page(browser, servers)
    page.goto(servers["app"] + "/?wall")
    set_settings(page, start="22:30", end="06:45", idle=600, nightIdle=20)
    stored = json.loads(page.evaluate("localStorage.getItem('hc.wall.settings')"))
    assert stored == {"start": "22:30", "end": "06:45", "idle": 600, "nightIdle": 20}
    page.reload()
    hold(page, page.locator("#wallClock")); page.click("#wallSettings")
    expect(page.locator("#wallForm [name=start]")).to_have_value("22:30")
    shot(page, "wall-tablet-settings")


def test_night_dimming(browser, servers, ha):
    # Local (London) 22:59:20; idle timeout stays at its 2 min default.
    page = new_page(browser, servers, clock="2026-10-09T22:59:20+01:00")
    page.goto(servers["app"] + "/?wall")
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


def test_daily_reload_during_dim(browser, servers, ha):
    page = new_page(browser, servers, clock="2026-10-09T12:00:00+01:00")
    page.goto(servers["app"] + "/?wall")
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


def test_offline_reload_from_service_worker(browser, servers, ha):
    page = new_page(browser, servers, service_workers="allow")
    page.goto(servers["app"] + "/?wall")
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


def test_signed_out_wall_tablet_returns_to_wall(browser, servers, ha):
    page = new_page(browser, servers)
    page.goto(servers["app"] + "/?wall")
    expect(page.locator("body")).to_have_class(re.compile(r"\bwall\b"))
    page.context.clear_cookies()
    page.goto(servers["app"] + "/login.html")
    page.fill("[name=username]", USER); page.fill("[name=password]", PASSWORD); page.click("button[type=submit]")
    page.wait_for_url(re.compile(r"/\?wall$"))
    expect(page.locator("body")).to_have_class(re.compile(r"\bwall\b"))
