"""The ⋯ menu: Away / Home, temperatures on the plan, layout export / import; the header on a 390px phone."""
import json
import re

import pytest
from playwright.sync_api import expect

from conftest import LAYOUT, shot


def menu(page, item):
    page.click("#moreBtn")
    expect(page.locator("#moreMenu")).to_be_visible()
    page.click(item)


def test_away_then_home_exact_calls(fresh_stack, open_page):
    ha, url = fresh_stack.ha, fresh_stack.url
    page = open_page(fresh_stack)
    # Door alerts off before leaving: Away turns them on, Home puts them back off.
    assert page.request.put(url + "/api/alerts/settings", data=json.dumps({"enabled": False}),
                            headers={"Content-Type": "application/json"}).ok
    ha.reset()
    menu(page, "#awayBtn")
    expect(page.locator("#modeDialog")).to_be_visible()
    expect(page.locator("#modeTitle")).to_have_text("Leaving home?")
    expect(page.locator("#modeList li")).to_have_text([
        "Turn off all lights and plugs (6)",
        "Set 2 radiators to the away temperature (current targets are remembered)",
        "Turn door alerts on"])
    expect(page.locator("#modeForm [name=away_temp]")).to_have_value("16")
    page.fill("#modeForm [name=away_temp]", "15")
    shot(page, "away-dialog")
    page.click("#modeOk")
    expect(page.locator("#awayBadge")).to_be_visible()
    expect(page.locator("#status")).to_have_text("Away: 6 devices off, radiators 15°")
    calls = ha.wait_calls(3)
    assert [(c["domain"], c["service"], c["data"]) for c in calls] == [
        ("light", "turn_off", {"entity_id": ["light.bedroom", "light.kitchen", "light.lounge", "light.strip"]}),
        ("switch", "turn_off", {"entity_id": ["switch.kettle", "switch.tv"]}),
        ("climate", "set_temperature", {"entity_id": ["climate.bedroom_valve", "climate.lounge_valve"], "temperature": 15}),
    ]
    assert page.request.get(url + "/api/alerts/settings").json()["enabled"] is True
    assert page.request.get(url + "/api/mode").json()["mode"] == "away"
    expect(page.locator('#list li[data-dev="climate.lounge_valve"] .val')).to_have_text("20.5° → 15°")
    page.click("#moreBtn")
    expect(page.locator("#awayBtn")).to_have_text("I'm home")
    page.click("#awayBtn")
    expect(page.locator("#modeTitle")).to_have_text("Back home?")
    expect(page.locator("#awayTempRow")).to_be_hidden()
    page.click("#modeOk")
    expect(page.locator("#awayBadge")).to_be_hidden()
    expect(page.locator("#status")).to_have_text("Welcome home: 2 radiators restored")
    calls = ha.wait_calls(5)
    assert [(c["domain"], c["service"], c["data"]) for c in calls[3:]] == [
        ("climate", "set_temperature", {"entity_id": ["climate.bedroom_valve"], "temperature": 19}),
        ("climate", "set_temperature", {"entity_id": ["climate.lounge_valve"], "temperature": 21}),
    ]
    assert page.request.get(url + "/api/alerts/settings").json()["enabled"] is False
    assert page.request.get(url + "/api/mode").json()["mode"] == "home"
    page.click("#moreBtn")
    expect(page.locator("#awayBtn")).to_have_text("Away…")


def tint(page, room):
    return page.locator(f'[data-room="{room}"] .temp-tint')


def test_temperatures_tint_rooms(stack, ha, open_page):
    page = open_page(stack)
    page.evaluate("localStorage.removeItem('hc.showTemps')")
    expect(tint(page, "lounge")).to_have_attribute("data-temp", "20.5")
    expect(tint(page, "bed")).to_have_attribute("data-temp", "18.0")
    expect(tint(page, "kitchen")).to_have_count(0)  # no valve there
    expect(page.locator('[data-room="lounge"] .temp-label')).to_have_text("20.5°")
    cold = tint(page, "bed").get_attribute("style")
    shot(page, "temps")
    # Live: the lounge warms up -> orange.
    ha.set("climate.lounge_valve", current_temperature=23.5)
    expect(tint(page, "lounge")).to_have_attribute("data-temp", "23.5")
    expect(tint(page, "lounge")).to_have_attribute("style", "fill:rgb(255,138,61)")
    assert "rgb(" in cold and cold != tint(page, "lounge").get_attribute("style")
    # Hidden while editing.
    page.click("#editToggle")
    expect(page.locator(".temp-tint")).to_have_count(0)
    page.click("#cancelEdit")
    expect(page.locator(".temp-tint")).to_have_count(2)
    # Switch off in the menu; remembered across reloads.
    menu(page, "#tempToggle")
    expect(page.locator(".temp-tint")).to_have_count(0)
    expect(page.locator(".temp-label")).to_have_count(0)
    page.reload()
    page.locator("#plan .marker").first.wait_for()
    expect(page.locator(".temp-tint")).to_have_count(0)
    menu(page, "#tempToggle")
    expect(page.locator(".temp-tint")).to_have_count(2)


def test_export_import_round_trip(stack, ha, open_page, tmp_path):
    page = open_page(stack)
    original = page.request.get(stack.url + "/api/layout").json()
    page.click("#moreBtn")
    with page.expect_download() as dl:
        page.click("#exportLayout")
    d = dl.value
    assert re.fullmatch(r"homecontrol-layout-\d{4}-\d\d-\d\d\.json", d.suggested_filename), d.suggested_filename
    path = tmp_path / d.suggested_filename
    d.save_as(path)
    assert json.loads(path.read_text()) == original
    expect(page.locator("#status")).to_have_text("Layout exported")

    # Replace the plan, then import the export back.
    other = {"unit": "ft", "rooms": [{"id": "x", "name": "Shed", "x": 0, "y": 0, "w": 2, "h": 2}], "placements": []}
    assert page.request.put(stack.url + "/api/layout", data=json.dumps(other), headers={"Content-Type": "application/json"}).ok
    page.reload()
    expect(page.locator("#rooms .room")).to_have_count(1)
    page.click("#moreBtn")
    with page.expect_file_chooser() as fc:
        page.click("#importLayout")
    fc.value.set_files(str(path))
    expect(page.locator("#importDialog")).to_be_visible()
    expect(page.locator("#importList li")).to_have_text(
        ["5 rooms", "9 placed devices", "2 doors/windows", "All devices are known to Home Assistant"])
    shot(page, "import-dialog")
    page.click("#importOk")
    expect(page.locator("#importDialog")).to_be_hidden()
    expect(page.locator("#status")).to_have_text(f"Imported {path.name}")
    expect(page.locator("#rooms .room")).to_have_count(5)
    assert page.request.get(stack.url + "/api/layout").json() == original
    assert page.locator("#unit").input_value() == "m"

    # A layout with a device HA doesn't have: rejected, then imported without it.
    ghost = {**original, "placements": original["placements"] + [{"entity_id": "light.ghost", "x": 1, "y": 1}]}
    gpath = tmp_path / "ghost.json"
    gpath.write_text(json.dumps(ghost))
    page.click("#moreBtn")
    with page.expect_file_chooser() as fc:
        page.click("#importLayout")
    fc.value.set_files(str(gpath))
    expect(page.locator("#importList li").nth(3)).to_have_text("1 not in Home Assistant now: light.ghost")
    page.click("#importOk")
    expect(page.locator("#importMsg")).to_have_text(re.compile(r"^Rejected: .*unknown", re.I))
    expect(page.locator("#importStrip")).to_be_visible()
    page.click("#importStrip")
    expect(page.locator("#importDialog")).to_be_hidden()
    assert page.request.get(stack.url + "/api/layout").json() == original
    assert ha.calls() == []


def test_menu_closes_on_outside_tap_and_escape(stack, ha, open_page):
    page = open_page(stack, "mobile")
    page.tap("#moreBtn")
    expect(page.locator("#moreMenu")).to_be_visible()
    expect(page.locator("#moreBtn")).to_have_attribute("aria-expanded", "true")
    page.touchscreen.tap(200, 700)
    expect(page.locator("#moreMenu")).to_be_hidden()
    page.tap("#moreBtn")
    page.keyboard.press("Escape")
    expect(page.locator("#moreMenu")).to_be_hidden()


def no_horizontal_scroll(page):
    m = page.evaluate("""(() => {
        const h = document.querySelector('header');
        const right = Math.max(...[...h.querySelectorAll('button, .status, .away-badge, h1')]
            .filter((e) => e.offsetParent).map((e) => e.getBoundingClientRect().right));
        return {hs: h.scrollWidth, hc: h.clientWidth, ds: document.documentElement.scrollWidth, w: innerWidth, right};
    })()""")
    assert m["hs"] <= m["hc"] and m["ds"] <= m["w"] and m["right"] <= m["w"], m


def test_header_fits_390px(fresh_stack, open_page):
    page = open_page(fresh_stack, "mobile")
    no_horizontal_scroll(page)
    for sel in ("#heating", "#allOff", "#editToggle", "#alertsBtn", "#moreBtn"):
        expect(page.locator(sel)).to_be_in_viewport()
    shot(page, "header-390")
    # With the AWAY badge and a long status line too.
    assert page.request.post(fresh_stack.url + "/api/mode", data=json.dumps({"mode": "away"}),
                             headers={"Content-Type": "application/json"}).ok
    page.reload()
    expect(page.locator("#awayBadge")).to_be_visible()
    no_horizontal_scroll(page)
    page.click("#editToggle")
    no_horizontal_scroll(page)
    shot(page, "header-390-away")
