"""Tapping devices, the light sheet, room-name taps, All off and the heating sheet — with the exact HA calls."""
import pytest
from playwright.sync_api import expect

from conftest import LAYOUT, Touch, center, hold, marker, marker_center, shot, toggles


def tap(page, x, y):
    if page.size == "mobile":
        Touch(page).tap(x, y)
    else:
        page.mouse.click(x, y)


def long_press(page, locator):
    if page.size == "mobile":
        Touch(page).hold(*center(locator), ms=700)
    else:
        hold(page, locator, 700)


@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_single_tap_toggles_light_and_plug(stack, ha, open_page, size):
    page = open_page(stack, size)
    tap(page, *marker_center(page, "light.kitchen"))
    ha.wait_call(lambda c: c["service"] == "toggle")
    tap(page, *marker_center(page, "switch.kettle"))
    calls = ha.wait_calls(2)
    assert [(c["domain"], c["service"], c["data"]) for c in calls] == [
        ("light", "toggle", {"entity_id": "light.kitchen"}),
        ("switch", "toggle", {"entity_id": "switch.kettle"}),
    ]
    expect(marker(page, "light.kitchen").locator("circle").first).to_have_attribute("fill", "var(--on)")
    expect(marker(page, "switch.kettle").locator("circle").first).to_have_attribute("fill", "var(--on)")
    # A valve opens its sheet instead.
    tap(page, *marker_center(page, "climate.lounge_valve"))
    expect(page.locator("#sheetContent h3")).to_have_text("Lounge radiator")
    assert len(ha.calls()) == 2


@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_long_press_opens_light_sheet_without_toggling(stack, ha, open_page, size):
    page = open_page(stack, size)
    long_press(page, marker(page, "light.lounge").locator("circle").first)
    expect(page.locator("#sheet")).to_be_visible()
    expect(page.locator("#sheetContent h3")).to_have_text("Lounge lamp")
    expect(page.locator("#sheetContent .ctl-top >> text=Brightness")).to_be_visible()
    expect(page.locator("#sheetContent .kelvin")).to_be_visible()
    expect(page.locator("#sheetContent .swatch")).to_have_count(8)
    shot(page, f"light-sheet-{size}")
    page.wait_for_timeout(600)
    assert toggles(ha, "light.lounge") == []
    page.click("#sheetClose")
    expect(page.locator("#sheet")).to_be_hidden()
    # A list row works the same way.
    long_press(page, page.locator('#list li[data-dev="light.kitchen"]'))
    expect(page.locator("#sheetContent h3")).to_have_text("Kitchen")
    page.wait_for_timeout(600)
    assert ha.calls() == []


def test_light_sheet_brightness_colour_kelvin(stack, ha, open_page):
    page = open_page(stack)
    hold(page, marker(page, "light.lounge").locator("circle").first, 700)
    rng = page.locator("#sheetContent .light-ctl label.ctl").first.locator("input[type=range]")
    page.evaluate("window.__inputs = 0")
    # Time the sends where the browser makes them: on a busy CI host the requests can reach the fake HA bunched up.
    page.evaluate("""() => { window.__sends = []; const f = window.fetch;
      window.fetch = (u, o) => { if (String(u).endsWith('/light') && o?.body?.includes('brightness_pct'))
        window.__sends.push({t: performance.now() / 1000, v: JSON.parse(o.body).brightness_pct}); return f(u, o); }; }""")
    rng.evaluate("r => r.addEventListener('input', () => window.__inputs++)")
    # Drag the brightness slider from left to right for about a second.
    b = rng.bounding_box()
    y = b["y"] + b["height"] / 2
    page.mouse.move(b["x"] + 4, y)
    page.mouse.down()
    for i in range(1, 31):
        page.mouse.move(b["x"] + 4 + (b["width"] - 8) * i / 30, y)
        page.wait_for_timeout(30)
    page.mouse.up()
    inputs = page.evaluate("window.__inputs")
    final = int(rng.input_value())
    assert final == 100 and inputs >= 15, (final, inputs)
    sends = page.evaluate("window.__sends")
    sent = [x["v"] for x in sends]
    # Every send reaches HA before moving on, so a late one can't spill into the next test.
    bright = ha.wait_calls(len(sends), "turn_on")
    assert [c["data"]["brightness_pct"] for c in bright if "brightness_pct" in c["data"]] == sent
    # Throttled: far fewer calls than slider events, at most one per ~300 ms over the drag, plus the final value on release.
    span = sends[-1]["t"] - sends[0]["t"]
    assert 2 <= len(sent) <= span / 0.3 + 2 and len(sent) < inputs, f"{inputs} slider events -> {len(sent)} calls over {span:.2f}s: {sent}"
    gaps = [b["t"] - a["t"] for a, b in zip(sends, sends[1:])]
    # Throttled sends are ~300 ms apart; the last is the final value on release and may follow the previous one at once.
    assert sorted(gaps)[len(gaps) // 2] >= 0.25 and min(gaps[:-1] or [1]) > 0.2, gaps
    assert sent == sorted(sent) and sent[-1] == 100
    assert all(c["domain"] == "light" and c["data"]["entity_id"] == "light.lounge" for c in ha.calls())
    expect(page.locator("#sheetContent .ctl-val").first).to_have_text("100 %")

    # Colour swatch -> hs_color, and the marker takes that colour.
    n = len(ha.calls())
    page.click('#sheetContent .swatch[aria-label="Colour hue 120"]')
    calls = ha.wait_calls(n + 1)
    assert calls[n:] == [{**calls[n], "domain": "light", "service": "turn_on", "data": {"entity_id": "light.lounge", "hs_color": [120, 90]}}]
    expect(marker(page, "light.lounge").locator("circle").first).to_have_attribute("fill", "rgb(25,255,25)")

    # White temperature (kelvin).
    n = len(ha.calls())
    page.locator("#sheetContent .kelvin input").evaluate(
        "r => { r.value = 4000; r.dispatchEvent(new Event('input')); r.dispatchEvent(new Event('change')); }")
    calls = ha.wait_calls(n + 1)
    assert [c["data"] for c in calls[n:]] == [{"entity_id": "light.lounge", "color_temp_kelvin": 4000}]
    expect(page.locator("#sheetContent .kelvin .ctl-val")).to_have_text("4000 K")
    shot(page, "light-sheet-after")


L_LAYOUT = {
    "unit": "m",
    "rooms": [
        # 6 x 5 m with the NE corner (2.5 x 2 m) cut out; the "Nook" fills that corner.
        {"id": "living", "name": "Living", "x": 0, "y": 0, "w": 6, "h": 5, "cut": {"corner": "ne", "w": 2.5, "h": 2}},
        {"id": "nook", "name": "Nook", "x": 3.5, "y": 0, "w": 2.5, "h": 2},
        {"id": "bed", "name": "Bedroom", "x": 0, "y": 5, "w": 4, "h": 3},
    ],
    "placements": [
        {"entity_id": "light.lounge", "x": 1.5, "y": 1.5},   # in the L
        {"entity_id": "light.strip", "x": 5, "y": 1},        # in the cut-out corner (inside the L's bounding box)
        {"entity_id": "light.kitchen", "x": 5, "y": 3.5},    # in the L's lower arm
        {"entity_id": "light.bedroom", "x": 2, "y": 6.5},
        {"entity_id": "switch.tv", "x": 1, "y": 4},          # a plug in the L: not a light
    ],
}


@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_room_name_tap_toggles_only_lights_in_l_shaped_room(stack, ha, open_page, size):
    page = open_page(stack, size, layout=L_LAYOUT)
    tag = page.locator('[data-roomtap="living"] rect')
    expect(page.locator('[data-roomtap="living"]')).to_have_class("room-tap lit")
    tap(page, *center(tag))  # lounge is on -> all off
    calls = ha.wait_calls(1)
    assert [(c["domain"], c["service"], c["data"]) for c in calls] == [
        ("light", "turn_off", {"entity_id": ["light.lounge", "light.kitchen"]})]
    expect(page.locator('[data-roomtap="living"]')).to_have_class("room-tap")
    page.wait_for_timeout(700)  # past the tap flash
    tap(page, *center(tag))  # none on -> all on
    calls = ha.wait_calls(2)
    assert (calls[1]["service"], calls[1]["data"]) == ("turn_on", {"entity_id": ["light.lounge", "light.kitchen"]})
    expect(page.locator("#status")).to_contain_text("Living: 2 lights on")
    # The strip in the cut-out corner was never touched; it belongs to the nook.
    assert toggles(ha, "light.strip") == []
    page.wait_for_timeout(700)
    tap(page, *center(page.locator('[data-roomtap="nook"] rect')))
    calls = ha.wait_calls(3)
    assert (calls[2]["service"], calls[2]["data"]) == ("turn_off", {"entity_id": ["light.strip"]})


def test_all_off_skips_keep_on_plugs(stack, ha, open_page):
    page = open_page(stack)
    # Mark the TV "keep on" in its sheet (long-press), which saves it in the layout.
    hold(page, marker(page, "switch.tv").locator("circle").first, 700)
    page.check("#sheetContent .keep-on input")
    expect(page.locator("#status")).to_contain_text("TV stays on with “All off”")
    assert page.request.get(stack.url + "/api/layout").json()["settings"]["keep_on"] == ["switch.tv"]
    page.click("#sheetClose")
    asked = []
    page.once("dialog", lambda d: (asked.append(d.message), d.accept()))
    page.click("#allOff")
    calls = ha.wait_calls(2)
    assert asked == ["Turn off 2 devices?\n1 “keep on” plug stay on."]
    got = {(c["domain"], c["service"]): set(c["data"]["entity_id"]) for c in calls}
    assert got == {("light", "turn_off"): {"light.lounge", "light.kitchen", "light.bedroom", "light.strip"},
                   ("switch", "turn_off"): {"switch.kettle"}}
    expect(marker(page, "switch.tv").locator("circle").first).to_have_attribute("fill", "var(--on)")
    expect(marker(page, "light.lounge").locator("circle").first).to_have_attribute("fill", "var(--off)")
    expect(page.locator("#status")).to_contain_text("Turned off 2")
    # Nothing left on (apart from the kept plug): no confirmation, no calls.
    page.click("#allOff")
    expect(page.locator("#status")).to_have_text("Everything is already off (1 kept on)")
    assert len(ha.calls()) == 2


def test_all_off_cancel_sends_nothing(stack, ha, open_page):
    page = open_page(stack, "mobile", layout={**LAYOUT, "settings": {"keep_on": []}})
    page.once("dialog", lambda d: d.dismiss())
    page.tap("#allOff")
    page.wait_for_timeout(500)
    assert ha.calls() == []


@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_heating_sheet_sets_all_valves(stack, ha, open_page, size):
    page = open_page(stack, size)
    page.click("#heating")
    expect(page.locator("#sheetContent h3")).to_have_text("Heating")
    rows = page.locator("#sheetContent .valve-row:not(.all)")
    expect(rows).to_have_count(2)
    expect(rows.nth(0).locator(".name")).to_have_text("Bedroom radiator")
    expect(rows.nth(1).locator(".sub")).to_have_text("Now 20.5° · heat")
    all_row = page.locator("#sheetContent .valve-row.all")
    expect(all_row.locator(".target")).to_have_text("21°")
    all_row.locator("button", has_text="+").click()
    all_row.locator("button", has_text="+").click()
    expect(all_row.locator(".target")).to_have_text("22°")
    shot(page, f"heating-{size}")
    page.click("#sheetContent button.big.primary")
    calls = ha.wait_calls(1)
    assert [(c["domain"], c["service"], c["data"]) for c in calls] == [
        ("climate", "set_temperature", {"entity_id": ["climate.bedroom_valve", "climate.lounge_valve"], "temperature": 22})]
    expect(rows.nth(0).locator(".target")).to_have_text("22°")
    # One valve on its own: −, debounced into a single call.
    rows.nth(1).locator("button", has_text="−").click()
    rows.nth(1).locator("button", has_text="−").click()
    calls = ha.wait_calls(2, timeout=4)
    assert [c["data"] for c in calls[1:]] == [{"entity_id": "climate.lounge_valve", "temperature": 21}]
