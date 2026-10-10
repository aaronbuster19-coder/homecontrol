"""Scenes and sleep timers in the browser: make a scene in ⋯ → Scenes… (each ticked device starts from its current
state), run it from the list, a room view and the quick tiles — one batched set of HA calls on the wire; protected
plugs can't be set to off. Sleep timers from a light / plug sheet and a room, the countdown strip, cancel, firing when
the server clock passes the end, and surviving an app restart. Guests: light-only scenes and light timers. Desktop and
a 390px touch phone, light and dark."""
import json
import time

import pytest
from playwright.sync_api import expect

from conftest import LAYOUT, Touch, center, hold, login, marker, put_layout, shot

SIZES_THEMES = [("desktop", "dark"), ("phone", "light"), ("desktop", "light"), ("phone", "dark")]


def press(page, locator):
    locator.tap() if page.size == "phone" else locator.click()


def long_press(page, locator):
    if page.size == "phone":
        Touch(page).hold(*center(locator), ms=700)
    else:
        hold(page, locator, 700)


def wait_until(fn, timeout=10.0, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def api(page, base, method, path, body=None):
    r = page.request.fetch(base + path, method=method, data=json.dumps(body) if body is not None else None,
                           headers={"Content-Type": "application/json"})
    assert r.ok, r.text()
    return r.json()


def clear_scenes(page, base):
    for s in api(page, base, "GET", "/api/scenes")["scenes"]:
        api(page, base, "DELETE", f"/api/scenes/{s['id']}")


def clear_timers(page, base):
    for t in api(page, base, "GET", "/api/timers")["timers"]:
        api(page, base, "DELETE", f"/api/timers/{t['id']}")


def open_scenes(page):
    press(page, page.locator("#moreBtn"))
    press(page, page.locator("#scenesBtn"))
    expect(page.locator("#scenesSheet")).to_be_visible()


def open_sheet(page, eid):
    long_press(page, marker(page, eid).locator("circle").first)
    expect(page.locator("#sheet")).to_be_visible()
    expect(page.locator("#sleepRow")).to_be_visible()


def dev_row(page, eid):
    return page.locator(f'#sceneDevs li[data-dev="{eid}"]')


def scene_calls(ha):
    return [(c["domain"], c["service"], c["data"]) for c in ha.calls() if c["service"] in ("turn_on", "turn_off", "select_source")]


# ---------------- scenes ----------------
@pytest.mark.parametrize("size,theme", SIZES_THEMES)
def test_make_and_run_a_scene(stack, ha, open_page, size, theme):
    page = open_page(stack, size, color_scheme=theme)
    clear_scenes(page, stack.url)
    open_scenes(page)
    expect(page.locator("#sceneList")).to_contain_text("No scenes yet")
    press(page, page.locator("#sceneNew"))
    page.locator("#sceneName").fill("Movie night")
    page.locator("#sceneRoom").select_option("lounge")  # only the lounge's devices are listed now
    expect(page.locator("#sceneDevs li[data-dev]")).to_have_count(3)  # lounge lamp, TV strip, TV plug
    press(page, dev_row(page, "light.lounge").locator("input[type=checkbox]"))
    lounge = dev_row(page, "light.lounge")
    expect(lounge.locator(".scene-seg button.sel")).to_have_text("On")  # captured: on, 78 %, 2700 K
    expect(lounge.locator(".ctl-val").first).to_have_text("78 %")
    lounge.locator("input[type=range]").first.evaluate("(r) => { r.value = 40; r.dispatchEvent(new Event('input')); }")
    press(page, dev_row(page, "light.strip").locator("input[type=checkbox]"))
    press(page, dev_row(page, "light.strip").locator('.scene-seg button[data-on="0"]'))
    press(page, dev_row(page, "switch.tv").locator("input[type=checkbox]"))
    press(page, dev_row(page, "switch.tv").locator('.scene-seg button[data-on="0"]'))
    expect(page.locator(".scene-bar")).to_contain_text("3 picked")
    shot(page, f"scenes-editor-{size}-{theme}")
    press(page, page.locator("#sceneSave"))
    expect(page.locator("#sceneList .scene-run")).to_have_count(1)
    expect(page.locator("#sceneList .scene-sum")).to_have_text("2 lights · 1 plug · Lounge")
    shot(page, f"scenes-list-{size}-{theme}")
    ha.reset()
    press(page, page.locator("#sceneList .scene-run"))
    expect(page.locator("#status")).to_contain_text("Scene “Movie night”")
    calls = wait_until(lambda: len(scene_calls(ha)) >= 3 and scene_calls(ha), what="the scene's calls")
    assert sorted(calls, key=json.dumps) == sorted([
        ("light", "turn_on", {"entity_id": ["light.lounge"], "brightness_pct": 40, "color_temp_kelvin": 2700}),
        ("light", "turn_off", {"entity_id": ["light.strip"]}),
        ("switch", "turn_off", {"entity_id": ["switch.tv"]}),
    ], key=json.dumps)
    assert [c[1] for c in calls].index("turn_off") > 0  # switch-ons go first
    time.sleep(0.5)
    assert len(scene_calls(ha)) == 3  # one batched set, nothing more


def test_edit_capture_delete(stack, ha, open_page):
    page = open_page(stack)
    clear_scenes(page, stack.url)
    api(page, stack.url, "POST", "/api/scenes", {"name": "Reading", "actions": [{"entity_id": "light.lounge", "on": True, "brightness_pct": 20}]})
    ha.set("light.lounge", "on", brightness=255, color_mode="hs", hs_color=[120, 90])
    open_scenes(page)
    press(page, page.locator(".scene-edit"))
    expect(page.locator("#sceneName")).to_have_value("Reading")
    expect(dev_row(page, "light.lounge").locator(".ctl-val").first).to_have_text("20 %")
    press(page, page.locator("#sceneCapture"))  # read it from HA again: 100 %, green
    expect(page.locator("#sceneMsg")).to_have_text("Set from how everything is now.")
    expect(dev_row(page, "light.lounge").locator(".ctl-val").first).to_have_text("100 %")
    expect(dev_row(page, "light.lounge").locator("select.scene-colour")).to_have_value("colour")
    press(page, page.locator("#sceneSave"))
    expect(page.locator("#sceneList .scene-run")).to_have_count(1)
    saved = api(page, stack.url, "GET", "/api/scenes")["scenes"][0]
    assert saved["actions"] == [{"entity_id": "light.lounge", "on": True, "brightness_pct": 100, "hs_color": [120, 90]}]
    press(page, page.locator(".scene-edit"))
    page.once("dialog", lambda d: d.accept())
    press(page, page.locator("#sceneDelete"))
    expect(page.locator("#sceneList")).to_contain_text("No scenes yet")


def test_protected_plug_cant_be_off(stack, ha, open_page):
    page = open_page(stack, layout={**LAYOUT, "settings": {"keep_on": ["switch.tv"]}})
    clear_scenes(page, stack.url)
    open_scenes(page)
    press(page, page.locator("#sceneNew"))
    press(page, dev_row(page, "switch.tv").locator("input[type=checkbox]"))
    expect(dev_row(page, "switch.tv").locator('.scene-seg button[data-on="0"]')).to_be_disabled()
    r = page.request.post(stack.url + "/api/scenes", data=json.dumps({"name": "x", "actions": [{"entity_id": "switch.tv", "on": False}]}),
                          headers={"Content-Type": "application/json"})
    assert r.status == 400 and "protected" in r.text()
    put_layout(page, stack.url, LAYOUT)


@pytest.mark.parametrize("size,theme", [("phone", "dark"), ("desktop", "light")])
def test_run_from_room_view_and_tiles(stack, ha, open_page, size, theme):
    page = open_page(stack, size, color_scheme=theme, goto=None)
    clear_scenes(page, stack.url)
    api(page, stack.url, "POST", "/api/scenes", {"name": "Bedtime", "room": "bed", "actions": [
        {"entity_id": "light.bedroom", "on": True, "brightness_pct": 10}, {"entity_id": "light.lounge", "on": False}]})
    api(page, stack.url, "POST", "/api/scenes", {"name": "Morning", "actions": [{"entity_id": "light.kitchen", "on": True}]})
    page.goto("about:blank")  # from the signed-in "/" a hash-only goto doesn't load
    page.goto(stack.url + "/#room=bed")
    chip = page.locator('#roomFacts [data-fact="scene"]')
    expect(chip).to_have_count(1)
    expect(chip).to_have_text("Bedtime")
    shot(page, f"scenes-room-{size}-{theme}")
    ha.reset()
    press(page, chip)
    wait_until(lambda: len(scene_calls(ha)) >= 2, what="Bedtime's calls")
    assert ("light", "turn_on", {"entity_id": ["light.bedroom"], "brightness_pct": 10}) in scene_calls(ha)
    page.goto(stack.url + "/?view=tiles")
    expect(page.locator("#tilesScenes .scene-tile")).to_have_count(2)
    shot(page, f"scenes-tiles-{size}-{theme}")
    ha.reset()
    press(page, page.locator('#tilesScenes .scene-tile[data-scene]').filter(has_text="Morning"))
    ha.wait_call(lambda c: c["service"] == "turn_on" and c["data"]["entity_id"] == ["light.kitchen"])


def test_double_tap_runs_once(stack, ha, open_page):
    page = open_page(stack)
    clear_scenes(page, stack.url)
    s = api(page, stack.url, "POST", "/api/scenes", {"name": "Lamp", "actions": [{"entity_id": "light.kitchen", "on": True}]})
    ha.reset()
    a = api(page, stack.url, "POST", f"/api/scenes/{s['id']}/run")
    b = api(page, stack.url, "POST", f"/api/scenes/{s['id']}/run")
    assert a["ok"] and b.get("repeat")
    assert len(ha.wait_calls(1, "turn_on")) == 1


# ---------------- sleep timers ----------------
@pytest.mark.parametrize("size,theme", SIZES_THEMES)
def test_sleep_timer_from_a_light_fires(clock_stack, open_page, size, theme):
    ha = clock_stack.ha
    ha.reset()
    page = open_page(clock_stack, size, color_scheme=theme)
    clear_timers(page, clock_stack.url)
    open_sheet(page, "light.lounge")
    press(page, page.locator('#sleepRow .sleep-chip[data-min="30"]'))
    expect(page.locator("#sleepRow .sleep-now")).to_contain_text("Off in 30 min")
    expect(page.locator("#sleepStrip")).to_be_visible()
    expect(page.locator("#sleepStrip .sleep-item")).to_contain_text("Lounge lamp")
    shot(page, f"sleep-sheet-{size}-{theme}")
    press(page, page.locator("#sheetClose"))
    shot(page, f"sleep-strip-{size}-{theme}")
    assert not [c for c in ha.calls() if c["service"] == "turn_off"]
    clock_stack.set_clock(time.time() + 30 * 60 + 2)
    ha.wait_call(lambda c: c["service"] == "turn_off" and c["data"]["entity_id"] == ["light.lounge"], timeout=15)
    expect(page.locator("#sleepStrip")).to_be_hidden(timeout=10000)
    expect(page.locator("#status")).to_contain_text("Sleep timer: Lounge lamp off")
    clock_stack.set_clock(time.time())


def test_custom_minutes_cancel_and_restart(clock_stack, open_page):
    ha = clock_stack.ha
    ha.reset()
    page = open_page(clock_stack, "phone", color_scheme="light")
    clear_timers(page, clock_stack.url)
    open_sheet(page, "switch.tv")
    press(page, page.locator('#sleepRow .sleep-chip[data-min="custom"]'))
    page.locator("#sleepRow .sleep-custom input").fill("5")
    press(page, page.locator("#sleepRow .sleep-custom button"))
    expect(page.locator("#sleepRow .sleep-now")).to_contain_text("Off in 5 min")
    shot(page, "sleep-custom-phone-light")
    # a restart keeps the end time
    ends = api(page, clock_stack.url, "GET", "/api/timers")["timers"][0]["ends_at"]
    clock_stack.restart_app()
    page.reload()
    page.locator("#plan .marker").first.wait_for()
    expect(page.locator("#sleepStrip .sleep-item")).to_contain_text("TV")
    assert api(page, clock_stack.url, "GET", "/api/timers")["timers"][0]["ends_at"] == ends
    # cancel from the strip: nothing is switched off when the time passes
    press(page, page.locator("#sleepStrip .sleep-x"))
    expect(page.locator("#sleepStrip")).to_be_hidden()
    clock_stack.set_clock(time.time() + 6 * 60)
    time.sleep(2.5)  # the timer loop wakes at least every second: give it two chances to (wrongly) fire
    assert not [c for c in ha.calls() if c["service"] == "turn_off"]
    clock_stack.set_clock(time.time())


@pytest.mark.parametrize("size,theme", [("desktop", "dark"), ("phone", "light")])
def test_room_sleep_timer(clock_stack, open_page, size, theme):
    ha = clock_stack.ha
    ha.reset()
    page = open_page(clock_stack, size, color_scheme=theme, goto="/#room=lounge")
    clear_timers(page, clock_stack.url)
    chip = page.locator('#roomFacts [data-fact="sleep"]')
    expect(chip).to_have_text("Sleep")
    press(page, chip)
    expect(page.locator("#sleepSheet")).to_be_visible()
    expect(page.locator("#sleepContent .sub")).to_have_text("Turns off Lounge lamp, TV, TV strip")
    press(page, page.locator('#sleepContent .sleep-chip[data-min="15"]'))
    expect(page.locator("#sleepContent .sleep-now")).to_contain_text("Off in 15 min")
    shot(page, f"sleep-room-{size}-{theme}")
    press(page, page.locator("#sleepClose"))
    expect(chip).to_have_text("Off in 15 min")
    clock_stack.set_clock(time.time() + 15 * 60 + 2)
    calls = ha.wait_calls(2, "turn_off", timeout=15)
    assert sorted(json.dumps(c["data"]) for c in calls) == sorted(
        [json.dumps({"entity_id": ["light.lounge", "light.strip"]}), json.dumps({"entity_id": ["switch.tv"]})])
    expect(chip).to_have_text("Sleep", timeout=10000)
    clock_stack.set_clock(time.time())


# ---------------- guests ----------------
def test_guest_scenes_and_timers(stack, ha, open_page):
    admin = open_page(stack, goto=None)
    clear_scenes(admin, stack.url)
    clear_timers(admin, stack.url)
    r = admin.request.post(stack.url + "/api/users", data=json.dumps({"username": "kid", "password": "guest-pass-9", "role": "guest"}),
                           headers={"Content-Type": "application/json"})
    assert r.ok or r.status == 409 or "exists" in r.text(), r.text()
    api(admin, stack.url, "POST", "/api/scenes", {"name": "Lamps", "actions": [{"entity_id": "light.kitchen", "on": True}]})
    api(admin, stack.url, "POST", "/api/scenes", {"name": "TV off", "actions": [{"entity_id": "switch.tv", "on": False}]})
    fan = api(admin, stack.url, "POST", "/api/timers", {"entity_id": "switch.tv", "minutes": 60})
    page = open_page(stack, "phone", signed_in=False, color_scheme="dark")
    login(page, stack.url, "kid", "guest-pass-9")
    page.locator("#plan .marker").first.wait_for()
    open_scenes(page)
    expect(page.locator("#sceneList .scene-run")).to_have_count(1)
    expect(page.locator("#sceneList .scene-run")).to_contain_text("Lamps")
    expect(page.locator("#sceneNew")).to_have_count(0)
    expect(page.locator(".scene-edit")).to_have_count(0)
    shot(page, "scenes-guest-phone-dark")
    ha.reset()
    press(page, page.locator("#sceneList .scene-run"))
    ha.wait_call(lambda c: c["service"] == "turn_on" and c["data"]["entity_id"] == ["light.kitchen"])
    press(page, page.locator("#scenesClose"))
    # the admin's TV timer shows, but a guest can't cancel it; a light timer they can set and cancel
    expect(page.locator(f'#sleepStrip [data-timer="{fan["id"]}"]')).to_be_visible()
    expect(page.locator(f'#sleepStrip [data-timer="{fan["id"]}"] .sleep-x')).to_have_count(0)
    open_sheet(page, "light.lounge")
    press(page, page.locator('#sleepRow .sleep-chip[data-min="15"]'))
    expect(page.locator("#sleepRow .sleep-now")).to_contain_text("Off in 15 min")
    shot(page, "sleep-guest-phone-dark")
    for method, path, body in (("POST", "/api/timers", {"entity_id": "switch.tv", "minutes": 5}), ("DELETE", f"/api/timers/{fan['id']}", None),
                               ("POST", "/api/scenes", {"name": "x", "actions": [{"entity_id": "light.kitchen", "on": True}]})):
        r = page.request.fetch(stack.url + path, method=method, data=json.dumps(body) if body else None,
                               headers={"Content-Type": "application/json"})
        assert r.status == 403, (method, path, r.status)
    press(page, page.locator("#sleepRow .sleep-cancel"))
    expect(page.locator("#sleepRow .sleep-now")).to_have_count(0)
    clear_timers(admin, stack.url)
