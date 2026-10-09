"""Room snapping, shared walls and Tidy up: desktop mouse and a 390px touch phone (CDP touch events)."""
import json

import pytest
from playwright.sync_api import expect

from conftest import Touch, plan_xy, shot


def room(id, x, y, w, h, cut=None):
    return {"id": id, "name": id, "x": x, "y": y, "w": w, "h": h, **({"cut": cut} if cut else {})}


def near(a, b, t=1e-6):
    return abs(a - b) <= t


@pytest.fixture
def editor(stack, ha, open_page):
    """editor(size) -> (page, fresh) where fresh(layout) saves a layout, reloads and enters edit mode."""
    def make(size="desktop"):
        page = open_page(stack, size, layout=None, goto=None)

        def fresh(layout):
            r = page.request.put(stack.url + "/api/layout", data=json.dumps(layout), headers={"Content-Type": "application/json"})
            assert r.ok, r.text()
            page.goto(stack.url + "/")
            page.wait_for_selector("#walls line", state="attached")
            page.click("#editToggle")
            page.wait_for_selector("#editbar:not([hidden])")
        return page, fresh
    return make


def get_layout(page, stack):
    return page.request.get(stack.url + "/api/layout").json()


def room_of(L, id):
    return next(r for r in L["rooms"] if r["id"] == id)


def draft_room(page, id):
    return page.evaluate("id => st.draft.rooms.find((r) => r.id === id)", id)


def guides(page):
    return page.locator("#walls .snap-guide").count()


def mdrag(page, a, b, during=None):
    """Mouse drag in plan coordinates; optional check while still pressed."""
    x0, y0 = plan_xy(page, *a)
    page.mouse.move(x0, y0)
    page.mouse.down()
    x1, y1 = plan_xy(page, *b)  # the viewBox is frozen while dragging
    page.mouse.move((x0 + x1) / 2, (y0 + y1) / 2, steps=4)
    page.mouse.move(x1, y1, steps=6)
    r = during() if during else None
    page.mouse.up()
    return r


def save(page):
    page.click("#save")
    page.wait_for_selector("#editbar[hidden]", state="attached")


def name_room(page, name):
    page.wait_for_selector("#roomDialog[open]")
    page.fill("#roomForm [name=name]", name)
    page.click("#roomDialog button[value=ok]")


def test_draw_room_snaps_flush(editor, stack):
    page, fresh = editor()
    fresh({"unit": "m", "rooms": [room("lounge", 0, 0, 4, 3)], "placements": []})
    page.click("#addRoom")
    g = mdrag(page, (4.07, 0.06), (7.03, 2.93), lambda: guides(page))
    assert g >= 1, "guide lines visible while drawing"
    name_room(page, "Kitchen")
    assert guides(page) == 0
    save(page)
    k = next(r for r in get_layout(page, stack)["rooms"] if r["name"] == "Kitchen")
    assert (k["x"], k["y"], k["h"]) == (4, 0, 3) and near(k["w"], 3), k


def test_move_room_snaps_and_carries_contents(editor):
    page, fresh = editor()
    fresh({"unit": "m", "rooms": [room("lounge", 0, 0, 4, 3), room("bed", 8, 0.5, 3, 3)],
           "placements": [{"entity_id": "light.lounge", "x": 9.5, "y": 2}],
           "openings": [{"id": "d1", "type": "door", "x": 8.5, "y": 3.5, "len": 0.9, "orient": "h"}]})
    g = mdrag(page, (9, 1.2), (9 - 3.92, 1.2 - 0.55), lambda: guides(page))
    assert g >= 1 and guides(page) == 0
    b = draft_room(page, "bed")
    assert (b["x"], b["y"]) == (4, 0), b
    p, o = page.evaluate("[st.draft.placements[0], st.draft.openings[0]]")
    assert near(p["x"], 5.5) and near(p["y"], 1.5) and near(o["x"], 4.5) and near(o["y"], 3), (p, o)


def test_alt_disables_snapping(editor):
    page, fresh = editor()
    fresh({"unit": "m", "rooms": [room("lounge", 0, 0, 4, 3), room("bed", 8, 0.5, 3, 3)], "placements": []})
    page.keyboard.down("Alt")
    g = mdrag(page, (9, 1.2), (9 - 3.92, 1.2 - 0.55), lambda: guides(page))
    page.keyboard.up("Alt")
    b = draft_room(page, "bed")
    assert near(b["x"], 4.1) and near(b["y"], -0.05) and g == 0, (b, g)


def test_resize_handles_snap(editor):
    page, fresh = editor()
    fresh({"unit": "m", "rooms": [room("lounge", 0, 0, 4, 3), room("bath", 0, 4, 2.5, 2), room("hall", 4, 3, 3, 3)], "placements": []})
    page.click("[data-room=bath]", position={"x": 20, "y": 30})
    nh = page.locator(".handle.h-n rect.hit").bounding_box()
    _, ty = plan_xy(page, 0, 3.06)
    page.mouse.move(nh["x"] + nh["width"] / 2, nh["y"] + nh["height"] / 2)
    page.mouse.down()
    page.mouse.move(nh["x"] + nh["width"] / 2, ty, steps=6)
    g = guides(page)
    page.mouse.up()
    bath = draft_room(page, "bath")
    assert (bath["y"], bath["h"]) == (3, 3) and g >= 1, bath
    eh = page.locator(".handle.h-e rect.hit").bounding_box()
    tx, _ = plan_xy(page, 3.93, 0)
    page.mouse.move(eh["x"] + eh["width"] / 2, eh["y"] + eh["height"] / 2)
    page.mouse.down()
    page.mouse.move(tx, eh["y"] + eh["height"] / 2, steps=6)
    page.mouse.up()
    bath = draft_room(page, "bath")
    assert (bath["w"], bath["x"]) == (4, 0), bath


def test_l_cut_corner_snaps(editor):
    page, fresh = editor()
    fresh({"unit": "m", "rooms": [room("living", 0, 6, 6, 4, {"corner": "ne", "w": 1.5, "h": 1.5}), room("store", 4, 5, 3, 2)], "placements": []})
    page.click("[data-room=living]", position={"x": 30, "y": 120})
    ch = page.locator(".handle.h-cut rect.hit").bounding_box()
    cx, cy = plan_xy(page, 4.07, 7.08)
    page.mouse.move(ch["x"] + ch["width"] / 2, ch["y"] + ch["height"] / 2)
    page.mouse.down()
    page.mouse.move(cx, cy, steps=6)
    g = guides(page)
    page.mouse.up()
    cut = draft_room(page, "living")["cut"]
    assert (cut["w"], cut["h"], g) == (2, 1, 2), (cut, g)


def test_shared_wall_drawn_once_with_openings(editor):
    page, fresh = editor()
    fresh({"unit": "m", "rooms": [room("lounge", 0, 0, 4, 3), room("kitchen", 4, 0, 3, 3)], "placements": [],
           "openings": [{"id": "d1", "type": "door", "x": 4, "y": 1, "len": 0.9, "orient": "v"}]})
    walls = page.evaluate("""[...document.querySelectorAll('#walls line.wall')].map((l) => ({
        x1: +l.getAttribute('x1'), y1: +l.getAttribute('y1'), x2: +l.getAttribute('x2'), y2: +l.getAttribute('y2'), cls: l.getAttribute('class') }))""")
    at4 = [w for w in walls if w["x1"] == 4 and w["x2"] == 4]
    assert len(at4) == 1 and "inner" in at4[0]["cls"] and (at4[0]["y1"], at4[0]["y2"]) == (0, 3), at4
    assert len(walls) == 5
    assert page.evaluate("""parseFloat(getComputedStyle(document.querySelector('.wall.outer')).strokeWidth)
        > parseFloat(getComputedStyle(document.querySelector('.wall.inner')).strokeWidth)""")
    page.click("#addWindow")
    page.mouse.click(*plan_xy(page, 4.03, 2.3))
    ops = page.evaluate("st.draft.openings.map((o) => ({ ...o }))")
    assert len(ops) == 2 and ops[1]["orient"] == "v" and ops[1]["x"] == 4, ops
    assert page.locator(".opening.door .gap").count() == 1 and page.locator(".opening.door .swing").count() == 1
    assert page.locator(".opening.window .pane").count() == 2
    page.mouse.click(10, 10)  # deselect (off the plan)
    page.mouse.click(*plan_xy(page, 4, 1.45))
    assert page.evaluate("st.sel?.type === 'open' && st.sel.id === 'd1'")
    expect(page.locator("#linkSensor")).to_be_visible()
    save(page)
    assert page.locator(".opening.door .swing").count() == 1 and page.locator(".opening.window .pane").count() == 2


def test_tidy_up_keep_undo_cancel(editor, stack):
    page, fresh = editor()
    fresh({"unit": "m", "rooms": [room("lounge", 0, 0, 5, 4), room("kitchen", 5.15, 0, 3, 4)],
           "placements": [{"entity_id": "light.kitchen", "x": 6.5, "y": 2}]})
    page.click("#tidyUp")
    assert page.locator(".tidy-ghost").count() == 1
    expect(page.locator("#tidyUndo")).to_be_visible()
    expect(page.locator("#tidyUp")).to_have_text("Keep")
    assert draft_room(page, "kitchen")["x"] == 5
    page.click("#tidyUndo")
    assert draft_room(page, "kitchen")["x"] == 5.15
    expect(page.locator("#tidyUndo")).to_be_hidden()
    page.click("#tidyUp"); page.click("#tidyUp")  # tidy, keep
    assert page.locator(".tidy-ghost").count() == 0
    assert room_of(get_layout(page, stack), "kitchen")["x"] == 5.15, "not saved until Save"
    save(page)
    L = get_layout(page, stack)
    assert room_of(L, "kitchen")["x"] == 5 and near(L["placements"][0]["x"], 6.35), L
    page.click("#editToggle"); page.click("#tidyUp")
    expect(page.locator("#status")).to_contain_text("Nothing to tidy")
    page.click("#cancelEdit")
    # Cancel discards a tidy.
    fresh({"unit": "m", "rooms": [room("lounge", 0, 0, 5, 4), room("kitchen", 5.15, 0, 3, 4)], "placements": []})
    page.click("#tidyUp"); page.click("#cancelEdit")
    assert room_of(get_layout(page, stack), "kitchen")["x"] == 5.15
    assert page.evaluate("st.layout.rooms[1].x") == 5.15


def test_tidy_up_l_shaped_flat(editor, stack):
    page, fresh = editor()
    rooms = [room("Living room", 0, 0, 6.2, 4.8, {"corner": "se", "w": 2.2, "h": 1.6}), room("Kitchen", 6.33, 0, 3.4, 3.1),
             room("Hall", 4.1, 4.95, 2.1, 3.0), room("Bathroom", 6.25, 3.2, 2.4, 2.2), room("Bedroom", 0, 4.9, 3.95, 3.5),
             room("Study", 8.75, 3.0, 2.7, 3.2, {"corner": "sw", "w": 0.9, "h": 1.0})]
    for r in rooms:
        r["id"] = r["id"].lower().replace(" ", "")
    flat = {"unit": "m", "rooms": rooms,
            "placements": [{"entity_id": "light.lounge", "x": 2, "y": 2}, {"entity_id": "light.kitchen", "x": 8, "y": 1.5},
                           {"entity_id": "switch.tv", "x": 4.5, "y": 0.8}, {"entity_id": "climate.lounge_valve", "x": 0.6, "y": 3.8},
                           {"entity_id": "binary_sensor.contact_sensor_door", "x": 5.1, "y": 7.5}],
            "openings": [{"id": "o1", "type": "door", "x": 4.6, "y": 7.95, "len": 0.9, "orient": "h"},
                         {"id": "o2", "type": "window", "x": 1, "y": 0, "len": 1.6, "orient": "h"},
                         {"id": "o3", "type": "window", "x": 7.2, "y": 0, "len": 1.2, "orient": "h"},
                         {"id": "o4", "type": "door", "x": 6.2, "y": 1.2, "len": 0.8, "orient": "v"},
                         {"id": "o5", "type": "door", "x": 1.5, "y": 4.8, "len": 0.8, "orient": "h"},
                         {"id": "o6", "type": "window", "x": 0, "y": 5.8, "len": 1.4, "orient": "v"}]}
    fresh(flat)
    page.click("#cancelEdit")
    shot(page, "flat-before")
    page.click("#editToggle"); page.click("#tidyUp")
    shot(page, "flat-tidy-preview")
    page.click("#tidyUp")
    save(page)
    shot(page, "flat-after")
    L = get_layout(page, stack)
    kitchen, hall, bed = room_of(L, "kitchen"), room_of(L, "hall"), room_of(L, "bedroom")
    assert kitchen["x"] == 6.2 and bed["y"] == 4.8 and hall["x"] == bed["x"] + bed["w"], L["rooms"]


def test_phone_touch_snap_draw_tidy(editor, stack):
    page, fresh = editor("mobile")
    t = Touch(page)
    fresh({"unit": "m", "rooms": [room("lounge", 0, 0, 4, 3), room("bed", 8, 0.5, 3, 3)], "placements": []})
    wrap = page.evaluate("(() => { const b = document.getElementById('editbar'); return [b.scrollWidth, b.clientWidth]; })()")
    assert wrap[0] <= wrap[1], f"edit toolbar overflows: {wrap}"
    shot(page, "phone-edit")
    g = t.drag(*plan_xy(page, 9, 1.5), lambda: plan_xy(page, 9 - 3.8, 1.5 - 0.4), during=lambda: guides(page))
    b = draft_room(page, "bed")
    assert (b["x"], b["y"]) == (4, 0) and g >= 1, (b, g)
    assert guides(page) == 0
    page.click("#addRoom")
    t.drag(*plan_xy(page, 0.1, 3.15), lambda: plan_xy(page, 3.8, 5.5))
    name_room(page, "Bath")
    save(page)
    bath = next(r for r in get_layout(page, stack)["rooms"] if r["name"] == "Bath")
    assert (bath["x"], bath["y"]) == (0, 3) and near(bath["x"] + bath["w"], 4), bath
    fresh({"unit": "m", "rooms": [room("lounge", 0, 0, 4, 3), room("bed", 4.15, 0, 3, 3)], "placements": []})
    page.tap("#tidyUp"); page.tap("#tidyUp")
    save(page)
    assert room_of(get_layout(page, stack), "bed")["x"] == 4
    shot(page, "phone-view")
