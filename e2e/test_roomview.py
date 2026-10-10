"""Room view: one room zoomed in on the live plan (⤢, double-tap / long-press on the floor, ⋯ → Rooms…, #room=<id>)."""
import copy
import re

from playwright.sync_api import expect

from conftest import LAYOUT, WAIT, Touch, center, marker, marker_center, plan_xy, shot, toggles

WHOLE = [-0.5, -0.5, 9.4, 8.6]  # computeViewBox() of LAYOUT: rooms 0..8.4 × 0..7.6, 0.5 m margin
PAD = 0.4


def box(x, y, w, h):
    return [x - PAD, y - PAD, w + 2 * PAD, h + 2 * PAD]


ROOMS = {r["id"]: box(r["x"], r["y"], r["w"], r["h"]) for r in LAYOUT["rooms"]}


def viewbox(page):
    return [float(v) for v in page.get_attribute("#plan", "viewBox").split()]


def wait_viewbox(page, want, tol=1e-3):
    page.wait_for_function("""([want, tol]) => {
        const vb = document.getElementById('plan').getAttribute('viewBox').split(' ').map(Number);
        return vb.length === 4 && vb.every((v, i) => Math.abs(v - want[i]) <= tol);
    }""", arg=[want, tol])


def in_room(page, room):
    expect(page.locator("body")).to_have_class(re.compile(r"\broom-view\b"))
    expect(page.locator("#roomName")).to_have_text(next(r["name"] for r in LAYOUT["rooms"] if r["id"] == room))
    wait_viewbox(page, ROOMS[room])
    assert page.url.endswith(f"/#room={room}"), page.url


def at_home(page, url):
    expect(page.locator("body")).not_to_have_class(re.compile(r"\broom-view\b"))
    expect(page.locator("#roomBar")).to_be_hidden()
    wait_viewbox(page, WHOLE)
    page.wait_for_function("u => location.href === u", arg=url + "/")
    expect(page.locator("#sideTitle")).to_have_text("Devices")


def listed(page):
    return sorted(page.locator("#list li[data-dev]").evaluate_all("els => els.map((e) => e.dataset.dev)"))


def next_calls(ha, act):
    """The fake-HA calls made by act() (ha.reset() would also reset the states)."""
    n = len(ha.calls())
    act()
    return ha.wait_calls(n + 1)[n:]


def no_horizontal_scroll(page):
    m = page.evaluate("""(() => {
        const right = Math.max(...[...document.querySelectorAll('#roomBar button, #roomBar h2, #roomFacts .rf')]
            .filter((e) => e.offsetParent).map((e) => e.getBoundingClientRect().right));
        return {ds: document.documentElement.scrollWidth, bs: document.body.scrollWidth, w: innerWidth, right};
    })()""")
    assert m["ds"] <= m["w"] and m["bs"] <= m["w"] and m["right"] <= m["w"], m


# ---------------------------------------------------------------------------------------------------------------------

def test_expand_button_zooms_dims_and_filters(stack, ha, open_page):
    page = open_page(stack)
    expect(page.locator("[data-roomopen]")).to_have_count(5)
    shot(page, "roomview-home-desktop")
    page.click('[data-roomopen="lounge"]')
    in_room(page, "lounge")
    # Everything outside the room is dimmed and can't be tapped; the mask's hole is the room.
    expect(page.locator("#roomMask .rv-dim")).to_have_count(1)
    assert page.get_attribute("#roomMask .rv-outline", "points") == "0,0 5.2,0 5.2,4.2 0,4.2"
    for eid in ("light.kitchen", "switch.kettle", "light.bedroom", "climate.bedroom_valve", "binary_sensor.contact_sensor_door"):
        expect(marker(page, eid)).to_have_class(re.compile(r"\brv-out\b"))
        assert marker(page, eid).evaluate("e => getComputedStyle(e).pointerEvents") == "none"
    for eid in ("light.lounge", "light.strip", "switch.tv", "climate.lounge_valve"):
        expect(marker(page, eid)).not_to_have_class(re.compile(r"\brv-out\b"))
    expect(page.locator("[data-roomopen]")).to_have_count(0)
    # Bigger markers than the whole-home view, still a sensible size on screen.
    r_px = marker(page, "light.lounge").locator("circle").bounding_box()["width"] / 2
    assert 18 <= r_px <= 30, r_px
    # The strip, the filtered list and the facts.
    expect(page.locator("#roomClimate [data-fact=temp]")).to_have_text("20.5°")
    expect(page.locator("#roomLights")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#sideTitle")).to_have_text("In Lounge")
    assert listed(page) == ["climate.lounge_valve", "light.lounge", "light.strip", "switch.tv"]
    expect(page.locator("#roomFacts [data-fact=lights]")).to_have_text("2 of 2 lights on")
    expect(page.locator("#roomFacts [data-fact=power]")).to_have_text("86.4 W")
    expect(page.locator("#roomFacts [data-fact=open]")).to_have_count(0)  # no sensor in the lounge
    shot(page, "roomview-lounge-desktop")

    # A device tap in room view toggles exactly that light.
    page.click('#plan .marker[data-dev="light.lounge"] circle')
    calls = ha.wait_calls(1)
    assert [(c["domain"], c["service"], c["data"]) for c in calls] == [("light", "toggle", {"entity_id": "light.lounge"})]
    expect(page.locator('#list li[data-dev="light.lounge"] .val')).to_have_text("off")
    expect(page.locator("#roomFacts [data-fact=lights]")).to_have_text("1 of 2 lights on")
    # Lights switch: one still on -> both off, in one call.
    calls = next_calls(ha, lambda: page.click("#roomLights"))
    assert [(c["domain"], c["service"], sorted(c["data"]["entity_id"])) for c in calls] == [
        ("light", "turn_off", ["light.lounge", "light.strip"])]
    expect(page.locator("#roomLights")).to_have_attribute("aria-pressed", "false")
    expect(page.locator("#roomFacts [data-fact=lights]")).to_have_text("Lights off")
    calls = next_calls(ha, lambda: page.click("#roomLights"))
    assert [(c["service"], sorted(c["data"]["entity_id"])) for c in calls] == [("turn_on", ["light.lounge", "light.strip"])]
    expect(page.locator("#roomLights")).to_have_attribute("aria-pressed", "true")
    # The room name still toggles its lights in room view.
    calls = next_calls(ha, lambda: page.click('[data-roomtap="lounge"] rect'))
    assert [c["service"] for c in calls] == ["turn_off"]

    page.click("#roomBack")
    at_home(page, stack.url)
    expect(page.locator("[data-roomopen]")).to_have_count(5)
    expect(page.locator(".marker.rv-out")).to_have_count(0)
    assert len(listed(page)) > 9


def test_rooms_menu_arrows_escape_and_browser_back(stack, ha, open_page):
    page = open_page(stack)
    page.click("#moreBtn")
    page.click("#roomsBtn")
    expect(page.locator("#roomsSheet")).to_be_visible()
    expect(page.locator("#roomsList .rooms-item .name")).to_have_text(["Lounge", "Kitchen", "Hall", "Bedroom", "Bathroom"])
    expect(page.locator('#roomsList [data-room="lounge"] .sub')).to_have_text("2 of 2 on · 20.5°")
    shot(page, "roomview-rooms-sheet")
    page.click('#roomsList [data-room="kitchen"]')
    expect(page.locator("#roomsSheet")).to_be_hidden()
    in_room(page, "kitchen")
    assert listed(page) == ["light.kitchen", "switch.kettle"]
    page.keyboard.press("ArrowRight")
    in_room(page, "hall")
    expect(page.locator("#roomFacts [data-fact=open]")).to_have_text("Front door closed")
    ha.set("binary_sensor.contact_sensor_door", "on")
    expect(page.locator("#roomFacts [data-fact=open]")).to_have_text("Front door open")
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("ArrowLeft")
    in_room(page, "lounge")
    page.keyboard.press("ArrowLeft")  # wraps round
    in_room(page, "bath")
    expect(page.locator("#sideHint")).to_have_text("No devices placed in this room.")
    page.click("#roomNext")
    in_room(page, "lounge")
    page.click("#roomPrev")
    in_room(page, "bath")
    # Switching rooms replaced the history entry: one Escape / Back goes home.
    page.keyboard.press("Escape")
    at_home(page, stack.url)

    # Escape closes an open sheet first, not the room view.
    page.click('[data-roomopen="lounge"]')
    in_room(page, "lounge")
    page.click('#list li[data-dev="climate.lounge_valve"]')
    expect(page.locator("#sheet")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#sheet")).to_be_hidden()
    in_room(page, "lounge")

    # Browser back / forward.
    page.go_back()
    at_home(page, stack.url)
    page.go_forward()
    in_room(page, "lounge")
    page.go_back()
    at_home(page, stack.url)


def test_deep_link_and_back(stack, ha, open_page):
    page = open_page(stack, goto=None)
    page.locator("#plan .marker").first.wait_for()
    # Typed into the address bar of the open app (same page)...
    page.goto(stack.url + "/#room=bed")
    in_room(page, "bed")
    # ...and opened fresh (a bookmark).
    page.reload()
    in_room(page, "bed")
    assert listed(page) == ["climate.bedroom_valve", "light.bedroom"]
    expect(page.locator("#roomClimate [data-fact=temp]")).to_have_text("18.0°")
    # Back lands on the whole home, not off the app.
    page.go_back()
    at_home(page, stack.url)
    # Unknown room: the whole home, hash dropped.
    page.goto(stack.url + "/#room=attic")
    page.reload()
    page.locator("#plan .marker").first.wait_for()
    at_home(page, stack.url)


def test_reduced_motion_is_instant(stack, ha, open_page):
    page = open_page(stack, reduced_motion="reduce")
    vb = page.evaluate("""() => { document.querySelector('[data-roomopen="kitchen"]')
        .dispatchEvent(new MouseEvent('click', { bubbles: true })); return document.getElementById('plan').getAttribute('viewBox'); }""")
    assert [round(float(v), 6) for v in vb.split()] == [round(v, 6) for v in ROOMS["kitchen"]]


def test_phone_touch_open_swipe_and_tap(stack, ha, open_page):
    page = open_page(stack, "mobile")
    touch = Touch(page)
    no_horizontal_scroll(page)
    shot(page, "roomview-home-phone")
    touch.tap(*center(page.locator('[data-roomopen="lounge"] .bg')))
    in_room(page, "lounge")
    no_horizontal_scroll(page)
    for sel in ("#roomBack", "#roomName", "#roomPrev", "#roomNext", "#roomLights"):
        expect(page.locator(sel)).to_be_in_viewport()
    shot(page, "roomview-lounge-phone")
    assert ha.calls() == []
    # Tapping a light in the room toggles it.
    touch.tap(*marker_center(page, "light.strip"))
    assert [c["data"] for c in ha.wait_calls(1)] == [{"entity_id": "light.strip"}]
    # Swipe left: next room; right: back.
    n = len(ha.calls())
    y = plan_xy(page, 0, 2.4)[1]
    touch.drag(320, y, 70, y)
    in_room(page, "kitchen")
    no_horizontal_scroll(page)
    shot(page, "roomview-kitchen-phone")
    touch.drag(70, y, 320, y)
    in_room(page, "lounge")
    # A mostly vertical drag doesn't switch rooms.
    touch.drag(200, y - 60, 230, y + 80)
    page.wait_for_timeout(400)
    in_room(page, "lounge")
    assert len(ha.calls()) == n
    page.go_back()
    at_home(page, stack.url)

    # Double-tap on the bedroom's floor.
    x, y = plan_xy(page, 3.4, 6.8)
    touch.tap(x, y)
    page.wait_for_timeout(120)
    touch.tap(x, y)
    in_room(page, "bed")
    page.click("#roomBack")
    at_home(page, stack.url)
    # Long-press on the kitchen's floor; lifting the finger toggles nothing.
    x, y = plan_xy(page, 6.0, 1.7)
    touch.hold(x, y, 900)
    in_room(page, "kitchen")
    page.wait_for_timeout(300)
    assert len(ha.calls()) == n
    page.click("#roomBack")
    at_home(page, stack.url)
    # A single tap on a floor does nothing; the room name still toggles its lights.
    x, y = plan_xy(page, 3.4, 6.8)
    touch.tap(x, y)
    page.wait_for_timeout(500)
    expect(page.locator("body")).not_to_have_class(re.compile(r"\broom-view\b"))
    calls = next_calls(ha, lambda: touch.tap(*center(page.locator('[data-roomtap="kitchen"] rect'))))
    assert [(c["service"], c["data"]) for c in calls] == [("turn_on", {"entity_id": ["light.kitchen"]})]
    expect(page.locator("body")).not_to_have_class(re.compile(r"\broom-view\b"))


def test_double_click_desktop(stack, ha, open_page):
    page = open_page(stack)
    x, y = plan_xy(page, 6.0, 2.2)
    page.mouse.dblclick(x, y)
    in_room(page, "kitchen")
    assert ha.calls() == []


def test_l_shaped_room(stack, ha, open_page):
    layout = copy.deepcopy(LAYOUT)
    lounge = next(r for r in layout["rooms"] if r["id"] == "lounge")
    lounge["cut"] = {"corner": "ne", "w": 2.0, "h": 1.5}
    # The kettle sits in the cut-out corner: outside the L, so not the lounge's.
    next(p for p in layout["placements"] if p["entity_id"] == "switch.kettle").update(x=4.4, y=0.7)
    page = open_page(stack, layout=layout)
    # ⤢ sits inside the L, clear of the cut-out corner.
    assert page.evaluate("""() => { const b = document.querySelector('[data-roomopen="lounge"] .bg'), r = st.layout.rooms[0];
        const x = +b.getAttribute('x'), y = +b.getAttribute('y'), s = +b.getAttribute('width');
        return [[x, y], [x + s, y], [x, y + s], [x + s, y + s]].every(([px, py]) => inRoom(r, { x: px, y: py })); }""")
    page.click('[data-roomopen="lounge"]')
    in_room(page, "lounge")  # the box of the whole L
    assert page.get_attribute("#roomMask .rv-outline", "points") == "0,0 3.2,0 3.2,1.5 5.2,1.5 5.2,4.2 0,4.2"
    assert listed(page) == ["climate.lounge_valve", "light.lounge", "light.strip", "switch.tv"]
    expect(marker(page, "switch.kettle")).to_have_class(re.compile(r"\brv-out\b"))
    shot(page, "roomview-l-shape")


def test_edit_leaves_room_view(stack, ha, open_page):
    page = open_page(stack)
    page.click('[data-roomopen="kitchen"]')
    in_room(page, "kitchen")
    page.click("#editToggle")
    expect(page.locator("body")).to_have_class(re.compile(r"\bediting\b"))
    expect(page.locator("body")).not_to_have_class(re.compile(r"\broom-view\b"))
    page.wait_for_function("u => location.href === u", arg=stack.url + "/")
    expect(page.locator("#sideTitle")).to_have_text("Unplaced devices")
    expect(page.locator("[data-roomopen]")).to_have_count(0)
    # No way in while editing: a double-click on a floor isn't a room view.
    x, y = plan_xy(page, 3.0, 6.5)
    page.mouse.dblclick(x, y)
    page.wait_for_timeout(300)
    expect(page.locator("body")).not_to_have_class(re.compile(r"\broom-view\b"))
    page.keyboard.press("Escape")
    page.click("#cancelEdit")
    expect(page.locator("[data-roomopen]")).to_have_count(5)


def test_wall_mode_room_view(stack, ha, open_page):
    page = open_page(stack, "tablet", goto="/?wall")
    page.locator("#plan .marker").first.wait_for()
    expect(page.locator("body")).to_have_class(re.compile(r"\bwall\b"))
    page.click('[data-roomopen="lounge"]')
    expect(page.locator("body")).to_have_class(re.compile(r"\broom-view\b"))
    wait_viewbox(page, ROOMS["lounge"])
    assert page.url.endswith("/?wall#room=lounge"), page.url
    expect(page.locator("#wallRoomBack")).to_be_visible()
    expect(page.locator("#roomBack")).to_be_hidden()
    expect(page.locator("#roomName")).to_have_text("Lounge")
    shot(page, "roomview-wall")
    page.click("#wallRoomBack")
    expect(page.locator("body")).not_to_have_class(re.compile(r"\broom-view\b"))
    expect(page.locator("#wallRoomBack")).to_be_hidden()
    page.wait_for_function("u => location.href === u", arg=stack.url + "/?wall")
    # Escape leaves the room first, then (a second time) wall mode.
    page.click('[data-roomopen="kitchen"]')
    wait_viewbox(page, ROOMS["kitchen"])
    page.keyboard.press("Escape")
    expect(page.locator("body")).not_to_have_class(re.compile(r"\broom-view\b"))
    expect(page.locator("body")).to_have_class(re.compile(r"\bwall\b"))
    page.wait_for_function("u => location.href === u", arg=stack.url + "/?wall")
    # Dimming returns to the whole home.
    page.click('[data-roomopen="bed"]')
    expect(page.locator("body")).to_have_class(re.compile(r"\broom-view\b"))
    page.evaluate("localStorage.setItem('hc.wall.settings', JSON.stringify({start: '23:00', end: '07:00', idle: 1, nightIdle: 1}))")
    expect(page.locator("#wallDim")).to_be_visible(timeout=WAIT * 1000)
    expect(page.locator("body")).not_to_have_class(re.compile(r"\broom-view\b"))
    wait_viewbox(page, WHOLE)
    page.wait_for_function("u => location.href === u", arg=stack.url + "/?wall")
    page.evaluate("localStorage.removeItem('hc.wall.settings')")


def test_offline_room_view_from_cache(stack, ha, open_page):
    page = open_page(stack)
    page.wait_for_function("navigator.serviceWorker.controller !== null")
    page.reload()
    page.locator("#plan .marker").first.wait_for()
    page.context.set_offline(True)
    page.goto(stack.url + "/#room=kitchen")
    in_room(page, "kitchen")
    assert listed(page) == ["light.kitchen", "switch.kettle"]
    page.context.set_offline(False)
