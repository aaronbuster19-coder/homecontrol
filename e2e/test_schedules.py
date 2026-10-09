"""Browser tests for Schedules (⋯ → Schedules) and quiet hours (bell sheet), against the real app with a movable server
clock (e2e/clock_app.py) and the fake Home Assistant (e2e/fake_ha.py).

    pip install -r requirements-dev.txt playwright
    python -m pytest -q e2e/test_schedules.py    # SHOTS=dir to keep screenshots (default: e2e/screenshots)
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOTS = Path(os.environ.get("SHOTS", ROOT / "e2e" / "screenshots"))
USER, PASSWORD = "me", "pw-for-tests"
LONDON = ZoneInfo("Europe/London")
SIZES = {"desktop": (1280, 800), "phone": (390, 844)}
LAYOUT = {
    "unit": "m",
    "rooms": [{"id": "lounge", "name": "Lounge", "x": 0, "y": 0, "w": 5.2, "h": 4.2},
              {"id": "kitchen", "name": "Kitchen", "x": 5.2, "y": 0, "w": 3.2, "h": 2.6},
              {"id": "bed", "name": "Bedroom", "x": 0, "y": 4.2, "w": 4.4, "h": 3.4}],
    "placements": [{"entity_id": "light.lounge", "x": 2.4, "y": 2.0}, {"entity_id": "light.strip", "x": 1.0, "y": 3.5},
                   {"entity_id": "light.kitchen", "x": 6.8, "y": 1.3}, {"entity_id": "light.bedroom", "x": 2.2, "y": 5.9},
                   {"entity_id": "climate.bedroom_valve", "x": 0.5, "y": 7.0}],
    "openings": [],
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


@pytest.fixture(scope="module")
def servers(tmp_path_factory):
    ha_port, app_port = free_port(), free_port()
    ha = subprocess.Popen([sys.executable, str(ROOT / "e2e" / "fake_ha.py"), str(ha_port)], cwd=ROOT)
    wait_http(f"http://127.0.0.1:{ha_port}/_calls")
    env = {**os.environ, "HA_URL": f"http://127.0.0.1:{ha_port}", "HA_TOKEN": "test-token", "APP_USER": USER,
           "APP_PASSWORD": PASSWORD, "DB_PATH": str(tmp_path_factory.mktemp("db") / "layout.db"), "TZ_NAME": "Europe/London"}
    app = subprocess.Popen([sys.executable, "-m", "uvicorn", "--app-dir", str(ROOT / "e2e"), "clock_app:create", "--factory",
                            "--host", "127.0.0.1", "--port", str(app_port), "--log-level", "warning"], cwd=ROOT, env=env)
    wait_http(f"http://127.0.0.1:{app_port}/healthz")
    yield {"ha": f"http://127.0.0.1:{ha_port}", "app": f"http://localhost:{app_port}"}
    for proc in (app, ha):
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        exe = "/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").is_file() else None
        b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        yield b
        b.close()


class HA:
    def __init__(self, base):
        self.base = base

    def calls(self):
        return json.load(urllib.request.urlopen(f"{self.base}/_calls"))

    def reset(self):
        urllib.request.urlopen(urllib.request.Request(f"{self.base}/_reset", method="POST"))


@pytest.fixture
def ha(servers):
    h = HA(servers["ha"]); h.reset()
    return h


def new_page(browser, servers, size):
    w, h = SIZES[size]
    ctx = browser.new_context(viewport={"width": w, "height": h}, timezone_id="Europe/London", locale="en-GB",
                              has_touch=size == "phone", is_mobile=size == "phone")
    page = ctx.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.goto(servers["app"] + "/login.html")
    page.fill("[name=username]", USER)
    page.fill("[name=password]", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(re.compile(r"/(\?.*)?$"))
    r = page.request.put(servers["app"] + "/api/layout", data=json.dumps(LAYOUT), headers={"Content-Type": "application/json"})
    assert r.ok, r.text()
    page.goto(servers["app"] + "/")
    expect(page.locator('#plan .marker[data-dev="light.bedroom"]')).to_be_visible()
    return page


def set_clock(page, servers, t):
    r = page.request.post(servers["app"] + "/_test/clock", data=json.dumps({"t": t}), headers={"Content-Type": "application/json"})
    assert r.ok, r.text()


def shot(page, name):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(250)
    page.screenshot(path=str(SHOTS / f"{name}.png"))


def tap(page, sel, size):
    loc = page.locator(sel)
    loc.tap() if size == "phone" else loc.click()


@pytest.mark.parametrize("size", list(SIZES))
def test_schedule_create_edit_toggle_run_delete(browser, servers, ha, size):
    page = new_page(browser, servers, size)
    set_clock(page, servers, datetime(2026, 10, 12, 5, 0, tzinfo=LONDON).timestamp())   # Monday 05:00
    page.click("#moreBtn"); page.click("#schedulesBtn")
    expect(page.locator("#schedSheet")).to_be_visible()
    expect(page.locator("#schedMaster")).to_be_checked()
    expect(page.locator("#schedList")).to_contain_text("No schedules yet")

    # ---- create: Bedroom lights 40 %, weekdays 06:30 ----
    tap(page, "#schedAdd", size)
    expect(page.locator("#schedEdit")).to_be_visible()
    page.fill("#schedForm [name=name]", "Wake up")
    page.select_option("#schedForm [name=action]", "brightness")
    page.fill("#schedForm [name=value]", "40")
    page.select_option("#schedForm [name=room]", "bed")
    expect(page.locator("#schedForm input[name=ttype][value=room]")).to_be_checked()
    tap(page, ".sched-presets .chip >> text=Weekdays", size)
    expect(page.locator(".sched-days .chip[aria-pressed=true]")).to_have_count(5)
    page.fill("#schedForm [name=at]", "06:30")
    shot(page, f"sched-{size}-editor")
    page.locator("#schedEdit .sheet-body").evaluate("e => e.scrollTop = e.scrollHeight")
    tap(page, "#schedSave", size)
    expect(page.locator("#schedEdit")).to_be_hidden()
    item = page.locator(".sched-item").first
    expect(item).to_contain_text("Wake up")
    expect(item).to_contain_text("Weekdays · 06:30 · Bedroom lights 40 %")
    expect(item).to_contain_text("Next: Mon 06:30")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    shot(page, f"sched-{size}-list")

    # ---- validation in the editor: no days ----
    tap(page, ".sched-item .sched-main", size)
    for day in page.locator(".sched-days .chip[aria-pressed=true]").evaluate_all("cs => cs.map(c => c.dataset.day)"):
        page.click(f'.sched-days .chip[data-day="{day}"]')
    tap(page, "#schedSave", size)
    expect(page.locator("#schedEditMsg")).to_have_text("Pick at least one day.")
    # ---- edit: every day 06:45 ----
    tap(page, ".sched-presets .chip >> text=Every day", size)
    page.fill("#schedForm [name=at]", "06:45")
    tap(page, "#schedSave", size)
    expect(item).to_contain_text("Every day · 06:45 · Bedroom lights 40 %")

    # ---- toggle off and on ----
    sw = item.locator(".sched-switch input")
    sw.click()
    expect(item).to_have_class(re.compile(r"\bdisabled\b"))
    expect(item).to_contain_text("Off")
    assert page.request.get(servers["app"] + "/api/schedules").json()["schedules"][0]["enabled"] is False
    sw.click()
    expect(item).to_contain_text("Next: Mon 06:45")

    # ---- fast-forward the server clock: the fake HA gets exactly one call ----
    due = page.request.get(servers["app"] + "/api/schedules").json()["schedules"][0]["next"] / 1000
    set_clock(page, servers, due - 2)
    end = time.time() + 15
    while time.time() < end and not [c for c in ha.calls() if c["domain"] == "light"]:
        time.sleep(0.2)
    calls = [c for c in ha.calls() if c["domain"] == "light"]
    assert len(calls) == 1, calls
    assert calls[0]["service"] == "turn_on" and calls[0]["data"] == {"entity_id": ["light.bedroom"], "brightness_pct": 40}
    time.sleep(2)
    assert len([c for c in ha.calls() if c["domain"] == "light"]) == 1   # never repeated
    tap(page, ".sched-sheet:not([hidden]) .close", size)
    page.click("#moreBtn"); page.click("#schedulesBtn")
    expect(item).to_contain_text("Last ran: Mon 06:45 — 1 light at 40 %")
    expect(item).to_contain_text("Next: Tue 06:45")
    shot(page, f"sched-{size}-ran")

    # ---- delete ----
    tap(page, ".sched-item .sched-main", size)
    page.once("dialog", lambda d: d.accept())
    tap(page, "#schedDelete", size)
    expect(page.locator("#schedList")).to_contain_text("No schedules yet")
    assert page.request.get(servers["app"] + "/api/schedules").json()["schedules"] == []
    assert not page.errors


def test_master_switch_and_valve_schedule(browser, servers, ha):
    page = new_page(browser, servers, "desktop")
    set_clock(page, servers, datetime(2026, 10, 13, 5, 0, tzinfo=LONDON).timestamp())
    page.click("#moreBtn"); page.click("#schedulesBtn")
    page.click("#schedAdd")
    page.fill("#schedForm [name=name]", "Warm up")
    page.select_option("#schedForm [name=action]", "temperature")
    expect(page.locator("#schedForm label:has(input[value=room])")).to_be_hidden()   # rooms are for lights
    page.fill("#schedForm [name=value]", "20.5")
    page.locator("#schedDevs label", has_text="Bedroom radiator").locator("input").check()
    page.select_option("#schedForm [name=ttype_time]", "sunrise")
    page.fill("#schedForm [name=offset]", "-30")
    page.click("#schedSave")
    item = page.locator(".sched-item").first
    expect(item).to_contain_text("Every day · Sunrise −30 min · Bedroom radiator 20.5°")
    # master switch off: nothing runs at the time
    page.click("#schedMaster")
    expect(item).to_contain_text("Schedules are off")
    shot(page, "sched-desktop-master-off")
    nxt = page.request.get(servers["app"] + "/api/schedules").json()
    assert nxt["enabled"] is False and nxt["schedules"][0]["next"] is None
    page.click("#schedMaster")
    due = page.request.get(servers["app"] + "/api/schedules").json()["schedules"][0]["next"] / 1000
    page.click("#schedMaster")   # off again, then jump past the time
    set_clock(page, servers, due + 5)
    time.sleep(3)
    assert [c for c in ha.calls() if c["domain"] == "climate"] == []
    page.click("#schedMaster")   # back on after the time: no catch-up
    time.sleep(2)
    assert [c for c in ha.calls() if c["domain"] == "climate"] == []
    page.once("dialog", lambda d: d.accept())
    page.click(".sched-item .sched-main"); page.click("#schedDelete")
    expect(page.locator("#schedList")).to_contain_text("No schedules yet")
    assert not page.errors


@pytest.mark.parametrize("size", list(SIZES))
def test_quiet_hours_ui(browser, servers, ha, size):
    page = new_page(browser, servers, size)
    set_clock(page, servers, datetime(2026, 10, 14, 2, 0, tzinfo=LONDON).timestamp())   # 02:00: quiet hours
    tap(page, "#alertsBtn", size)
    sec = page.locator("#quietSec")
    sec.scroll_into_view_if_needed()
    expect(page.locator("#quietOn")).to_be_checked()
    expect(page.locator("#quietFrom")).to_have_value("23:00")
    expect(page.locator("#quietTo")).to_have_value("07:00")
    expect(page.locator("#quietText")).to_have_text(re.compile(r"Quiet hours now, until 07:00\."))
    tap(page, "#mute1h", size)
    expect(page.locator("#quietText")).to_have_text(re.compile(r"Muted until( \w{3})? 03:00\."))
    expect(page.locator("#muteCancel")).to_be_visible()
    shot(page, f"quiet-{size}")
    tap(page, "#muteCancel", size)
    expect(page.locator("#muteCancel")).to_be_hidden()
    expect(page.locator("#quietText")).to_have_text(re.compile(r"Quiet hours now"))
    page.fill("#quietFrom", "22:30")
    page.locator("#quietFrom").dispatch_event("change")
    expect(page.locator("#alertMsg")).to_have_text("Saved.")
    s = page.request.get(servers["app"] + "/api/alerts/settings").json()
    assert s["quiet_from"] == "22:30" and s["quiet_hours"] is True and s["mute_until"] is None
    tap(page, "#muteMorning", size)
    expect(page.locator("#quietText")).to_have_text(re.compile(r"Muted until( \w{3})? 07:00\."))
    tap(page, "#muteCancel", size)
    page.request.put(servers["app"] + "/api/alerts/settings", data=json.dumps({"quiet_from": "23:00"}),
                     headers={"Content-Type": "application/json"})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not page.errors
