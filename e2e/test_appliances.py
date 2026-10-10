"""Appliances end to end: furniture linked to smart plugs. Catalogue, Link plug + rename offer, live glow / watts /
status, tap = toggle (fridge and keep-on plugs open the sheet, switching off asks), long-press = sheet, hiding the plug
marker, per-link thresholds, the washer cycle from a scripted power sequence on a movable server clock, the bell-sheet
setting, import without unknown plugs, room view and wall mode. Desktop mouse and a 390px touch phone.

Also the hoover (charging → charged from a scripted power sequence, the opt-in auto-off), the desktop PC and the
protected home server (tap opens the sheet, off asks with a warning, All off skips it).

Runs on its own stack (conftest.appliance_stack): e2e/clock_app.py and a fake HA with six extra plugs —
switch.washer ("Washing machine"), switch.fridge ("Fridge plug"), switch.plug_3 ("Plug 3"), switch.hoover ("Hoover plug"),
switch.pc ("PC plug") and switch.server ("Server plug")."""
import copy
import json
import re
import time

import pytest
from playwright.sync_api import expect

from conftest import LAYOUT, Touch, center, hold, marker, plan_xy, shot, toggles


def fur(id, type, x, y, w, h, rot=0, **kw):
    return {"id": id, "type": type, "x": x, "y": y, "w": w, "h": h, "rot": rot, **kw}


KITCHEN = [
    fur("kettle", "kettle", 7.6, 1.95, 0.25, 0.25, plug="switch.kettle"),
    fur("washer", "washer", 6.75, 1.35, 0.6, 0.6, plug="switch.washer"),   # under the kitchen light's marker
    fur("fridge", "fridge", 8.05, 1.0, 0.6, 0.65, 270, plug="switch.fridge"),
    fur("tv", "tv", 2.6, 4.05, 1.25, 0.25, 180, plug="switch.tv"),
    fur("fan", "fan", 3.6, 6.8, 0.45, 0.45, plug="switch.plug_3"),
    fur("sofa", "sofa", 0.45, 2.2, 2.0, 0.9, 270),
]
LINKED = {**copy.deepcopy(LAYOUT), "furniture": KITCHEN}
START = {**copy.deepcopy(LAYOUT), "furniture": []}


@pytest.fixture
def aha(appliance_stack):
    appliance_stack.ha.reset()
    return appliance_stack.ha


def appl(page, fid):
    return page.locator(f'#appliances .appl[data-appl="{fid}"]')


def tag(page, fid):
    return page.locator(f'#applTags .appl-tag[data-tag="{fid}"]')


def appl_center(page, fid):
    return center(appl(page, fid).locator(".appl-hit"))


def tap(page, x, y):
    if page.size == "mobile":
        Touch(page).tap(x, y)
    else:
        page.mouse.click(x, y)


def long_press(page, x, y):
    if page.size == "mobile":
        Touch(page).hold(x, y, ms=700)
    else:
        page.mouse.move(x, y)
        page.mouse.down()
        page.wait_for_timeout(700)
        page.mouse.up()


def press(page, sel):
    (page.tap if page.size == "mobile" else page.click)(sel)


def no_hscroll(page):
    assert page.evaluate("document.scrollingElement.scrollWidth <= innerWidth"), "horizontal scroll"


def stored(page, stack):
    return page.request.get(stack.url + "/api/layout").json()


def by_id(layout, fid):
    return next(f for f in layout.get("furniture", []) if f["id"] == fid)


# ---------------- linking ----------------
@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_add_link_rename_save(appliance_stack, aha, open_page, size):
    page = open_page(appliance_stack, size, layout=START)
    # Undo a rename from the other parametrisation (names live in the layout settings, carried across PUTs).
    page.request.put(appliance_stack.url + "/api/devices/switch.plug_3/meta", data=json.dumps({"name": None}),
                     headers={"Content-Type": "application/json"})
    page.reload()
    page.locator("#plan .marker").first.wait_for()
    press(page, "#editToggle")
    expect(page.locator("#editbar")).to_be_visible()
    ids = {}
    for t, plug in (("fan", "switch.plug_3"), ("kettle", "switch.kettle"), ("washer", "switch.washer")):
        press(page, "#addFurniture")
        expect(page.locator("#furSheet")).to_be_visible()
        item = page.locator(f'#furGrid .fur-item[data-type="{t}"]')
        expect(item.locator(".appl-badge")).to_have_count(1)  # appliance-capable
        expect(page.locator('#furGrid .fur-item[data-type="iron"] .appl-badge')).to_have_count(1)
        expect(page.locator('#furGrid .fur-item[data-type="hair_straightener"] .appl-badge')).to_have_count(1)
        item.scroll_into_view_if_needed()
        press(page, f'#furGrid .fur-item[data-type="{t}"]')
        expect(page.locator("#furSheet")).to_be_hidden()
        page.wait_for_function("t => st.sel && st.draft.furniture.find((f) => f.id === st.sel.id)?.type === t", arg=t)
        fid = page.evaluate("st.sel.id")
        ids[t] = fid
        # Spread them out so each can be tapped later.
        page.evaluate("([id, x, y]) => { const f = st.draft.furniture.find((f) => f.id === id); f.x = x; f.y = y; render(); }",
                      [fid, *{"fan": (3.6, 6.8), "kettle": (7.6, 1.95), "washer": (6.0, 1.9)}[t]])
        expect(page.locator("#furLink")).to_be_visible()
        press(page, "#furLink")
        expect(page.locator("#plugDialog")).to_be_visible()
        expect(page.locator("#plugList .plug-opt")).to_have_count(9)  # None + 8 plugs
        if t == "kettle":  # already-linked marker on the plug the fan took
            expect(page.locator('#plugList .plug-opt[data-plug="switch.plug_3"]')).to_have_class("plug-opt taken")
            expect(page.locator('#plugList .plug-opt[data-plug="switch.plug_3"] .sub')).to_contain_text("linked to Fan")
        if size == "mobile" and t == "fan":
            shot(page, "appliances-link-dialog-mobile")
            no_hscroll(page)
        press(page, f'#plugList .plug-opt[data-plug="{plug}"]')
        expect(page.locator("#plugDialog")).to_be_hidden()
        assert page.evaluate("id => st.draft.furniture.find((f) => f.id === id).plug", fid) == plug
        expect(page.locator("#furLink span")).to_have_text(re.compile(r"^Plug: "))
        if t == "fan":
            # "Plug 3" isn't called "Fan": one tap renames it (Home Assistant isn't changed).
            offer = page.locator("#applOffer")
            expect(offer).to_be_visible()
            expect(offer.locator(".do")).to_have_text("Rename plug to “Fan”")
            if size == "mobile":
                shot(page, "appliances-rename-offer-mobile")
            press(page, "#applOffer .do")
            expect(offer).to_be_hidden()
            page.wait_for_function("st.devices.get('switch.plug_3').name === 'Fan'")
            assert stored(page, appliance_stack)["settings"]["names"]["switch.plug_3"] == "Fan"
        else:
            expect(page.locator("#applOffer")).to_be_hidden()  # same name already
    # The edit plan shows a link badge on linked pieces.
    expect(page.locator("#furniture .fur .fu-linkdot")).to_have_count(3)
    # Copying a linked appliance doesn't copy its link (one plug, one appliance).
    page.evaluate("id => { st.sel = {type: 'fur', id}; render(); }", ids["kettle"])
    press(page, "#furDup")
    page.wait_for_function("st.draft.furniture.length === 4")
    assert page.evaluate("st.draft.furniture.find((f) => f.id === st.sel.id).plug") is None
    press(page, "#deleteSel")
    page.wait_for_function("st.draft.furniture.length === 3")
    press(page, "#save")
    expect(page.locator("#editbar")).to_be_hidden()
    saved = stored(page, appliance_stack)["furniture"]
    assert {f["type"]: (f.get("plug"), f.get("hide_marker")) for f in saved} == {
        "fan": ("switch.plug_3", True), "kettle": ("switch.kettle", True), "washer": ("switch.washer", True)}
    # View mode: the appliances are the controls; the kettle's own marker is hidden, the others never were placed.
    expect(page.locator("#appliances .appl")).to_have_count(3)
    expect(marker(page, "switch.kettle")).to_have_count(0)
    expect(marker(page, "switch.tv")).to_have_count(1)
    # Device list rows: the appliance's drawing and its status.
    row = page.locator('#list li[data-dev="switch.plug_3"]')
    expect(row.locator("svg.appl-ic")).to_have_count(1)
    expect(row.locator(".name")).to_have_text("Fan")
    expect(row.locator(".val")).to_have_text("Off")
    # Unlink (None) in edit mode: back to plain furniture, the marker returns.
    press(page, "#editToggle")
    page.evaluate("id => { st.sel = {type: 'fur', id}; render(); }", ids["kettle"])
    press(page, "#furLink")
    press(page, '#plugList .plug-opt[data-plug=""]')
    press(page, "#save")
    expect(page.locator("#editbar")).to_be_hidden()
    assert "plug" not in by_id(stored(page, appliance_stack), ids["kettle"])
    expect(marker(page, "switch.kettle")).to_have_count(1)
    expect(page.locator("#appliances .appl")).to_have_count(2)
    no_hscroll(page)


# ---------------- live state ----------------
@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_glow_and_watts_follow_power(appliance_stack, aha, open_page, size):
    page = open_page(appliance_stack, size, layout=LINKED)
    k = appl(page, "kettle")
    expect(k).to_have_class("fur appl fu-kettle")            # off: no glow, no pill
    expect(tag(page, "kettle")).to_have_count(0)
    aha.set("sensor.kettle_power", "2105")
    aha.set("switch.kettle", "on")
    expect(k).to_have_class("fur appl fu-kettle on busy")
    expect(tag(page, "kettle")).to_have_text("Boiling… · 2105 W")
    assert float(page.evaluate("getComputedStyle(document.querySelector('[data-appl=kettle] .appl-glow')).opacity")) > 0
    aha.set("sensor.kettle_power", "1.5")
    expect(k).to_have_class("fur appl fu-kettle on")
    expect(tag(page, "kettle")).to_have_text("Idle · 1.5 W")
    # Fan: "On · 35 W"; fridge: Cooling over 30 W, else Idle; TV standby.
    aha.set("switch.plug_3", "on")
    aha.set("sensor.plug_3_power", "35")
    expect(tag(page, "fan")).to_have_text("On · 35 W")
    aha.set("sensor.plug_3_power", "0")  # plug on, fan itself switched off
    expect(tag(page, "fan")).to_have_text("Idle · 0 W")
    expect(tag(page, "fridge")).to_have_text("Idle · 2.1 W")
    aha.set("sensor.fridge_power", "84")
    expect(tag(page, "fridge")).to_have_text("Cooling · 84 W")
    expect(appl(page, "fridge")).to_have_class("fur appl fu-fridge on busy")
    expect(tag(page, "tv")).to_have_text("On · 86.4 W")
    aha.set("switch.washer", "unavailable")
    expect(tag(page, "washer")).to_have_text("Offline")
    expect(appl(page, "washer")).to_have_class("fur appl fu-washer offline")
    expect(page.locator('#list li[data-dev="switch.fridge"] .val')).to_have_text("Cooling · 84 W")
    shot(page, f"appliances-live-{size}")
    no_hscroll(page)


@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_tap_toggles_plug_and_markers_on_top_win(appliance_stack, aha, open_page, size):
    page = open_page(appliance_stack, size, layout=LINKED)
    tap(page, *appl_center(page, "kettle"))
    aha.wait_call(lambda c: c["service"] == "toggle" and c["data"] == {"entity_id": "switch.kettle"})
    expect(appl(page, "kettle")).to_have_class("fur appl fu-kettle on")
    tap(page, *appl_center(page, "fan"))
    aha.wait_call(lambda c: c["data"] == {"entity_id": "switch.plug_3"})
    # The kitchen light's marker sits on the washer: the marker wins.
    x, y = center(marker(page, "light.kitchen").locator("circle").first)
    tap(page, x, y)
    calls = aha.wait_calls(3)
    assert [c["data"]["entity_id"] for c in calls] == ["switch.kettle", "switch.plug_3", "light.kitchen"]
    # Unlinked furniture still never takes a tap.
    sx, sy = plan_xy(page, 0.45, 2.6)
    assert page.evaluate("([x, y]) => !document.elementFromPoint(x, y).closest('#furniture, #appliances')", [sx, sy])
    # Room names keep their taps even with an appliance under them.
    tap(page, *center(page.locator('[data-roomtap="kitchen"] rect')))
    aha.wait_call(lambda c: c["service"] in ("turn_on", "turn_off") and "light.kitchen" in json.dumps(c["data"]))


@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_fridge_and_keep_on_open_the_sheet_and_off_asks(appliance_stack, aha, open_page, size):
    layout = {**LINKED, "settings": {"keep_on": ["switch.tv"]}}
    page = open_page(appliance_stack, size, layout=layout)
    tap(page, *appl_center(page, "fridge"))
    expect(page.locator("#sheet")).to_be_visible()
    expect(page.locator("#sheetContent h3")).to_have_text("Fridge plug")
    expect(page.locator("#sheetContent .appl-sec .name")).to_have_text("Fridge")
    page.wait_for_timeout(400)
    assert toggles(aha, "switch.fridge") == []
    shot(page, f"appliances-fridge-sheet-{size}")
    seen = []
    page.once("dialog", lambda d: (seen.append(d.message), d.dismiss()))
    press(page, "#sheetContent button.big")
    page.wait_for_timeout(400)
    assert seen == ["Turn off the fridge?"] and toggles(aha, "switch.fridge") == []
    page.once("dialog", lambda d: (seen.append(d.message), d.accept()))
    press(page, "#sheetContent button.big")
    aha.wait_call(lambda c: c["data"] == {"entity_id": "switch.fridge"})
    press(page, "#sheetClose")
    expect(page.locator("#sheet")).to_be_hidden()
    # A keep-on plug's appliance (the TV) opens its sheet too, and switching off asks.
    tap(page, *appl_center(page, "tv"))
    expect(page.locator("#sheetContent h3")).to_have_text("TV")
    page.once("dialog", lambda d: (seen.append(d.message), d.dismiss()))
    press(page, "#sheetContent button.big")
    page.wait_for_timeout(300)
    assert seen[-1] == "Turn off the TV?" and toggles(aha, "switch.tv") == []
    # "All off" leaves the fridge alone (like keep-on).
    press(page, "#sheetClose")
    aha.set("switch.fridge", "on")
    page.once("dialog", lambda d: d.accept())
    press(page, "#allOff")
    call = aha.wait_call(lambda c: c["service"] == "turn_off" and c["domain"] == "switch")[0]
    assert "switch.fridge" not in call["data"]["entity_id"] and "switch.tv" not in call["data"]["entity_id"]


@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_long_press_opens_plug_sheet_with_history(appliance_stack, aha, open_page, size):
    page = open_page(appliance_stack, size, layout=LINKED)
    long_press(page, *appl_center(page, "kettle"))
    expect(page.locator("#sheet")).to_be_visible()
    expect(page.locator("#sheetContent h3")).to_have_text("Kettle")
    expect(page.locator("#sheetContent .appl-sec")).to_be_visible()
    expect(page.locator("#sheetContent details.hist")).to_have_count(1)
    expect(page.locator("#sheetContent .appl-th")).to_have_count(1)
    page.wait_for_timeout(500)
    assert toggles(aha, "switch.kettle") == []
    no_hscroll(page)


def test_hide_and_unhide_plug_marker(appliance_stack, aha, open_page):
    page = open_page(appliance_stack, layout=LINKED)
    expect(marker(page, "switch.kettle")).to_have_count(0)
    hold(page, appl(page, "kettle").locator(".appl-hit"), 700)
    box = page.locator("#applHide")
    expect(box).to_be_checked()
    box.uncheck()
    expect(marker(page, "switch.kettle")).to_have_count(1)
    page.wait_for_function("st.layout.furniture.find((f) => f.id === 'kettle').hide_marker === false")
    assert by_id(stored(page, appliance_stack), "kettle")["hide_marker"] is False
    page.click("#sheetClose")
    # Both still work: the marker and the appliance.
    page.mouse.click(*center(marker(page, "switch.kettle").locator("circle").first))
    page.mouse.click(*appl_center(page, "kettle"))
    calls = aha.wait_calls(2)
    assert [c["data"] for c in calls] == [{"entity_id": "switch.kettle"}] * 2
    # Showing the furniture off (⋯ menu) brings every marker back.
    hold(page, appl(page, "kettle").locator(".appl-hit"), 700)
    page.locator("#applHide").check()
    expect(marker(page, "switch.kettle")).to_have_count(0)
    page.click("#sheetClose")
    page.click("#moreBtn")
    page.locator("#furToggle").uncheck()
    expect(marker(page, "switch.kettle")).to_have_count(1)
    expect(page.locator("#appliances .appl")).to_have_count(0)
    page.locator("#furToggle").check()
    expect(marker(page, "switch.kettle")).to_have_count(0)


def test_thresholds_per_link(appliance_stack, aha, open_page):
    page = open_page(appliance_stack, layout=LINKED)
    aha.set("switch.kettle", "on")
    aha.set("sensor.kettle_power", "800")
    expect(tag(page, "kettle")).to_have_text("Idle · 800 W")
    hold(page, appl(page, "kettle").locator(".appl-hit"), 700)
    page.click("#sheetContent .appl-th summary")
    inp = page.locator('#sheetContent .appl-th input[name="on_w"]')
    expect(inp).to_have_value("1000")
    inp.fill("500")
    inp.press("Enter")
    page.wait_for_function("st.layout.furniture.find((f) => f.id === 'kettle').thresholds?.on_w === 500")
    assert by_id(stored(page, appliance_stack), "kettle")["thresholds"] == {"on_w": 500.0}
    expect(tag(page, "kettle")).to_have_text("Boiling… · 800 W")
    page.click("#sheetContent .appl-th .linkish")  # reset to defaults
    page.wait_for_function("!st.layout.furniture.find((f) => f.id === 'kettle').thresholds")
    expect(tag(page, "kettle")).to_have_text("Idle · 800 W")


# ---------------- washer cycle ----------------
@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_washer_cycle_from_power(appliance_stack, aha, open_page, size):
    stack = appliance_stack
    t0 = time.time() + 86400 * (2 if size == "desktop" else 3)  # each run on its own day, after any earlier cycle
    stack.set_clock(t0)
    page = open_page(stack, size, layout=LINKED)

    def phase():
        return page.request.get(stack.url + "/api/appliances").json()["appliances"]["washer"].get("phase")

    expect(tag(page, "washer")).to_have_text("Idle · 0.8 W")
    aha.set("sensor.washer_power", "480")                    # the drum starts
    expect(tag(page, "washer")).to_have_text("Starting… · 480 W")
    stack.set_clock(t0 + 130)                                # > 10 W for 2 min: running
    expect(tag(page, "washer")).to_have_text("Running 2 min · 480 W")
    expect(appl(page, "washer")).to_have_class("fur appl fu-washer on busy")
    stack.set_clock(t0 + 47 * 60 + 30)
    aha.set("sensor.washer_power", "2.2")                    # soaking: a quiet spell mid-cycle
    expect(tag(page, "washer")).to_have_text("Running 47 min · 2.2 W")
    stack.set_clock(t0 + 49 * 60 + 30)                       # quiet for 2 min only: still the same cycle
    aha.set("sensor.washer_power", "2")
    expect(tag(page, "washer")).to_have_text("Running 49 min · 2 W")
    assert phase() == "running"
    aha.set("sensor.washer_power", "1900")                   # heating again
    expect(tag(page, "washer")).to_have_text("Running 49 min · 1900 W")
    stack.set_clock(t0 + 80 * 60)
    aha.set("sensor.washer_power", "1.1")                    # done
    stack.set_clock(t0 + 83 * 60 + 5)                        # quiet for 3 min: finished, 3 min ago
    expect(tag(page, "washer")).to_have_text("Finished 3 min ago · 1.1 W")
    assert phase() == "finished"
    expect(page.locator('#list li[data-dev="switch.washer"] .val')).to_have_text("Finished 3 min ago · 1.1 W")
    stack.set_clock(t0 + 95 * 60)
    page.reload()                                            # a fresh page gets the cycle from the server
    page.locator("#plan .marker").first.wait_for()
    expect(tag(page, "washer")).to_have_text("Finished 15 min ago · 1.1 W")
    long_press(page, *plan_xy(page, 6.5, 1.6))                # the part of the washer the light's marker doesn't cover
    expect(page.locator("#sheetContent .appl-status")).to_have_text("Finished 15 min ago · 1.1 W")
    expect(page.locator('#sheetContent .appl-th input')).to_have_count(4)
    shot(page, f"appliances-washer-sheet-{size}")
    no_hscroll(page)
    page.click("#sheetClose") if size == "desktop" else page.tap("#sheetClose")
    stack.set_clock(t0 + 4 * 3600)                           # long after: Idle
    page.reload()
    page.locator("#plan .marker").first.wait_for()
    expect(tag(page, "washer")).to_have_text("Idle · 1.1 W")


# ---------------- settings, import, room view, wall ----------------
def test_bell_sheet_setting(appliance_stack, aha, open_page):
    page = open_page(appliance_stack, layout=LINKED)
    page.click("#alertsBtn")
    box = page.locator("#applDone")
    expect(box).to_be_checked()
    box.uncheck()
    page.wait_for_function("fetch('/api/alerts/settings').then((r) => r.json()).then((s) => s.appliance_done === false)")
    box.check()
    page.wait_for_function("fetch('/api/alerts/settings').then((r) => r.json()).then((s) => s.appliance_done === true)")


def test_import_without_unknown_plugs(appliance_stack, aha, open_page, tmp_path):
    page = open_page(appliance_stack, layout=START)
    data = {**copy.deepcopy(LINKED), "furniture": KITCHEN + [fur("gone", "toaster", 6, 2, 0.3, 0.2, plug="switch.gone")]}
    f = tmp_path / "flat.json"
    f.write_text(json.dumps(data))
    page.click("#moreBtn")
    page.click("#importLayout")
    page.set_input_files("#importFile", str(f))
    expect(page.locator("#importList")).to_contain_text("switch.gone")
    page.click("#importOk")
    expect(page.locator("#importStrip")).to_be_visible()
    page.click("#importStrip")
    # The dialog closes once the save returns, which can take a while on a busy CI host.
    try:
        expect(page.locator("#importDialog")).to_be_hidden(timeout=15000)
    except AssertionError:
        raise AssertionError(f"import dialog still open: {page.locator('#importMsg').text_content()!r}")
    saved = stored(page, appliance_stack)["furniture"]
    assert "plug" not in by_id({"furniture": saved}, "gone") and by_id({"furniture": saved}, "kettle")["plug"] == "switch.kettle"
    expect(page.locator("#appliances .appl")).to_have_count(5)


@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_room_view_and_wall_mode(appliance_stack, aha, open_page, size):
    page = open_page(appliance_stack, size, layout=LINKED, goto="/#room=kitchen")
    expect(page.locator("#roomBar")).to_be_visible()
    page.wait_for_function("!ROOMVIEW.animating")
    aha.set("sensor.kettle_power", "2000")
    aha.set("switch.kettle", "on")
    expect(tag(page, "kettle")).to_have_text("Boiling… · 2000 W")
    shot(page, f"appliances-roomview-{size}")
    no_hscroll(page)
    tap(page, *appl_center(page, "kettle"))
    aha.wait_call(lambda c: c["data"] == {"entity_id": "switch.kettle"})
    page.goto(appliance_stack.url + "/?wall")
    page.locator("#plan .marker").first.wait_for()
    expect(appl(page, "kettle")).to_have_count(1)
    expect(page.locator("#appliances .appl")).to_have_count(5)
    shot(page, f"appliances-wall-{size}")
    tap(page, *appl_center(page, "fan"))
    aha.wait_call(lambda c: c["data"] == {"entity_id": "switch.plug_3"})


# ---------------- left-on reminders, Turn off, usage stats ----------------
def test_remind_setting_in_sheet(appliance_stack, aha, open_page):
    page = open_page(appliance_stack, layout=LINKED)
    hold(page, appl(page, "fan").locator(".appl-hit"), 700)
    cb, sel = page.locator("#applRemind"), page.locator("#applRemindFor")
    expect(cb).to_be_checked()                       # fans remind after 3 h by default
    expect(sel).to_have_value("180")
    sel.select_option("60")
    page.wait_for_function("st.layout.furniture.find((f) => f.id === 'fan').remind === 60")
    assert by_id(stored(page, appliance_stack), "fan")["remind"] == 60
    page.locator("#applRemind").uncheck()
    page.wait_for_function("st.layout.furniture.find((f) => f.id === 'fan').remind === false")
    expect(page.locator("#applRemindFor")).to_be_disabled()
    page.locator("#applRemind").check()              # back to the default: stored as no setting
    page.locator("#applRemindFor").select_option("180")
    page.wait_for_function("!('remind' in st.layout.furniture.find((f) => f.id === 'fan'))")
    page.click("#sheetClose")
    # Washers have cycles instead, fridges are always on: no reminder there.
    hold(page, page.locator('#appliances .appl[data-appl="fridge"] .appl-hit'), 700)
    expect(page.locator("#sheetContent .appl-sec")).to_be_visible()
    expect(page.locator("#applRemind")).to_have_count(0)


def test_turn_off_endpoint_and_dev_deep_link(appliance_stack, aha, open_page):
    page = open_page(appliance_stack, layout=LINKED)
    r = page.request.post(appliance_stack.url + "/api/devices/switch.tv/turn_off")
    assert r.ok
    call = aha.wait_call(lambda c: c["data"] == {"entity_id": "switch.tv"})[0]
    assert (call["domain"], call["service"]) == ("switch", "turn_off")
    assert page.request.post(appliance_stack.url + "/api/devices/climate.lounge_valve/turn_off").status == 400
    # Tapping the reminder (or Turn off while signed out) opens the app on that plug's sheet.
    page.goto(appliance_stack.url + "/?dev=switch.plug_3")
    expect(page.locator("#sheet")).to_be_visible()
    expect(page.locator("#sheetContent h3")).to_have_text(re.compile("Plug 3|Fan"))
    assert "dev=" not in page.url
    assert toggles(aha, "switch.plug_3") == []


@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_usage_stats_in_sheet(appliance_stack, aha, open_page, size):
    layout = {**LINKED, "settings": {"keep_on": [], "energy": {"rate_p": 25.0, "standing_p": None}}}
    page = open_page(appliance_stack, size, layout=layout)
    long_press(page, *plan_xy(page, 6.5, 1.6))       # washer
    stats = page.locator("#sheetContent .appl-stats")
    expect(stats).to_contain_text(re.compile(r"Cycles\s*\d+ this week · \d+ last week"))
    expect(stats).to_contain_text("Average cycle")
    expect(stats).to_contain_text("2 h · 2.50 kWh · 63p")  # 1 h at 2 kW + 1 h at 500 W, at 25p
    shot(page, f"appliances-stats-washer-{size}")
    no_hscroll(page)
    page.click("#sheetClose") if size == "desktop" else page.tap("#sheetClose")
    expect(page.locator("#sheet")).to_be_hidden()
    long_press(page, *appl_center(page, "kettle"))
    expect(page.locator("#sheetContent .appl-stats")).to_contain_text(re.compile(r"Boils\s*\d+ today · \d+ this week"))
    page.click("#sheetClose") if size == "desktop" else page.tap("#sheetClose")
    long_press(page, *appl_center(page, "fridge"))
    expect(page.locator("#sheetContent .appl-stats")).to_contain_text(re.compile(r"Per day\s*\d+\.\d\d kWh · \d+p \(7-day average\)"))
    page.click("#sheetClose") if size == "desktop" else page.tap("#sheetClose")
    long_press(page, *appl_center(page, "fan"))
    expect(page.locator("#sheetContent .appl-stats")).to_contain_text("On this week")


# ---------------- hoover, desktop PC, home server ----------------
OFFICE = [
    fur("hoover", "hoover", 6.6, 3.95, 0.3, 0.25, plug="switch.hoover"),        # in the hall
    fur("pc", "desktop_pc", 2.6, 1.0, 0.2, 0.45, plug="switch.pc"),             # in the lounge
    fur("server", "home_server", 4.2, 1.0, 0.4, 0.4, plug="switch.server"),
]
GADGETS = {**copy.deepcopy(LAYOUT), "furniture": OFFICE}


@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_add_hoover_pc_server_and_link(appliance_stack, aha, open_page, size):
    page = open_page(appliance_stack, size, layout=START)
    press(page, "#editToggle")
    expect(page.locator("#editbar")).to_be_visible()
    press(page, "#addFurniture")
    expect(page.locator("#furSheet")).to_be_visible()
    for t, name in (("hoover", "Hoover"), ("desktop_pc", "Desktop PC"), ("home_server", "Home server")):
        item = page.locator(f'#furGrid .fur-item[data-type="{t}"]')
        expect(item.locator(".appl-badge")).to_have_count(1)
        expect(item.locator(".n")).to_have_text(name)
    # Next to the desks, and the hoover with the appliances.
    assert page.evaluate("[...document.querySelectorAll('#furGrid .fur-item[data-type=desktop_pc], #furGrid .fur-item[data-type=home_server]')]"
                         ".every((b) => b.closest('.fur-grid').querySelector('[data-type=desk]'))")
    assert page.evaluate("!!document.querySelector('#furGrid .fur-item[data-type=hoover]').closest('.fur-grid').querySelector('[data-type=kettle]')")
    page.locator('#furGrid .fur-item[data-type="desktop_pc"]').scroll_into_view_if_needed()
    shot(page, f"appliances-catalogue-office-{size}")
    page.locator('#furGrid .fur-item[data-type="hoover"]').scroll_into_view_if_needed()
    shot(page, f"appliances-catalogue-hoover-{size}")
    no_hscroll(page)
    press(page, "#furClose")
    spots = {"hoover": (6.6, 3.95), "desktop_pc": (2.6, 1.0), "home_server": (4.2, 1.0)}
    plugs = {"hoover": "switch.hoover", "desktop_pc": "switch.pc", "home_server": "switch.server"}
    for t in spots:
        press(page, "#addFurniture")
        page.locator(f'#furGrid .fur-item[data-type="{t}"]').scroll_into_view_if_needed()
        press(page, f'#furGrid .fur-item[data-type="{t}"]')
        page.wait_for_function("t => st.sel && st.draft.furniture.find((f) => f.id === st.sel.id)?.type === t", arg=t)
        fid = page.evaluate("st.sel.id")
        page.evaluate("([id, x, y]) => { const f = st.draft.furniture.find((f) => f.id === id); f.x = x; f.y = y; render(); }",
                      [fid, *spots[t]])
        press(page, "#furLink")
        expect(page.locator("#plugDialog")).to_be_visible()
        press(page, f'#plugList .plug-opt[data-plug="{plugs[t]}"]')
        expect(page.locator("#plugDialog")).to_be_hidden()
        if page.locator("#applOffer").is_visible():
            press(page, "#applOffer .x")
    press(page, "#save")
    expect(page.locator("#editbar")).to_be_hidden()
    saved = stored(page, appliance_stack)["furniture"]
    assert {f["type"]: f.get("plug") for f in saved} == plugs
    assert all("auto_off" not in f for f in saved)   # the hoover's auto-off is opt-in
    expect(page.locator("#appliances .appl")).to_have_count(3)
    expect(tag(page, next(f["id"] for f in saved if f["type"] == "home_server"))).to_have_text("Running · 35 W")
    expect(tag(page, next(f["id"] for f in saved if f["type"] == "hoover"))).to_have_text("Not charging · 0.5 W")
    no_hscroll(page)


@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_hoover_charge_from_power_and_auto_off(appliance_stack, aha, open_page, size):
    stack = appliance_stack
    t0 = time.time() + 86400 * (5 if size == "desktop" else 6)   # after the washer tests' days
    stack.set_clock(t0)
    hid = f"hoover_{size}"                                  # its own charge state on the shared stack
    layout = {**GADGETS, "furniture": [{**OFFICE[0], "id": hid}, *OFFICE[1:]]}
    page = open_page(stack, size, layout=layout)
    h = tag(page, hid)

    def charge():
        return page.request.get(stack.url + "/api/appliances").json()["appliances"][hid]

    expect(h).to_have_text("Not charging · 0.5 W")
    aha.set("sensor.hoover_power", "45")                     # back on the dock
    expect(h).to_have_text("Charging · 45 W")
    expect(appl(page, hid)).to_have_class("fur appl fu-hoover on busy")
    stack.set_clock(t0 + 40 * 60)
    aha.set("sensor.hoover_power", "6")                      # tapering off: still charging
    expect(h).to_have_text("Charging · 6 W")
    aha.set("sensor.hoover_power", "1.2")                    # trickle
    stack.set_clock(t0 + 45 * 60)
    expect(h).to_have_text("Charging · 1.2 W")
    assert charge()["phase"] == "charging"
    stack.set_clock(t0 + 51 * 60)                            # under 3 W for 10 min: charged
    expect(h).to_have_text("Charged · 1.2 W")
    expect(h).to_have_class("appl-tag on charged")
    expect(appl(page, hid)).to_have_class("fur appl fu-hoover on charged")
    expect(page.locator('#list li[data-dev="switch.hoover"] .val')).to_have_text("Charged · 1.2 W")
    c = charge()
    assert c["phase"] == "charged" and c["status"] == "Charged"
    assert toggles(aha, "switch.hoover") == []               # auto-off is off: nothing switched
    shot(page, f"appliances-hoover-charged-{size}")
    # The sheet: status, the opt-in toggle (off), the charge thresholds.
    long_press(page, *appl_center(page, hid))
    expect(page.locator("#sheetContent .appl-status")).to_have_text("Charged · 1.2 W")
    expect(page.locator("#applRemind")).to_have_count(0)
    box = page.locator("#applAutoOff")
    expect(box).not_to_be_checked()
    page.click("#sheetContent .appl-th summary") if size == "desktop" else page.tap("#sheetContent .appl-th summary")
    expect(page.locator('#sheetContent .appl-th input[name="charge_w"]')).to_have_value("10")
    expect(page.locator('#sheetContent .appl-th input[name="trickle_w"]')).to_have_value("3")
    expect(page.locator('#sheetContent .appl-th input[name="charged_min"]')).to_have_value("10")
    box.check() if size == "desktop" else box.tap()
    page.wait_for_function("id => st.layout.furniture.find((f) => f.id === id).auto_off === true", arg=hid)
    assert by_id(stored(page, stack), hid)["auto_off"] is True
    shot(page, f"appliances-hoover-sheet-{size}")
    no_hscroll(page)
    page.click("#sheetClose") if size == "desktop" else page.tap("#sheetClose")
    # The next charge: with auto-off on, the server switches the plug off once it's charged — exactly once.
    stack.set_clock(t0 + 3 * 3600)
    aha.set("sensor.hoover_power", "52")
    expect(h).to_have_text("Charging · 52 W")
    stack.set_clock(t0 + 4 * 3600)
    aha.set("sensor.hoover_power", "0.8")
    stack.set_clock(t0 + 4 * 3600 + 5 * 60)
    expect(h).to_have_text("Charging · 0.8 W")
    assert toggles(aha, "switch.hoover") == []
    stack.set_clock(t0 + 4 * 3600 + 11 * 60)
    call = aha.wait_call(lambda c: c["service"] == "turn_off" and c["data"] == {"entity_id": "switch.hoover"})[0]
    assert call["domain"] == "switch"
    expect(h).to_have_count(0)                                # off plugs show no pill
    expect(page.locator('#list li[data-dev="switch.hoover"] .val')).to_have_text("Not charging")
    aha.set("switch.hoover", "on")                            # switched back on by hand, still full
    expect(h).to_have_text("Charged · 0.8 W")
    stack.set_clock(t0 + 5 * 3600)                            # time passes: never a second call
    page.wait_for_function("u => fetch(u).then((r) => r.json()).then((d) => d.now > 0)", arg=stack.url + "/api/appliances")
    assert len(toggles(aha, "switch.hoover")) == 1


@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_server_is_protected_and_pc_status(appliance_stack, aha, open_page, size):
    page = open_page(appliance_stack, size, layout=GADGETS)
    expect(tag(page, "server")).to_have_text("Running · 35 W")
    expect(tag(page, "pc")).to_have_count(0)                 # off
    aha.set("sensor.pc_power", "120")
    aha.set("switch.pc", "on")
    expect(tag(page, "pc")).to_have_text("On · 120 W")
    aha.set("sensor.pc_power", "4")
    expect(tag(page, "pc")).to_have_text("Sleep · 4 W")
    shot(page, f"appliances-office-{size}")
    # The PC toggles on a tap like any appliance…
    tap(page, *appl_center(page, "pc"))
    aha.wait_call(lambda c: c["service"] == "toggle" and c["data"] == {"entity_id": "switch.pc"})
    # …the server opens its sheet instead.
    tap(page, *appl_center(page, "server"))
    expect(page.locator("#sheet")).to_be_visible()
    expect(page.locator("#sheetContent h3")).to_have_text("Server plug")
    expect(page.locator("#sheetContent .appl-sec .name")).to_have_text("Home server")
    expect(page.locator("#sheetContent .appl-sec .hint").first).to_contain_text("All off, Away and the standby saver leave it on")
    expect(page.locator("#applRemind")).to_have_count(0)
    # The standby saver can't be switched on for it.
    expect(page.locator("#standbyRow .sb-info")).to_have_text("A home server is linked to this plug — it always stays on.")
    expect(page.locator("#standbyRow input[type=checkbox]")).to_be_disabled()
    shot(page, f"appliances-server-sheet-{size}")
    no_hscroll(page)
    seen = []
    page.once("dialog", lambda d: (seen.append(d.message), d.dismiss()))
    press(page, "#sheetContent button.big")              # the confirm is synchronous: dismissed, nothing sent
    assert len(seen) == 1 and seen[0].startswith("Turn off the home server?")
    assert "This may be the server running homecontrol" in seen[0]
    assert toggles(aha, "switch.server") == []
    page.once("dialog", lambda d: (seen.append(d.message), d.accept()))
    press(page, "#sheetContent button.big")
    aha.wait_call(lambda c: c["data"] == {"entity_id": "switch.server"})
    assert len(toggles(aha, "switch.server")) == 1     # neither the tap nor the dismissed confirm sent anything
    press(page, "#sheetClose")
    expect(page.locator("#sheet")).to_be_hidden()
    # "All off" never includes the server.
    aha.set("switch.server", "on")
    aha.set("switch.pc", "on")
    page.wait_for_function("st.devices.get('switch.server').state === 'on' && st.devices.get('switch.pc').state === 'on'")
    page.once("dialog", lambda d: d.accept())
    press(page, "#allOff")
    call = aha.wait_call(lambda c: c["service"] == "turn_off" and c["domain"] == "switch")[0]
    assert "switch.server" not in call["data"]["entity_id"] and "switch.pc" in call["data"]["entity_id"]
    expect(tag(page, "server")).to_have_text("Running · 35 W")


def test_hoover_and_pc_usage_stats(appliance_stack, aha, open_page):
    page = open_page(appliance_stack, layout=GADGETS)
    long_press(page, *appl_center(page, "hoover"))
    stats = page.locator("#sheetContent .appl-stats")
    expect(stats).to_contain_text(re.compile(r"Charges\s*\d+ this week"))
    expect(stats).to_contain_text(re.compile(r"Average charge\s*1 h"))
    page.click("#sheetClose")
    aha.set("switch.pc", "on")
    long_press(page, *appl_center(page, "pc"))
    expect(page.locator("#sheetContent .appl-stats")).to_contain_text("On this week")
    expect(page.locator("#applRemind")).not_to_be_checked()     # available, off by default
