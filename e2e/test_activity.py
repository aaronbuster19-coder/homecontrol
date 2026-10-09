"""Activity timeline (⋯ → Activity): HA history merged with the app's own log, "by you" attribution, filters,
older days on scroll, a tap opening the device sheet — on a desktop and a 390px touch phone."""
import re

import pytest
from playwright.sync_api import expect

from conftest import marker_center, shot


def menu(page, item):
    page.click("#moreBtn")
    expect(page.locator("#moreMenu")).to_be_visible()
    page.click(item)


def no_hscroll(page):
    return page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("size", ["desktop", "phone"])
def test_activity_door_and_light_by_you(stack, ha, open_page, size):
    page = open_page(stack, size=size)
    # A door opens in HA (by hand), then the kitchen light is switched in the app.
    ha.set("binary_sensor.contact_sensor_door", "on")
    expect(page.locator('#list li[data-dev="binary_sensor.contact_sensor_door"] .val')).to_contain_text("open")
    x, y = marker_center(page, "light.kitchen")
    (page.touchscreen.tap if size == "phone" else page.mouse.click)(x, y)
    ha.wait_call(lambda c: c["service"] == "toggle" and c["data"]["entity_id"] == "light.kitchen")

    menu(page, "#activityBtn")
    sheet = page.locator("#activity")
    expect(sheet).to_be_visible()
    first = page.locator("#actList .act-ev").first
    expect(first).to_contain_text("Kitchen turned on — by you")
    expect(first).to_have_attribute("data-dev", "light.kitchen")
    expect(first.locator(".act-meta")).to_have_text("Kitchen")
    expect(page.locator("#actList .act-day").first).to_have_text("Today")
    door = page.locator("#actList .act-ev", has_text="Front door opened").first
    expect(door).to_be_visible()
    expect(door.locator(".act-meta")).to_have_text("Hall")
    # HA's own generated history is in there too, attributed to "manually / other"
    expect(page.locator("#actList .act-by.other").first).to_be_attached()
    assert no_hscroll(page)
    shot(page, f"activity-{size}")

    # Filter by room: only the kitchen's entries
    page.select_option("#actRoom", "kitchen")
    expect(page.locator("#actList .act-ev").first).to_contain_text("Kitchen turned on — by you")
    expect(page.locator("#actList .act-ev", has_text="Front door")).to_have_count(0)
    metas = page.locator("#actList .act-ev .act-meta").all_text_contents()
    assert metas and all(m.startswith("Kitchen") for m in metas)
    # …and by type
    page.select_option("#actRoom", "")
    page.select_option("#actType", "doors")
    expect(page.locator("#actList .act-ev").first).to_contain_text("Front door opened")
    expect(page.locator("#actList .act-ev:not(.t-doors)")).to_have_count(0)
    shot(page, f"activity-{size}-doors")

    # Older days arrive on scroll
    page.select_option("#actType", "")
    expect(page.locator("#actList .act-ev").first).to_contain_text("Kitchen turned on")
    for _ in range(20):
        if page.locator("#actList .act-day", has_text="Yesterday").count():
            break
        page.evaluate("document.querySelector('#sheet .sheet-body').scrollTop = 1e9")
        page.wait_for_function("!document.querySelector('#actMore .loading')")
    expect(page.locator("#actList .act-day", has_text="Yesterday")).to_be_visible()

    # A tap jumps to the device sheet
    page.evaluate("document.querySelector('#sheet .sheet-body').scrollTop = 0")
    entry = page.locator('#actList .act-ev[data-dev="light.kitchen"]').first
    entry.tap() if size == "phone" else entry.click()
    expect(page.locator("#sheetContent h3")).to_have_text("Kitchen")
    expect(page.locator("#activity")).to_have_count(0)
    assert no_hscroll(page)


def test_activity_api_shapes_and_sign_ins(stack, ha, open_page):
    page = open_page(stack)
    r = page.request.get(stack.url + "/api/activity").json()
    assert r["days"][0]["label"] == "Today" and r["next_before"] == r["start"]
    assert {"t", "day", "type", "icon", "text", "by", "entity_id", "room"} <= set(r["entries"][0])
    # the sign-in of this very page is in the security filter
    sec = page.request.get(stack.url + "/api/activity?type=security").json()["entries"]
    assert any(e["text"] == "me signed in" for e in sec)
    older = page.request.get(stack.url + f"/api/activity?before={r['start']}").json()
    assert older["days"][0]["label"] == "Yesterday" and older["end"] == r["start"]
    assert page.request.get(stack.url + "/api/activity?type=bogus").status == 400
