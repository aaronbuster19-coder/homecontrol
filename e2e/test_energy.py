"""Browser tests for energy costs and device names / hidden devices (fake HA from e2e/fake_ha.py).

    python -m pytest -q e2e/test_energy.py      # SHOTS=dir to keep screenshots
Helpers come from test_wall.py. The fixtures are module-scoped copies of its session ones: two Playwright
instances can't be alive at once, so this module's browser is closed before test_wall.py's starts.
"""
import json
import re

import pytest
from playwright.sync_api import expect

import test_wall
from test_wall import HA, SIZES, hold, marker, new_page, shot

CLEAN = {"keep_on": [], "names": {}, "hidden": [], "energy": {}}
PHONE_DESKTOP = [s for s in SIZES if s != "portrait"]


@pytest.fixture(scope="module")
def servers(tmp_path_factory):
    yield from test_wall.servers.__wrapped__(tmp_path_factory)


@pytest.fixture(scope="module")
def browser():
    yield from test_wall.browser.__wrapped__()


@pytest.fixture
def ha(servers):
    h = HA(servers["ha"]); h.reset()
    return h


def page_for(browser, servers, size):
    page = new_page(browser, servers, size)
    r = page.request.get(servers["app"] + "/api/layout")
    L = {**r.json(), "settings": CLEAN}
    assert page.request.put(servers["app"] + "/api/layout", data=json.dumps(L), headers={"Content-Type": "application/json"}).ok
    page.goto(servers["app"] + "/")
    expect(marker(page, "light.lounge")).to_be_visible()
    return page


def open_menu_item(page, item_id):
    page.click("#moreBtn")
    page.click(f"#{item_id}")
    expect(page.locator("#sheet")).to_be_visible()


def device_sheet(page, eid):
    hold(page, marker(page, eid).locator("circle"), 700)
    expect(page.locator("#sheet")).to_be_visible()


@pytest.mark.parametrize("size", PHONE_DESKTOP)
def test_energy_costs(browser, servers, ha, size):
    page = page_for(browser, servers, size)
    open_menu_item(page, "energyBtn")
    expect(page.locator("#sheetContent h3")).to_have_text("Energy")
    expect(page.locator("#sheetContent")).to_contain_text("Smart plugs only")
    expect(page.locator(".energy-tariff")).to_contain_text("No unit rate set")
    expect(page.locator("#energyTotal")).to_contain_text("kWh")
    page.click("#tariffBtn")
    page.fill("#tariffForm [name=rate]", "24.5")
    page.fill("#tariffForm [name=standing]", "60")
    page.click("#tariffOk")
    expect(page.locator("#tariffDialog")).not_to_be_visible()
    expect(page.locator(".energy-tariff")).to_contain_text("24.5p/kWh · standing charge 60p/day")
    # Today: TV's own meter says 0.42 kWh -> 10p; kettle from history.
    tv = page.locator('.energy-body .sum-row[data-plug="switch.tv"]')
    expect(tv).to_contain_text("0.42 kWh · 10p")
    expect(page.locator("#energyTotal b")).to_have_text(re.compile(r"^(\d+p|£\d+\.\d\d)$"))
    expect(page.locator(".energy-body .standing")).to_contain_text("Plus standing charge 60p (1 day × 60p)")
    # Standby: the TV idles at 4 W overnight -> 35.04 kWh/yr × 24.5p = £8.58/yr; the kettle draws nothing.
    expect(page.locator('.standby .sum-row[data-plug="switch.tv"]')).to_contain_text("4 W · ≈ £8.58/yr")
    expect(page.locator('.standby .sum-row[data-plug="switch.kettle"]')).to_contain_text("0 W")
    shot(page, f"energy-{size}-today")
    page.locator("#sheetContent .seg button", has_text="week").click()
    expect(page.locator("#energyTotal span")).to_contain_text("This week")
    expect(page.locator(".energy-body .standing")).to_contain_text("day")
    page.locator("#sheetContent .seg button", has_text="month").click()
    expect(page.locator("#energyTotal span")).to_contain_text("This month")
    shot(page, f"energy-{size}-month")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.click("#sheetClose")

    # Plug sheet and history show pence too.
    device_sheet(page, "switch.tv")
    expect(page.locator("#sheetContent")).to_contain_text("Today 0.42 kWh · 10p")
    hist = page.locator("#sheetContent details.hist")
    if not hist.get_attribute("open"):
        page.click("#sheetContent details.hist summary > span")
    hist.locator(".seg button", has_text="7d").click()
    expect(page.locator(".hist-readout")).to_have_text(re.compile(r"^7d: \d+\.\d\d kWh · (\d+p|£\d+\.\d\d)"))
    shot(page, f"energy-{size}-plug-sheet")
    assert not page.errors


@pytest.mark.parametrize("size", PHONE_DESKTOP)
def test_rename_and_hide(browser, servers, ha, size):
    page = page_for(browser, servers, size)
    device_sheet(page, "light.lounge")
    page.click("#sheetContent .dev-meta button")
    page.fill("#renameForm [name=name]", "  Sofa lamp ")
    page.click("#renameForm button[value=ok]")
    expect(page.locator("#sheetContent h3")).to_have_text("Sofa lamp")
    expect(page.locator("#sheetContent .ha-name")).to_have_text("Home Assistant: Lounge lamp")
    shot(page, f"names-{size}-sheet")
    page.click("#sheetClose")
    expect(page.locator('#list li[data-dev="light.lounge"] .name')).to_have_text("Sofa lamp")
    expect(marker(page, "light.lounge").locator("title")).to_have_text(re.compile(r"^Sofa lamp — "))
    page.reload()
    expect(page.locator('#list li[data-dev="light.lounge"] .name')).to_have_text("Sofa lamp")
    expect(marker(page, "light.lounge").locator("title")).to_have_text(re.compile(r"^Sofa lamp — "))

    # Hide the kettle: gone from the plan and the list, placement kept.
    device_sheet(page, "switch.kettle")
    page.check("#sheetContent .hide-toggle input")
    page.click("#sheetClose")
    expect(marker(page, "switch.kettle")).to_have_count(0)
    expect(page.locator('#list li[data-dev="switch.kettle"]')).to_have_count(0)
    shot(page, f"names-{size}-hidden")
    page.reload()
    expect(marker(page, "light.lounge")).to_be_visible()
    expect(marker(page, "switch.kettle")).to_have_count(0)
    # The energy sheet folds it under "Hidden".
    open_menu_item(page, "energyBtn")
    expect(page.locator('.energy-body .sum-row[data-plug="switch.kettle"]')).to_have_count(0)
    expect(page.locator(".energy-hidden summary")).to_have_text("Hidden (1)")
    page.click("#sheetClose")
    # ⋯ → Hidden devices → Unhide brings the marker back where it was.
    open_menu_item(page, "hiddenBtn")
    row = page.locator('.hidden-list li[data-hidden="switch.kettle"]')
    expect(row).to_contain_text("Kettle")
    shot(page, f"names-{size}-hidden-sheet")
    row.locator("button", has_text="Unhide").click()
    expect(row).to_have_count(0)
    page.click("#sheetClose")
    expect(marker(page, "switch.kettle")).to_be_visible()
    expect(page.locator('#list li[data-dev="switch.kettle"]')).to_be_visible()
    assert not page.errors


def test_all_off_confirm_mentions_hidden(browser, servers, ha):
    page = page_for(browser, servers, "tablet")
    r = page.request.put(servers["app"] + "/api/devices/switch.tv/meta", data=json.dumps({"hidden": True}),
                         headers={"Content-Type": "application/json"})
    assert r.ok
    expect(marker(page, "switch.tv")).to_have_count(0)  # pushed over the live stream
    msgs = []
    page.once("dialog", lambda d: (msgs.append(d.message), d.dismiss()))
    page.click("#allOff")
    page.wait_for_timeout(300)
    assert msgs and "Includes 1 hidden device." in msgs[0], msgs
    assert not [c for c in ha.calls() if c["service"] == "turn_off"]  # dismissed: nothing sent
    page.once("dialog", lambda d: d.accept())
    page.click("#allOff")
    ha.wait_call(lambda c: c["service"] == "turn_off" and "switch.tv" in json.dumps(c["data"]))
    assert not page.errors
