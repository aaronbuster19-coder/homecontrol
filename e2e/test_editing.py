"""Floor plan editing end to end: draw a room, resize it, make it L-shaped, add a door and a window, link the window
to a contact sensor and watch its panes follow the sensor. Mouse on a desktop, CDP touch on a phone."""
import re

import pytest
from playwright.sync_api import expect

from conftest import Touch, center, plan_xy, shot

START = {"unit": "m", "rooms": [{"id": "lounge", "name": "Lounge", "x": 0, "y": 0, "w": 5, "h": 4}],
         "placements": [{"entity_id": "light.lounge", "x": 2, "y": 2}], "openings": []}


class Pointer:
    """Mouse or touch, same calls."""

    def __init__(self, page):
        self.page, self.touch = page, Touch(page) if page.size == "mobile" else None

    def tap(self, x, y):
        if self.touch:
            self.touch.tap(x, y)
        else:
            self.page.mouse.click(x, y)

    def drag(self, x0, y0, end):
        if self.touch:
            return self.touch.drag(x0, y0, end)
        m = self.page.mouse
        m.move(x0, y0)
        m.down()
        x1, y1 = end()
        m.move((x0 + x1) / 2, (y0 + y1) / 2, steps=4)
        m.move(x1, y1, steps=6)
        m.up()

    def button(self, sel):
        (self.page.tap if self.touch else self.page.click)(sel)


def draft(page, id=None):
    return page.evaluate("id => id ? st.draft.rooms.find((r) => r.id === id) : st.draft", id)


@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_draw_resize_l_shape_openings_and_link_sensor(stack, ha, open_page, size):
    page = open_page(stack, size, layout=START)
    p = Pointer(page)
    p.button("#editToggle")
    expect(page.locator("#editbar")).to_be_visible()

    # Draw a 3 x 3 m room well away from the lounge (no snapping).
    p.button("#addRoom")
    expect(page.locator("#addRoom")).to_have_text("Drag on the plan…")
    p.drag(*plan_xy(page, 7, 0), lambda: plan_xy(page, 10, 3))
    expect(page.locator("#roomDialog")).to_be_visible()
    expect(page.locator("#roomForm [name=w]")).to_have_value("3.0")
    page.fill("#roomForm [name=name]", "Study")
    page.click("#roomDialog button[value=ok]")
    study = next(r for r in draft(page)["rooms"] if r["name"] == "Study")
    sid = study["id"]
    assert (study["x"], study["y"], study["w"], study["h"]) == (7, 0, 3, 3), study

    # Selected after drawing: drag the east handle one metre to the right.
    hd = page.locator(".handle.h-e rect.hit")
    x, y = center(hd)
    p.drag(x, y, lambda: (x + plan_xy(page, 11, 0)[0] - plan_xy(page, 10, 0)[0], y))
    assert draft(page, sid)["w"] == 4, draft(page, sid)

    # L-shape: NE first, again for SE.
    p.button("#lShape")
    expect(page.locator("#lShape")).to_have_text("L: NE ↻")
    p.button("#lShape")
    expect(page.locator("#lShape")).to_have_text("L: SE ↻")
    assert draft(page, sid)["cut"] == {"corner": "se", "w": 2, "h": 1.5}
    expect(page.locator(f'[data-room="{sid}"] polygon')).to_have_count(1)

    # A door on the study's west wall, a window on its north wall.
    p.button("#addDoor")
    expect(page.locator("#status")).to_have_text("Tap a wall to add a door")
    p.tap(*plan_xy(page, 7.03, 1.0))
    p.button("#addWindow")
    p.tap(*plan_xy(page, 8.0, 0.03))
    ops = draft(page)["openings"]
    assert [(o["type"], o["orient"], o["x"], o["y"], o["len"]) for o in ops] == [
        ("door", "v", 7, 0.55, 0.9), ("window", "h", 7.4, 0, 1.2)], ops
    expect(page.locator(".opening.window .pane")).to_have_count(2)
    expect(page.locator(".opening.door .swing")).to_have_count(1)

    # The new window is selected: link it to the bedroom window sensor.
    expect(page.locator("#linkSensor")).to_be_visible()
    p.button("#linkSensor")
    expect(page.locator("#linkDialog")).to_be_visible()
    page.select_option("#linkSelect", "binary_sensor.contact_sensor_door_2")
    page.click("#linkDialog button[value=ok]")
    shot(page, f"edit-{size}")
    p.button("#save")
    expect(page.locator("#editbar")).to_be_hidden()
    expect(page.locator("#status")).to_have_text("Saved")

    saved = page.request.get(stack.url + "/api/layout").json()
    s = next(r for r in saved["rooms"] if r["id"] == sid)
    assert (s["name"], s["x"], s["w"], s["h"], s["cut"]) == ("Study", 7, 4, 3, {"corner": "se", "w": 2, "h": 1.5}), s
    assert [o.get("entity_id") for o in saved["openings"]] == [None, "binary_sensor.contact_sensor_door_2"]

    # View mode: the linked window's panes follow the sensor — green closed, red open.
    panes = page.locator(".opening.window .pane")
    expect(panes).to_have_count(2)
    for i in range(2):
        expect(panes.nth(i)).to_have_attribute("style", re.compile(r"stroke: var\(--closed\)"))
    ha.set("binary_sensor.contact_sensor_door_2", "on")
    for i in range(2):
        expect(panes.nth(i)).to_have_attribute("style", re.compile(r"stroke: var\(--open\)"))
    shot(page, f"edit-{size}-window-open")
    ha.set("binary_sensor.contact_sensor_door_2", "off")
    expect(panes.first).to_have_attribute("style", re.compile(r"stroke: var\(--closed\)"))
    # The unlinked door keeps its plain look.
    assert page.locator(".opening.door .swing").get_attribute("style") is None


def test_cancel_discards_edits(stack, ha, open_page):
    page = open_page(stack, layout=START)
    page.click("#editToggle")
    page.click("[data-room=lounge]", position={"x": 40, "y": 40})
    page.click("#lShape")
    page.click("#addDoor")
    page.mouse.click(*plan_xy(page, 0.02, 2))
    page.click("#cancelEdit")
    saved = page.request.get(stack.url + "/api/layout").json()
    assert "cut" not in saved["rooms"][0] and saved.get("openings", []) == []
    expect(page.locator(".opening")).to_have_count(0)
    expect(page.locator("[data-room=lounge] rect")).to_have_count(1)
