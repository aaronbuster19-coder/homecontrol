"""Quick tiles end to end: pin from a device sheet, ⋯ → Favourites / /?view=tiles, tap = the plan's tap (lights and
plugs toggle, valves open their sheet), long-press = sheet, Edit → + Add / reorder / remove (stored on the server),
the fridge's protections on a tile, the manifest shortcut. Desktop mouse and a 390px touch phone, both themes."""
import copy
import json

import pytest
from playwright.sync_api import expect

from conftest import LAYOUT, Touch, center, hold, marker, shot, toggles
from test_appliances import KITCHEN, aha  # noqa: F401  (aha: the appliance stack's fake HA, reset per test)

SIZES = ["desktop", "mobile"]


def tap(page, sel):
    loc = page.locator(sel) if isinstance(sel, str) else sel
    if page.size == "mobile":
        Touch(page).tap(*center(loc))
    else:
        loc.click()


def long_press(page, loc):
    if page.size == "mobile":
        Touch(page).hold(*center(loc), ms=700)
    else:
        hold(page, loc, 700)


def set_pins(page, base, pins):
    r = page.request.put(base + "/api/tiles", data=json.dumps({"pins": pins}), headers={"Content-Type": "application/json"})
    assert r.ok, r.text()


def pins(page, base):
    return page.request.get(base + "/api/tiles").json()["pins"]


def tile(page, eid):
    return page.locator(f'#tilesGrid .tile[data-tile="{eid}"]')


def no_hscroll(page):
    assert page.evaluate("document.scrollingElement.scrollWidth <= innerWidth"), "horizontal scroll"


@pytest.mark.parametrize("scheme", ["dark", "light"])
@pytest.mark.parametrize("size", SIZES)
def test_pin_from_sheets_then_use_tiles(stack, ha, open_page, size, scheme):
    page = open_page(stack, size, color_scheme=scheme, goto=None)
    set_pins(page, stack.url, [])
    page.goto(stack.url + "/")
    marker(page, "light.lounge").wait_for()
    # Light: long-press for its sheet, ☆ Add to favourites.
    long_press(page, marker(page, "light.lounge").locator("circle"))
    expect(page.locator("#tilesPin")).to_have_text("☆ Add to favourites")
    tap(page, "#tilesPin")
    expect(page.locator("#tilesPin")).to_have_text("★ In favourites — remove")
    shot(page, f"tiles-sheet-pinned-{scheme}-{size}")
    tap(page, "#sheetClose")
    # Valve: a tap opens its sheet.
    tap(page, marker(page, "climate.lounge_valve").locator("circle"))
    tap(page, "#tilesPin")
    expect(page.locator("#tilesPin")).to_have_attribute("aria-pressed", "true")
    tap(page, "#sheetClose")
    assert pins(page, stack.url) == ["light.lounge", "climate.lounge_valve"]
    assert toggles(ha, "light.lounge") == []  # pinning never switched anything

    # ⋯ → Favourites
    tap(page, "#moreBtn")
    tap(page, "#tilesBtn")
    expect(page.locator("#tilesView")).to_be_visible()
    expect(page.locator("header")).to_be_hidden()
    assert "view=tiles" in page.url
    expect(page.locator("#tilesGrid .tile")).to_have_count(2)
    expect(page.locator("#tilesGrid .tile .tile-name")).to_have_text(["Lounge lamp", "Lounge radiator"])
    expect(tile(page, "light.lounge")).to_have_class("tile on")
    expect(tile(page, "climate.lounge_valve").locator(".tile-val")).to_contain_text("°")
    expect(page.locator("#tilesStatus")).to_contain_text("devices")
    no_hscroll(page)
    shot(page, f"tiles-view-{scheme}-{size}")

    # Tap the light: toggles straight away, like on the plan.
    tap(page, tile(page, "light.lounge").locator(".tile-main"))
    ha.wait_call(lambda c: c["service"] == "toggle" and c["data"] == {"entity_id": "light.lounge"})
    expect(tile(page, "light.lounge")).to_have_class("tile off")
    # Tap the valve: its sheet, nothing sent.
    n = len(ha.calls())
    tap(page, tile(page, "climate.lounge_valve").locator(".tile-main"))
    expect(page.locator("#sheet")).to_be_visible()
    expect(page.locator("#sheetContent h3")).to_have_text("Lounge radiator")
    shot(page, f"tiles-valve-sheet-{scheme}-{size}")
    tap(page, "#sheetClose")
    expect(page.locator("#sheet")).to_be_hidden()
    # Long-press the light: its sheet, no toggle.
    long_press(page, tile(page, "light.lounge").locator(".tile-main"))
    expect(page.locator("#sheetContent h3")).to_have_text("Lounge lamp")
    page.wait_for_timeout(300)
    assert len(ha.calls()) == n
    # Unpin from the sheet: the tile goes.
    tap(page, "#tilesPin")
    expect(page.locator("#tilesPin")).to_have_text("☆ Add to favourites")
    tap(page, "#sheetClose")
    expect(page.locator("#tilesGrid .tile")).to_have_count(1)
    # Plan: back to the floor plan.
    tap(page, "#tilesExit")
    expect(page.locator("#tilesView")).to_be_hidden()
    expect(page.locator("header")).to_be_visible()
    assert "view=tiles" not in page.url
    marker(page, "light.lounge").wait_for()


@pytest.mark.parametrize("size", SIZES)
def test_edit_add_reorder_remove(stack, ha, open_page, size):
    page = open_page(stack, size, goto=None)
    set_pins(page, stack.url, [])
    page.goto(stack.url + "/?view=tiles")
    expect(page.locator("#tilesEmpty")).to_be_visible()
    shot(page, f"tiles-empty-{size}")
    tap(page, "#tilesAddFirst")
    expect(page.locator("#tilesPick")).to_be_visible()
    for eid in ("light.kitchen", "switch.kettle", "binary_sensor.contact_sensor_door"):
        b = page.locator(f'#tilesPick [data-pick="{eid}"]')
        tap(page, b)
        expect(b).to_have_attribute("aria-pressed", "true")
    shot(page, f"tiles-picker-{size}")
    tap(page, "#tilesPickClose")
    expect(page.locator("#tilesPick")).to_be_hidden()
    expect(page.locator("#tilesGrid .tile[data-tile]")).to_have_count(3)
    expect(page.locator("#tilesAdd")).to_be_visible()
    shot(page, f"tiles-edit-{size}")
    # Move the kettle first, remove the door sensor.
    tap(page, tile(page, "switch.kettle").get_by_role("button", name="Move earlier"))
    expect(page.locator("#tilesGrid .tile[data-tile]").first).to_have_attribute("data-tile", "switch.kettle")
    tap(page, tile(page, "binary_sensor.contact_sensor_door").get_by_role("button", name="Remove Front door"))
    expect(page.locator("#tilesGrid .tile[data-tile]")).to_have_count(2)
    # Taps in edit mode don't switch anything.
    tap(page, tile(page, "light.kitchen").locator(".tile-name"))
    tap(page, "#tilesEdit")
    expect(page.locator("#tilesEdit")).to_have_text("Edit")
    expect(page.locator("#tilesAdd")).to_have_count(0)
    page.wait_for_timeout(300)
    assert toggles(ha, "light.kitchen") == []
    assert pins(page, stack.url) == ["switch.kettle", "light.kitchen"]
    # Stored on the server: a reload (or another device) shows the same tiles.
    page.reload()
    expect(page.locator("#tilesGrid .tile .tile-name")).to_have_text(["Kettle", "Kitchen"])
    no_hscroll(page)


@pytest.mark.parametrize("size", SIZES)
def test_fridge_tile_keeps_its_protection(appliance_stack, aha, open_page, size):  # noqa: F811
    layout = {**copy.deepcopy(LAYOUT), "furniture": KITCHEN, "settings": {"keep_on": ["switch.kettle"]}}
    page = open_page(appliance_stack, size, layout=layout, goto=None)
    aha.set("switch.kettle", "on")
    set_pins(page, appliance_stack.url, ["switch.fridge", "switch.kettle", "switch.plug_3"])
    page.goto(appliance_stack.url + "/?view=tiles")
    fridge = tile(page, "switch.fridge")
    expect(fridge.locator(".tile-name")).to_have_text("Fridge")  # the appliance's name and drawing
    expect(fridge.locator(".tile-ic.appl svg")).to_have_count(1)
    shot(page, f"tiles-appliances-{size}")
    for eid, q in (("switch.fridge", "Turn off the fridge?"), ("switch.kettle", "Turn off the kettle?")):
        # A tap opens the sheet (fridge; keep-on plug), and switching off there asks first.
        tap(page, tile(page, eid).locator(".tile-main"))
        expect(page.locator("#sheet")).to_be_visible()
        seen = []
        page.once("dialog", lambda d: (seen.append(d.message), d.dismiss()))
        tap(page, "#sheetContent button.big")
        page.wait_for_timeout(400)
        assert seen == [q] and toggles(aha, eid) == []
        tap(page, "#sheetClose")
    # An ordinary appliance plug (the fan) toggles on a tap.
    tap(page, tile(page, "switch.plug_3").locator(".tile-main"))
    aha.wait_call(lambda c: c["service"] == "toggle" and c["data"] == {"entity_id": "switch.plug_3"})


def test_manifest_shortcut_and_shell(stack, open_page):
    page = open_page(stack, "desktop", goto=None)
    m = page.request.get(stack.url + "/manifest.webmanifest").json()
    assert {"name": "Favourites", "url": "/?view=tiles"}.items() <= m["shortcuts"][0].items()
    sw = page.request.get(stack.url + "/sw.js").text()
    assert '"/tiles.js"' in sw and '"/tiles.css"' in sw
