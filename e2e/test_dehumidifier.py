"""Browser tests for the dehumidifier (marker, list group, sheet controls, tank banner, humidity labels, wall bar),
against the real app and e2e/fake_ha.py. Desktop (1280 px, mouse) and phone (390 px, touch).

    python -m pytest -q e2e/test_dehumidifier.py
"""
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from legacy import HA, LAYOUT, PASSWORD, ROOT, USER, free_port, marker, marker_center, new_page, shot, wait_http

DH = "humidifier.dehumidifier"
LAYOUT_DH = {**LAYOUT, "placements": LAYOUT["placements"] + [{"entity_id": DH, "x": 3.6, "y": 6.9}]}


# Module-scoped (not session) so they are gone before test_wall.py starts its own Playwright and servers:
# only one sync Playwright may run at a time.
@pytest.fixture(scope="module")
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


@pytest.fixture
def ha(servers):
    h = HA(servers["ha"]); h.reset()
    return h


def set_state(ha, eid, state=None, **attrs):
    body = {"entity_id": eid, "attributes": attrs, **({"state": state} if state else {})}
    urllib.request.urlopen(urllib.request.Request(f"{ha.base}/_state", data=json.dumps(body).encode(), method="POST",
                                                  headers={"Content-Type": "application/json"}))


def open_page(browser, servers, size):
    page = new_page(browser, servers, "tablet" if size == "desktop" else size)
    r = page.request.put(servers["app"] + "/api/layout", data=json.dumps(LAYOUT_DH), headers={"Content-Type": "application/json"})
    assert r.ok, r.text()
    page.goto(servers["app"] + "/")
    expect(marker(page, DH)).to_be_visible()
    return page


def tap(page, size, x, y):
    (page.mouse.click if size == "desktop" else page.touchscreen.tap)(x, y)


def calls(ha, service):
    return [c for c in ha.calls() if c["service"] == service and DH in json.dumps(c["data"])]


@pytest.mark.parametrize("size", ["desktop", "phone"])
def test_marker_list_sheet_controls(browser, servers, ha, size):
    page = open_page(browser, servers, size)
    circle = marker(page, DH).locator("circle")
    expect(circle).to_have_attribute("fill", "var(--dry)")                       # on + drying
    expect(marker(page, DH).locator("text")).to_have_text("62 %")
    expect(page.locator("#list li.group", has_text="Dehumidifier")).to_be_visible()
    row = page.locator(f'#list li[data-dev="{DH}"]')
    expect(row.locator(".val")).to_have_text("on · 62 % → 50 %")
    expect(page.locator(f'#list li[data-dev="switch.dehumidifier_child_lock"]')).to_have_count(0)
    # Humidity next to the bedroom's temperature label.
    hum = page.locator('#rooms [data-room="bed"] .hum-label')
    expect(hum).to_have_text("62 %")
    expect(page.locator('#rooms [data-room="bed"] .temp-label')).to_be_visible()
    t = page.locator('#rooms [data-room="bed"] .temp-label').bounding_box(); h = hum.bounding_box()
    assert h["x"] >= t["x"] + t["width"] - 1 or h["y"] > t["y"] + t["height"] - 1  # beside or below, never on top
    shot(page, f"dehum-{size}-plan")

    # Tap opens the sheet; it never switches anything.
    tap(page, size, *marker_center(page, DH))
    expect(page.locator("#sheet")).to_be_visible()
    expect(page.locator("#sheetContent h3")).to_have_text("Dehumidifier")
    page.wait_for_timeout(400)
    assert not calls(ha, "toggle")
    expect(page.locator("#sheetContent .dh-now")).to_have_text("Now 62 % · 21.5° · drying")
    expect(page.locator("#sheetContent .dh-target .target")).to_have_text("50 %")
    expect(page.locator(".tank-banner")).to_have_count(0)
    shot(page, f"dehum-{size}-sheet")

    # +/- in 5 % steps, sent once after the taps settle.
    plus, minus = page.get_by_label("Raise target humidity"), page.get_by_label("Lower target humidity")
    plus.click(); plus.click()
    expect(page.locator("#sheetContent .dh-target .target")).to_have_text("60 %")
    got = ha.wait_call(lambda c: c["service"] == "set_humidity")
    page.wait_for_timeout(300)
    assert [c["data"] for c in calls(ha, "set_humidity")] == [{"entity_id": DH, "humidity": 60}]
    assert got[0]["domain"] == "humidifier"
    for _ in range(8):
        minus.click()
    expect(page.locator("#sheetContent .dh-target .target")).to_have_text("30 %")  # clamped at min_humidity
    ha.wait_call(lambda c: c["service"] == "set_humidity" and c["data"]["humidity"] == 30)

    # Mode select.
    page.select_option("#sheetContent .dh-mode select", "sleep")
    m = ha.wait_call(lambda c: c["service"] == "set_mode")
    assert m[0]["domain"] == "humidifier" and m[0]["data"] == {"entity_id": DH, "mode": "sleep"}

    # On/off button.
    page.click("#sheetContent button.big")
    t = ha.wait_call(lambda c: c["service"] == "toggle")
    assert t[0]["domain"] == "humidifier" and t[0]["data"] == {"entity_id": DH}
    expect(page.locator("#sheetContent button.big")).to_have_text("Off — tap to turn on")
    expect(circle).to_have_attribute("fill", "var(--off)")
    assert not page.errors


def test_tank_full_banner_and_marker(browser, servers, ha):
    page = open_page(browser, servers, "phone")
    set_state(ha, "binary_sensor.dehumidifier_tank_full", "on")
    expect(marker(page, DH).locator("circle")).to_have_attribute("fill", "var(--open)")
    expect(page.locator(f'#list li[data-dev="{DH}"] .val')).to_have_text("tank full")
    page.touchscreen.tap(*marker_center(page, DH))
    expect(page.locator(".tank-banner")).to_contain_text("Tank full — empty it")
    shot(page, "dehum-phone-tank-full")
    set_state(ha, "binary_sensor.dehumidifier_tank_full", "off")
    expect(page.locator(".tank-banner")).to_have_count(0)
    expect(marker(page, DH).locator("circle")).to_have_attribute("fill", "var(--dry)")
    assert not page.errors


def test_all_off_only_when_included(browser, servers, ha):
    page = open_page(browser, servers, "desktop")
    page.once("dialog", lambda d: d.accept())
    page.click("#allOff")
    ha.wait_call(lambda c: c["service"] == "turn_off")
    assert not calls(ha, "turn_off")  # default: the dehumidifier keeps running
    ha.reset()
    marker(page, DH).locator("circle").click()
    box = page.locator("#sheetContent .dh-alloff input")
    expect(box).not_to_be_checked()
    box.check()
    expect(page.locator("#status")).to_contain_text("“All off” also turns off Dehumidifier")
    assert page.request.get(servers["app"] + "/api/layout").json()["settings"]["all_off_include"] == [DH]
    page.click("#sheetClose")
    for eid in ("light.lounge", "light.strip", "switch.tv", DH):  # back on after ha.reset()
        expect(page.locator(f'#list li[data-dev="{eid}"] .val')).to_have_text(re.compile("^on"))
    asked = []
    page.once("dialog", lambda d: (asked.append(d.message), d.accept()))
    page.click("#allOff")
    ha.wait_call(lambda c: c["service"] == "turn_off" and c["domain"] == "humidifier")
    assert asked[0].startswith("Turn off 4 devices")
    assert not page.errors


def test_history_and_wall_humidity(browser, servers, ha):
    page = open_page(browser, servers, "desktop")
    page.evaluate("localStorage.setItem('hc-hist-open', '1')")
    page.reload()
    marker(page, DH).locator("circle").click()
    expect(page.locator("#sheetContent .dehumidifier-hist .line.s0")).to_be_visible()
    expect(page.locator("#sheetContent .dehumidifier-hist .line.s1.step")).to_have_count(1)
    expect(page.locator("#sheetContent .dh-runs .bar.dehumidifier.on")).to_have_count(2)
    expect(page.locator("#sheetContent .dh-runs-sum")).to_have_text(re.compile(r"^Running \d+ h( \d+ min)? in total · 2 times$"))
    expect(page.locator("#sheetContent .hist-readout")).to_have_text("Current 52–68 %")
    shot(page, "dehum-desktop-history")
    page.click("#sheetClose")
    page.goto(servers["app"] + "/?wall")
    expect(page.locator("#wallHum b")).to_have_text("62 %")
    # body.wall must not pick up the plan's wall-line stroke (room names used to turn into grey blobs)
    assert page.evaluate("getComputedStyle(document.querySelector('#rooms [data-room=bed] text')).stroke") == "none"
    set_state(ha, DH, current_humidity=58)
    expect(page.locator("#wallHum b")).to_have_text("58 %")
    expect(page.locator('#rooms [data-room="bed"] .hum-label')).to_have_text("58 %")
    shot(page, "dehum-wall")
    assert not page.errors


def test_bell_sheet_tank_setting(browser, servers, ha):
    page = open_page(browser, servers, "phone")
    page.click("#alertsBtn")
    expect(page.locator("#dehumSec")).to_be_visible()
    expect(page.locator("#dehumTank")).to_be_checked()
    page.locator("#dehumTank").uncheck()
    page.wait_for_timeout(500)
    assert page.request.get(servers["app"] + "/api/alerts/settings").json()["dehumidifier_tank"] is False
    page.locator("#dehumTank").check()
    page.wait_for_timeout(500)
    assert page.request.get(servers["app"] + "/api/alerts/settings").json()["dehumidifier_tank"] is True
    assert not page.errors
