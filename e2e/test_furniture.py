"""Furniture end to end: catalogue, drag (grid + flush to walls), rotate, resize, duplicate, delete, save, rooms carrying
their furniture, view mode never blocking taps, the Show furniture toggle, export. Mouse on a desktop, CDP touch on a
390px phone."""
import copy
import json

import pytest
from playwright.sync_api import expect

from conftest import LAYOUT, Touch, center, marker_center, plan_xy, shot


def fur(id, type, x, y, w, h, rot=0, label=None):
    return {"id": id, "type": type, "x": x, "y": y, "w": w, "h": h, "rot": rot, **({"label": label} if label else {})}


FURNISHED = {**copy.deepcopy(LAYOUT), "furniture": [
    # lounge
    fur("rug", "rug", 2.4, 2.2, 2.0, 1.4, 90),
    fur("sofa", "sofa", 0.45, 2.2, 2.0, 0.9, 270),
    fur("tv", "unit", 4.975, 2.2, 1.6, 0.45, 90),
    fur("coffee", "coffee_table", 2.4, 2.2, 1.0, 0.55, 90),
    fur("arm", "armchair", 3.55, 0.5, 0.85, 0.85, 0, "Reading chair"),
    fur("books", "bookcase", 3.9, 4.05, 0.8, 0.3, 180),
    fur("plant", "plant", 4.8, 0.4, 0.45, 0.45),
    # kitchen
    fur("counter", "counter", 5.8, 0.3, 1.2, 0.6),
    fur("ksink", "kitchen_sink", 6.8, 0.3, 0.8, 0.6),
    fur("hob", "hob", 7.5, 0.3, 0.6, 0.6),
    fur("fridge", "fridge", 8.1, 0.325, 0.6, 0.65),
    fur("washer", "washer", 8.1, 1.0, 0.6, 0.6, 90),
    fur("table", "dining_table", 6.4, 1.9, 1.1, 0.7),
    fur("chair1", "chair", 5.6, 1.9, 0.45, 0.5, 90),
    fur("chair2", "chair", 7.2, 1.9, 0.45, 0.5, 270),
    # bedroom
    fur("bed", "bed", 2.2, 5.2, 1.4, 2.0),
    fur("bs1", "bedside", 1.25, 4.4, 0.45, 0.4),
    fur("bs2", "bedside", 3.15, 4.4, 0.45, 0.4),
    fur("wardrobe", "wardrobe", 3.7, 7.3, 1.0, 0.6, 180),
    fur("desk", "desk", 1.4, 7.3, 1.2, 0.6, 180),
    fur("dchair", "desk_chair", 1.4, 6.65, 0.6, 0.6, 180),
    # bathroom
    fur("bath", "bathtub", 5.25, 4.575, 1.7, 0.75),
    fur("wc", "toilet", 6.475, 5.6, 0.4, 0.65, 90),
    fur("basin", "sink", 5.1, 6.175, 0.6, 0.45, 180),
]}

START = {"unit": "m", "rooms": [{"id": "lounge", "name": "Lounge", "x": 0, "y": 0, "w": 5, "h": 4},
                                {"id": "bed", "name": "Bedroom", "x": 5, "y": 0, "w": 4, "h": 4}],
         "placements": [{"entity_id": "light.lounge", "x": 2.5, "y": 2}], "openings": []}


class Pointer:
    """Mouse or touch, same calls."""

    def __init__(self, page):
        self.page, self.touch = page, Touch(page) if page.size in ("mobile", "phone") else None

    def tap(self, x, y):
        if self.touch:
            self.touch.tap(x, y)
        else:
            self.page.mouse.click(x, y)

    def drag(self, x0, y0, x1, y1):
        if self.touch:
            # The finger comes to rest before lifting. A flick (CDP sends the moves back to back) starts a fling,
            # and Chrome swallows the next tap as "stop the fling": the following button tap would never click.
            t = self.touch
            t._send("touchStart", [(x0, y0)])
            for i in range(1, 11):
                t._send("touchMove", [(x0 + (x1 - x0) * i / 10, y0 + (y1 - y0) * i / 10)])
            self.page.wait_for_timeout(150)
            t._send("touchMove", [(x1, y1)])
            t._send("touchEnd", [])
            return None
        m = self.page.mouse
        m.move(x0, y0)
        m.down()
        m.move((x0 + x1) / 2, (y0 + y1) / 2, steps=4)
        m.move(x1, y1, steps=6)
        m.up()

    def button(self, sel):
        (self.page.tap if self.touch else self.page.click)(sel)


def pieces(page):
    return page.evaluate("st.draft.furniture || []")


def piece(page, id):
    return page.evaluate("id => st.draft.furniture.find((f) => f.id === id)", id)


def selected(page):
    return page.evaluate("st.sel && st.sel.type === 'fur' ? st.draft.furniture.find((f) => f.id === st.sel.id) : null")


def add_from_catalogue(page, p, type):
    n = len(pieces(page))
    p.button("#addFurniture")
    expect(page.locator("#furSheet")).to_be_visible()
    p.button(f'#furGrid .fur-item[data-type="{type}"]')
    expect(page.locator("#furSheet")).to_be_hidden()
    page.wait_for_function("n => (st.draft.furniture || []).length === n + 1", arg=n)
    f = selected(page)
    assert f and f["type"] == type, f
    return f


def wait_rot(page, id, rot):
    """A tapped button's click lands a moment after the touch: wait for its effect."""
    page.wait_for_function("([id, rot]) => st.draft.furniture.find((f) => f.id === id).rot === rot", arg=[id, rot])


def drag_plan(page, p, a, b):
    """Drag from plan point a to plan point b (metres)."""
    x0, y0 = plan_xy(page, *a)
    x1, y1 = plan_xy(page, *b)
    p.drag(x0, y0, x1, y1)


def near(a, b, t=1e-6):
    return abs(a - b) <= t


@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_add_move_rotate_resize_duplicate_delete_save(stack, ha, open_page, size):
    page = open_page(stack, size, layout=START)
    p = Pointer(page)
    p.button("#editToggle")
    expect(page.locator("#editbar")).to_be_visible()
    expect(page.locator("#addFurniture")).to_be_visible()
    expect(page.locator("#furRot")).to_be_hidden()

    # A double bed with its real size, in the middle of the plan, selected with its handles.
    bed = add_from_catalogue(page, p, "bed")
    assert (bed["w"], bed["h"], bed["rot"]) == (1.4, 2.0, 0), bed
    expect(page.locator(".fur-handles .fh")).to_have_count(5)
    expect(page.locator("#furRot")).to_be_visible()

    # Drag it so its west edge lands 8 cm from the lounge's west wall (grid would say 10 cm): it goes flush.
    x0, y0 = bed["x"] - 0.7, bed["y"] - 1.0
    drag_plan(page, p, (bed["x"], bed["y"]), (bed["x"] - x0 + 0.08, bed["y"] - y0 + 0.5))
    bed = piece(page, bed["id"])
    assert near(bed["x"] - 0.7, 0) and near(bed["y"] - 1.0, 0.5, 0.051), bed
    # Alt: grid only, 5 cm steps.
    if size == "desktop":
        page.keyboard.down("Alt")
        drag_plan(page, p, (bed["x"], bed["y"]), (bed["x"] + 0.12, bed["y"]))  # without Alt: back to the wall
        page.keyboard.up("Alt")
        assert near(piece(page, bed["id"])["x"] - 0.7, 0.1), piece(page, bed["id"])
        drag_plan(page, p, (bed["x"] + 0.1, bed["y"]), (bed["x"], bed["y"]))
        assert near(piece(page, bed["id"])["x"] - 0.7, 0), piece(page, bed["id"])

    # Rotate 90° with the button: the bed now lies across.
    for rot in (90, 180, 270, 0):
        p.button("#furRot")
        wait_rot(page, bed["id"], rot)
        wait_rot(page, bed["id"], rot)

    # The user's list, each from the catalogue with its default size.
    sizes = {}
    for t in ["sofa", "desk", "bedside", "unit"]:
        f = add_from_catalogue(page, p, t)
        sizes[t] = (f["w"], f["h"])
        # Move it into the bedroom, apart from the rest.
        dest = {"sofa": (7, 1), "desk": (7, 3), "bedside": (5.6, 2.2), "unit": (2.8, 3.5)}[t]
        drag_plan(page, p, (f["x"], f["y"]), dest)
    assert sizes == {"sofa": (2.0, 0.9), "desk": (1.2, 0.6), "bedside": (0.45, 0.4), "unit": (1.6, 0.45)}, sizes
    assert sorted(f["type"] for f in pieces(page)) == ["bed", "bedside", "desk", "sofa", "unit"]

    # Resize the desk (selected by tapping it) from its south-east corner: the north-west corner stays put.
    desk = next(f for f in pieces(page) if f["type"] == "desk")
    p.tap(*plan_xy(page, desk["x"], desk["y"]))
    assert selected(page)["id"] == desk["id"]
    hx, hy = center(page.locator(".fh-se rect:not(.hit)"))
    k = plan_xy(page, 1, 0)[0] - plan_xy(page, 0, 0)[0]  # px per metre
    p.drag(hx, hy, hx + 0.3 * k, hy + 0.1 * k)
    d2 = piece(page, desk["id"])
    assert (d2["w"], d2["h"]) == (1.5, 0.7), d2
    assert near(d2["x"] - d2["w"] / 2, desk["x"] - 0.6, 1e-3) and near(d2["y"] - d2["h"] / 2, desk["y"] - 0.3, 1e-3), d2

    # Rotate with the handle: drag it round to the right of the centre -> 90°.
    rx, ry = center(page.locator(".fh-rot circle:not(.hit)"))
    cx, cy = plan_xy(page, d2["x"], d2["y"])
    p.drag(rx, ry, cx + 2 * k, cy + 0.05 * k)
    assert piece(page, desk["id"])["rot"] == 90, piece(page, desk["id"])

    # Duplicate, then delete the copy.
    p.button("#furDup")
    page.wait_for_function("st.draft.furniture.length === 6")
    copy_ = selected(page)
    assert copy_["id"] != desk["id"] and (copy_["type"], copy_["w"], copy_["h"], copy_["rot"]) == ("desk", 1.5, 0.7, 90)
    assert len(pieces(page)) == 6
    p.button("#deleteSel")
    page.wait_for_function("st.draft.furniture.length === 5")
    assert len(pieces(page)) == 5 and not any(f["id"] == copy_["id"] for f in pieces(page))

    # Label via the details dialog (desk selected again by tapping it).
    expect(page.locator("#furEdit")).to_be_hidden()
    p.tap(*plan_xy(page, d2["x"], d2["y"]))
    p.button("#furEdit")
    expect(page.locator("#furDialog")).to_be_visible()
    page.fill("#furForm [name=label]", "  Work   desk ")
    page.click("#furDialog button[value=ok]")
    page.wait_for_function("id => st.draft.furniture.find((f) => f.id === id).label === 'Work desk'", arg=desk["id"])
    expect(page.locator("#furniture .fu-label")).to_have_text("Work desk")

    shot(page, f"furniture-edit-{size}")
    if size == "mobile":
        assert page.evaluate("document.scrollingElement.scrollWidth <= innerWidth"), "no horizontal scroll at 390px"
    draft = pieces(page)
    p.button("#save")
    expect(page.locator("#editbar")).to_be_hidden()
    expect(page.locator("#status")).to_have_text("Saved")
    saved = page.request.get(stack.url + "/api/layout").json()["furniture"]
    assert saved == draft, (saved, draft)
    # View mode draws them too.
    expect(page.locator("#furniture .fur")).to_have_count(5)


def test_room_move_carries_furniture_and_cancel(stack, ha, open_page):
    layout = {**START, "furniture": [fur("b", "bed", 7, 1.5, 1.4, 2.0), fur("s", "sofa", 2, 1, 2.0, 0.9)]}
    page = open_page(stack, layout=layout)
    page.click("#editToggle")
    expect(page.locator("#editbar")).to_be_visible()
    p = Pointer(page)
    # Drag the bedroom by an empty corner, 1 m down.
    drag_plan(page, p, (8.7, 3.7), (8.7, 4.7))
    b, s = piece(page, "b"), piece(page, "s")
    assert (b["x"], b["y"]) == (7, 2.5), b
    assert (s["x"], s["y"]) == (2, 1), s  # the lounge's sofa stays
    page.click("#cancelEdit")
    saved = page.request.get(stack.url + "/api/layout").json()["furniture"]
    assert next(f for f in saved if f["id"] == "b")["y"] == 1.5


@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_view_mode_taps_go_through_furniture(stack, ha, open_page, size):
    page = open_page(stack, size, layout=FURNISHED)
    expect(page.locator("#furniture .fur")).to_have_count(len(FURNISHED["furniture"]))
    # The bedroom light sits on the bed: tapping it toggles the light.
    x, y = marker_center(page, "light.bedroom")
    hit = page.evaluate("([x, y]) => !!document.elementFromPoint(x, y).closest('.marker')", [x, y])
    assert hit
    if size == "mobile":
        page.touchscreen.tap(x, y)
    else:
        page.mouse.click(x, y)
    ha.wait_call(lambda c: c["service"] in ("toggle", "turn_on", "turn_off") and "light.bedroom" in json.dumps(c["data"]))
    # Furniture never takes a tap in view mode: the middle of the sofa is the room underneath.
    sx, sy = plan_xy(page, 0.45, 2.6)
    assert page.evaluate("([x, y]) => !document.elementFromPoint(x, y).closest('#furniture')", [sx, sy])
    shot(page, f"furniture-view-{size}")
    assert page.evaluate("document.scrollingElement.scrollWidth <= innerWidth")


def test_room_name_tap_through_furniture(stack, ha, open_page):
    # A wardrobe right over the bedroom's name: tapping the name still switches the room's lights.
    layout = {**copy.deepcopy(LAYOUT), "furniture": [fur("w", "wardrobe", 0.7, 4.6, 1.4, 0.8)]}
    page = open_page(stack, layout=layout)
    tap = page.locator('[data-roomtap="bed"]')
    x, y = center(tap)
    page.mouse.click(x, y)
    ha.wait_call(lambda c: "light.bedroom" in json.dumps(c["data"]))


def test_show_furniture_toggle(stack, ha, open_page):
    page = open_page(stack, layout=FURNISHED)
    n = len(FURNISHED["furniture"])
    expect(page.locator("#furniture .fur")).to_have_count(n)
    page.click("#moreBtn")
    box = page.locator("#furToggle")
    expect(box).to_be_checked()
    box.uncheck()
    expect(page.locator("#furniture .fur")).to_have_count(0)
    page.reload()
    page.locator("#plan .marker").first.wait_for()
    expect(page.locator("#furniture .fur")).to_have_count(0)
    # Edit mode always shows it (it's what you're editing).
    page.click("#editToggle")
    expect(page.locator("#furniture .fur")).to_have_count(n)
    page.click("#cancelEdit")
    expect(page.locator("#furniture .fur")).to_have_count(0)
    page.click("#moreBtn")
    page.locator("#furToggle").check()
    expect(page.locator("#furniture .fur")).to_have_count(n)
    # Wall mode shows it too, subtly.
    page.goto(stack.url + "/?wall")
    page.locator("#plan .marker").first.wait_for()
    expect(page.locator("#furniture .fur")).to_have_count(n)
    assert float(page.evaluate("getComputedStyle(document.getElementById('furniture')).opacity")) < 1


def test_catalogue_drag_onto_plan_and_phone_layout(stack, ha, open_page):
    page = open_page(stack, "phone", layout=START)
    page.tap("#editToggle")
    page.tap("#addFurniture")
    expect(page.locator("#furSheet")).to_be_visible()
    assert page.locator("#furGrid .fur-item").count() >= 20
    shot(page, "furniture-catalogue-phone")
    assert page.evaluate("document.scrollingElement.scrollWidth <= innerWidth")
    # Every thumbnail fits its button.
    assert page.evaluate("""[...document.querySelectorAll('#furGrid .fur-item')].every((b) => b.scrollWidth <= b.clientWidth + 1)""")
    page.click("#furClose")
    expect(page.locator("#furSheet")).to_be_hidden()
    page.set_viewport_size({"width": 1280, "height": 800})
    page.click("#addFurniture")
    item = page.locator('#furGrid .fur-item[data-type="plant"]')
    x, y = center(item)
    tx, ty = plan_xy(page, 1, 1)
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 20, y, steps=3)
    expect(page.locator("#furSheet")).to_be_hidden()
    tx, ty = plan_xy(page, 1, 1)
    page.mouse.move(tx, ty, steps=6)
    page.mouse.up()
    page.wait_for_function("(st.draft.furniture || []).length === 1")
    f = pieces(page)[0]
    assert f["type"] == "plant" and near(f["x"], 1, 0.06) and near(f["y"], 1, 0.06), f


def test_export_carries_furniture(stack, ha, open_page):
    page = open_page(stack, layout=FURNISHED)
    page.click("#moreBtn")
    with page.expect_download() as dl:
        page.click("#exportLayout")
    data = json.loads(open(dl.value.path()).read())
    assert data["furniture"] == FURNISHED["furniture"]


def test_import_keeps_furniture(stack, ha, open_page, tmp_path):
    page = open_page(stack, layout=START)
    data = {**copy.deepcopy(FURNISHED), "placements": FURNISHED["placements"] + [{"entity_id": "light.gone", "x": 1, "y": 1}]}
    f = tmp_path / "flat.json"
    f.write_text(json.dumps(data))
    page.click("#moreBtn")
    page.click("#importLayout")
    page.set_input_files("#importFile", str(f))
    expect(page.locator("#importDialog")).to_be_visible()
    expect(page.locator("#importList")).to_contain_text(f"{len(FURNISHED['furniture'])} pieces of furniture")
    page.click("#importOk")
    expect(page.locator("#importStrip")).to_be_visible()  # light.gone is unknown
    page.click("#importStrip")
    expect(page.locator("#importDialog")).to_be_hidden()
    assert page.request.get(stack.url + "/api/layout").json()["furniture"] == FURNISHED["furniture"]
    expect(page.locator("#furniture .fur")).to_have_count(len(FURNISHED["furniture"]))


@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_screenshot_furnished_flat(stack, ha, open_page, size):
    page = open_page(stack, size, layout=FURNISHED)
    expect(page.locator("#furniture .fur")).to_have_count(len(FURNISHED["furniture"]))
    shot(page, f"furnished-{size}")
    page.click("#editToggle") if size == "desktop" else page.tap("#editToggle")
    expect(page.locator("#editbar")).to_be_visible()
    p = Pointer(page)
    p.tap(*plan_xy(page, 2.2, 4.9))
    expect(page.locator(".fur-handles")).to_have_count(1)
    shot(page, f"furnished-edit-{size}")
