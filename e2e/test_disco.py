"""Disco mode: start from ⋯ → Disco… and from a room view, the running bar and its Stop, the photosensitivity warning,
lights restored, stops on a hand change / All off / the time limit, HA errors, guests. Desktop and a 390px touch
phone, light and dark. The fake HA logs every call (with its time), so timing and batching are checked on the wire."""
import json
import time

import pytest
from playwright.sync_api import expect

from conftest import login, shot

SIZES_THEMES = [("desktop", "dark"), ("phone", "light"), ("desktop", "light"), ("phone", "dark")]
LOUNGE = {"entity_id": ["light.lounge"], "brightness": 200, "color_temp_kelvin": 2700}  # how the fake HA starts
STRIP = {"entity_id": ["light.strip"], "brightness": 120, "hs_color": [275, 90]}


def press(page, locator):
    locator.tap() if page.size == "phone" else locator.click()


def wait_until(fn, timeout=8.0, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def steps(ha):
    """The disco's colour steps (a restore sends raw brightness, a step never does)."""
    return [c for c in ha.calls("turn_on") if "hs_color" in c["data"] and "brightness" not in c["data"]]


def restores(ha):
    return [c for c in ha.calls() if c["service"] == "turn_off" or (c["service"] == "turn_on" and "brightness" in c["data"])]


def api_start(page, base, **body):
    r = page.request.post(base + "/api/disco/start", data=json.dumps({"preset": "rainbow", "speed": "fast", **body}),
                          headers={"Content-Type": "application/json"})
    assert r.ok, r.text()
    return r.json()


def open_disco(page):
    press(page, page.locator("#moreBtn"))
    press(page, page.locator("#discoBtn"))
    expect(page.locator("#discoSheet")).to_be_visible()


@pytest.fixture(autouse=True)
def no_disco_left(stack):
    """The module shares one app: a test that fails mid-disco must not leave it running for the next."""
    yield
    import urllib.request
    import base64
    from conftest import USER, PASSWORD
    req = urllib.request.Request(stack.url + "/api/disco/stop", method="POST", headers={
        "Authorization": "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()})
    urllib.request.urlopen(req, timeout=10)


@pytest.mark.parametrize("size,theme", SIZES_THEMES)
def test_start_from_menu_then_stop_restores(stack, ha, open_page, size, theme):
    page = open_page(stack, size, color_scheme=theme)
    open_disco(page)
    sheet = page.locator("#discoSheet")
    expect(sheet.locator("h3")).to_have_text("Disco")
    lights = sheet.locator(".disco-lights input")
    expect(lights).to_have_count(2)  # the colour lights; kitchen and bedroom can only dim
    expect(sheet.locator(".disco-lights input:checked")).to_have_count(2)  # both on, so both ticked
    expect(sheet).to_contain_text("2 lights without colour stay as they are")
    expect(page.locator("#discoMinutes")).to_have_value("30")
    press(page, sheet.locator(".disco-seg label", has_text="Fast"))
    shot(page, f"disco-sheet-{size}-{theme}")
    press(page, page.locator("#discoGo"))
    bar = page.locator("#discoBar")
    expect(bar).to_be_visible()
    expect(sheet).to_be_hidden()
    expect(page.locator("#discoBarTitle")).to_have_text("Disco · Rainbow fade")
    expect(page.locator("#discoBarSub")).to_have_text("2 lights · stops in 30 min")
    # batched: one light.turn_on for both lights per step; never twice a second per light
    got = wait_until(lambda: len(steps(ha)) >= 3 and steps(ha), what="three disco steps")
    assert all(c["data"]["entity_id"] == ["light.lounge", "light.strip"] for c in got)
    ts = [c["t"] for c in got]
    assert all(b - a >= 0.95 for a, b in zip(ts, ts[1:])), ts
    shot(page, f"disco-running-{size}-{theme}")
    n = len(ha.calls())
    press(page, page.locator("#discoStop"))
    expect(bar).to_be_hidden()
    expect(page.locator("#status")).to_contain_text("Disco stopped")
    back = wait_until(lambda: len(restores(ha)) >= 2 and restores(ha), what="the restore")
    assert sorted((c["data"] for c in back), key=json.dumps) == sorted([LOUNGE, STRIP], key=json.dumps)
    time.sleep(1.5)
    assert not [c for c in ha.calls()[n:] if c in steps(ha)], "a colour step went out after Stop"


@pytest.mark.parametrize("size,theme", [("phone", "dark"), ("desktop", "light")])
def test_room_chip_and_flash_warning(stack, ha, open_page, size, theme):
    page = open_page(stack, size, color_scheme=theme)
    press(page, page.locator('[data-roomopen="lounge"]'))  # the ⤢ in the room's corner
    expect(page.locator("#roomName")).to_have_text("Lounge")
    chip = page.locator("#roomFacts .disco-chip")
    expect(chip).to_have_text("Disco")
    press(page, chip)
    sheet = page.locator("#discoSheet")
    expect(sheet.locator(".sub")).to_contain_text("Lounge")
    press(page, sheet.locator(".disco-presets label", has_text="Party flash"))
    expect(page.locator("#discoGo")).to_have_text("Start party flash")
    press(page, page.locator("#discoGo"))
    expect(sheet.locator("h3")).to_have_text("Photosensitivity warning")
    expect(sheet.locator(".disco-warning")).to_contain_text("seizures")
    shot(page, f"disco-warning-{size}-{theme}")
    assert not steps(ha), "nothing may flash before the warning is acknowledged"
    press(page, page.locator("#discoWarnOk"))
    expect(page.locator("#discoBar")).to_be_visible()
    expect(page.locator("#discoBarTitle")).to_have_text("Disco · Party flash")
    expect(chip).to_have_text("Disco on")
    wait_until(lambda: steps(ha), what="a flash step")
    shot(page, f"disco-room-{size}-{theme}")
    press(page, page.locator("#discoStop"))
    expect(chip).to_have_text("Disco")


def test_flash_refused_by_the_server_without_the_warning(stack, ha, open_page):
    page = open_page(stack)
    r = page.request.post(stack.url + "/api/disco/start", data=json.dumps({"preset": "flash", "entity_ids": ["light.lounge"]}),
                          headers={"Content-Type": "application/json"})
    assert r.status == 400 and "photosensitivity" in r.text()
    assert not ha.calls()


def test_picked_light_that_was_off_goes_on_and_back_off(stack, ha, open_page):
    ha.set("light.strip", "off")
    page = open_page(stack, "phone")
    open_disco(page)
    row = page.locator(".disco-lights label", has_text="TV strip")
    expect(row.locator(".disco-state")).to_have_text("off")
    expect(row.locator("input")).not_to_be_checked()  # an off light only takes part when picked
    press(page, row)
    expect(row.locator(".disco-state")).to_have_text("off · will switch on")
    press(page, page.locator("#discoGo"))
    expect(page.locator("#discoBar")).to_be_visible()
    first = ha.wait_call(lambda c: c["service"] == "turn_on" and "light.strip" in c["data"]["entity_id"])[0]
    assert first["data"]["brightness_pct"] == 80
    press(page, page.locator("#discoStop"))
    ha.wait_call(lambda c: c["service"] == "turn_off" and c["data"]["entity_id"] == ["light.strip"])
    assert any(c["data"] == LOUNGE for c in restores(ha))


def test_nothing_on_nothing_picked(stack, ha, open_page):
    ha.set("light.strip", "off")
    ha.set("light.lounge", "off")
    page = open_page(stack)
    open_disco(page)
    expect(page.locator(".disco-lights input:checked")).to_have_count(0)
    expect(page.locator("#discoGo")).to_be_disabled()
    r = page.request.post(stack.url + "/api/disco/start", data=json.dumps({"preset": "rainbow"}), headers={"Content-Type": "application/json"})
    assert r.status == 400 and "No colour lights are on" in r.text()
    assert not ha.calls()


def test_hand_change_stops_and_keeps_that_light(stack, ha, open_page):
    page = open_page(stack)
    api_start(page, stack.url)
    expect(page.locator("#discoBar")).to_be_visible()
    wait_until(lambda: len(steps(ha)) >= 2, what="two steps")
    ha.set("light.lounge", "off")  # the wall switch
    expect(page.locator("#discoBar")).to_be_hidden()
    expect(page.locator("#status")).to_contain_text("A light was changed by hand")
    back = wait_until(lambda: restores(ha), what="the restore")
    time.sleep(1.2)
    assert [c["data"] for c in restores(ha)] == [STRIP]  # the lounge lamp stays off, as switched
    assert not any("light.lounge" in c["data"]["entity_id"] for c in restores(ha))


def test_tapping_a_disco_light_in_the_app_stops_it(stack, ha, open_page):
    page = open_page(stack)
    api_start(page, stack.url)
    expect(page.locator("#discoBar")).to_be_visible()
    page.locator('#plan .marker[data-dev="light.strip"]').click()
    expect(page.locator("#discoBar")).to_be_hidden()
    toggle = ha.wait_call(lambda c: c["service"] == "toggle")[0]
    assert not any(c["data"] == STRIP and c["t"] > toggle["t"] for c in restores(ha))  # the tap wins


def test_all_off_stops_without_switching_back_on(stack, ha, open_page):
    page = open_page(stack, "phone")
    api_start(page, stack.url)
    expect(page.locator("#discoBar")).to_be_visible()
    page.once("dialog", lambda d: d.accept())
    page.locator("#allOff").tap()
    expect(page.locator("#discoBar")).to_be_hidden()
    ha.wait_call(lambda c: c["service"] == "turn_off" and "light.lounge" in c["data"]["entity_id"])
    time.sleep(1.2)
    assert not [c for c in ha.calls("turn_on") if "brightness" in c["data"]], "All off must not restore lights to on"


def test_ha_errors_stop_the_disco(stack, ha, open_page):
    page = open_page(stack)
    api_start(page, stack.url)
    expect(page.locator("#discoBar")).to_be_visible()
    ha._req("/fake/fail", {"service": "turn_on", "count": 3})
    expect(page.locator("#status")).to_contain_text("Home Assistant kept failing", timeout=15000)
    expect(page.locator("#discoBar")).to_be_hidden()
    failed = [c["t"] for c in ha.calls("turn_on") if c.get("failed")]
    assert len(failed) == 3 and failed[2] - failed[1] > failed[1] - failed[0] >= 1.5  # backed off, then gave up
    wait_until(lambda: restores(ha), what="the restore after the errors")


def test_guest_starts_and_stops(stack, ha, open_page):
    admin = open_page(stack, goto=None)
    r = admin.request.post(stack.url + "/api/users", data=json.dumps({"username": "dj", "password": "guest-pass-9", "role": "guest"}),
                           headers={"Content-Type": "application/json"})
    assert r.ok or r.status == 409 or "exists" in r.text(), r.text()
    page = open_page(stack, "phone", signed_in=False, color_scheme="light")
    login(page, stack.url, "dj", "guest-pass-9")
    page.locator("#plan .marker").first.wait_for()
    open_disco(page)
    press(page, page.locator("#discoGo"))
    expect(page.locator("#discoBar")).to_be_visible()
    shot(page, "disco-guest-phone-light")
    press(page, page.locator("#discoStop"))
    expect(page.locator("#discoBar")).to_be_hidden()
    wait_until(lambda: len(restores(ha)) >= 2, what="the restore")


def test_time_limit_stops_it(clock_stack, open_page):
    ha = clock_stack.ha
    ha.reset()
    page = open_page(clock_stack, "phone")
    api_start(page, clock_stack.url, minutes=1)
    expect(page.locator("#discoBarSub")).to_have_text("2 lights · stops in 1 min")
    clock_stack.set_clock(time.time() + 61)
    expect(page.locator("#discoBar")).to_be_hidden(timeout=10000)
    expect(page.locator("#status")).to_contain_text("Time's up")
    wait_until(lambda: len(restores(ha)) >= 2, what="the restore")
