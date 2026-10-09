"""Browser tests for presence lighting (⋯ → Presence lighting…): fake HA's sun.sun and front door (e2e/fake_ha.py) and a
movable server clock (e2e/clock_app.py via `clock_stack`), so "10 quiet minutes" takes no real waiting.
Desktop and a 390px touch phone, light and dark; a member sees it read-only, a guest not at all.

    python -m pytest -q e2e/test_presence_lighting.py      # SHOTS=dir to keep screenshots
"""
import json
import re
import time

import pytest
from playwright.sync_api import expect

from conftest import login, shot

DOOR, KITCHEN = "binary_sensor.contact_sensor_door", "light.kitchen"
WAIT = 15  # s: slow, contended CI runners


def api(page, stack, path, method="GET", body=None):
    r = page.request.fetch(stack.url + path, method=method, data=json.dumps(body) if body is not None else None,
                           headers={"Content-Type": "application/json"})
    assert r.ok, r.text()
    return r.json()


def wait_api(page, stack, path, pred, timeout=WAIT):
    end = time.time() + timeout
    while True:
        d = api(page, stack, path)
        if pred(d):
            return d
        if time.time() > end:
            raise AssertionError(f"{path}: condition not met, last {d}")
        page.wait_for_timeout(100)


def room(d, rid):
    return next(r for r in d["rooms"] if r["id"] == rid)


def tap(page, loc):
    loc = page.locator(loc) if isinstance(loc, str) else loc
    loc.tap() if page.size in ("phone", "mobile") else loc.click()


def open_sheet(page):
    tap(page, "#moreBtn")
    tap(page, "#plBtn")
    expect(page.locator("#plSheet")).to_be_visible()
    expect(page.locator("#plStatus")).to_be_visible()


def reopen(page):
    page.locator("#plSheet .close").click()
    open_sheet(page)


@pytest.mark.parametrize("size,theme", [("desktop", "dark"), ("mobile", "light")])
def test_door_after_dark_then_quiet(clock_stack, open_page, size, theme):
    st, ha = clock_stack, clock_stack.ha
    page = open_page(st, size, color_scheme=theme)
    ha.set("sun.sun", "below_horizon")
    open_sheet(page)
    expect(page.locator("#plStatus")).to_have_text(re.compile(r"^Dark now until .*\(Home Assistant's sun\)\.$"))
    expect(page.locator("#plOn")).not_to_be_checked()
    hall = page.locator('#plRooms li[data-room="hall"]')
    expect(hall.locator(".pl-phase")).to_have_count(0)  # off: no pill
    tap(page, "#plOn")
    expect(page.locator("#plMsg")).to_have_text("Presence lighting on")
    tap(page, hall.locator("input[data-enable]"))
    expect(page.locator("#plMsg")).to_have_text("Hall: on")
    # the hall has the front door on its wall but no light of its own: pick the kitchen light
    expect(hall.locator(".pl-phase")).to_have_text("Pick the lights")
    tap(page, hall.locator("details summary"))
    expect(hall.locator("details summary")).to_have_text("1 door · 0 lights")
    door_row = hall.locator("label.check", has=page.locator(f'input[data-sensors="{DOOR}"]'))
    expect(door_row).to_contain_text("door of this room")
    expect(door_row.locator("input")).to_be_checked()
    tap(page, hall.locator(f'input[data-lights="{KITCHEN}"]'))
    expect(page.locator("#plMsg")).to_have_text("Hall: lights saved")
    hall = page.locator('#plRooms li[data-room="hall"]')
    expect(hall.locator(".pl-phase")).to_have_text("Ready")
    expect(hall.locator("details")).to_have_attribute("open", "")  # stays open across the re-render
    expect(hall.locator("button.pl-reset")).to_be_visible()  # lights chosen by hand: a way back to the plan's
    page.fill('input[data-quiet="hall"]', "4")
    page.locator('input[data-quiet="hall"]').dispatch_event("change")
    expect(page.locator("#plMsg")).to_have_text("Hall: off after 4 min")
    shot(page, f"plight-{size}-{theme}-setup")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not ha.calls()

    # the front door opens: the kitchen light comes on, once
    ha.set(DOOR, "on")
    ha.wait_call(lambda c: c["service"] == "turn_on" and c["data"]["entity_id"] == [KITCHEN], timeout=WAIT)
    ha.set(DOOR, "off")
    d = wait_api(page, st, "/api/presence-lighting", lambda d: room(d, "hall")["phase"] == "on")
    reopen(page)
    expect(page.locator('#plRooms li[data-room="hall"] .pl-phase')).to_have_text(re.compile(r"^Lights on — off at \d\d:\d\d if quiet$"))
    expect(page.locator(f'#plan .marker[data-dev="{KITCHEN}"] circle').first).to_have_attribute("fill", "var(--on)")
    shot(page, f"plight-{size}-{theme}-on")
    assert len(ha.calls("turn_on")) == 1

    # 4 quiet minutes after the door's last activity: off again
    d = wait_api(page, st, "/api/presence-lighting", lambda d: room(d, "hall")["off_at"] and
                 room(d, "hall")["off_at"] >= d["now"] + 3 * 60_000)
    st.set_clock(room(d, "hall")["off_at"] / 1000 + 2)
    ha.wait_call(lambda c: c["service"] == "turn_off" and c["data"]["entity_id"] == [KITCHEN], timeout=WAIT)
    wait_api(page, st, "/api/presence-lighting", lambda d: room(d, "hall")["phase"] == "ready")
    reopen(page)
    expect(page.locator('#plRooms li[data-room="hall"] .pl-phase')).to_have_text("Ready")
    tap(page, "#plLog summary")
    expect(page.locator("#plLog .sum-row").first).to_contain_text("Hall: lights off — 1 light — 4 min without door activity")
    expect(page.locator("#plLog")).to_contain_text("Hall: lights on — 1 light — door opened after dark")
    shot(page, f"plight-{size}-{theme}-log")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert len(ha.calls("turn_on")) == 1 and len(ha.calls("turn_off")) == 1


def test_manual_change_daylight_and_away(clock_stack, open_page):
    st, ha = clock_stack, clock_stack.ha
    page = open_page(st, "desktop")
    api(page, st, "/api/presence-lighting/settings", "PUT", {"enabled": True})
    api(page, st, "/api/presence-lighting/rooms/hall", "PUT", {"enabled": True, "lights": [KITCHEN], "quiet_minutes": 2})
    # daylight: the door does nothing
    ha.set(DOOR, "on"); ha.set(DOOR, "off")
    wait_api(page, st, "/api/presence-lighting", lambda d: any(x["action"] == "enabled" for x in d["log"]))
    page.wait_for_timeout(1500)
    assert not ha.calls("turn_on")
    open_sheet(page)
    expect(page.locator("#plStatus")).to_have_text(re.compile(r"^Daylight — doors switch lights on from"))
    expect(page.locator('#plRooms li[data-room="hall"] .pl-phase')).to_have_text("Waiting for dark")

    # dark: on; then switched off at the wall -> paused, and the door doesn't switch it back on
    ha.set("sun.sun", "below_horizon")
    ha.set(DOOR, "on")
    ha.wait_call(lambda c: c["service"] == "turn_on", timeout=WAIT)
    ha.set(DOOR, "off")
    wait_api(page, st, "/api/presence-lighting", lambda d: room(d, "hall")["phase"] == "on")
    ha.set(KITCHEN, "off")  # by hand
    d = wait_api(page, st, "/api/presence-lighting", lambda d: room(d, "hall")["phase"] == "paused")
    reopen(page)
    expect(page.locator('#plRooms li[data-room="hall"] .pl-phase')).to_have_text(re.compile(r"^Paused \(switched by hand\) until \d\d:\d\d if quiet$"))
    shot(page, "plight-desktop-paused")
    ha.set(DOOR, "on"); ha.set(DOOR, "off")
    page.wait_for_timeout(1500)
    assert len(ha.calls("turn_on")) == 1 and not ha.calls("turn_off")
    d = wait_api(page, st, "/api/presence-lighting", lambda d: room(d, "hall")["paused_until"] >= d["now"] + 60_000)
    st.set_clock(room(d, "hall")["paused_until"] / 1000 + 2)
    wait_api(page, st, "/api/presence-lighting", lambda d: room(d, "hall")["phase"] == "ready")
    assert not ha.calls("turn_off")  # never switched off against the user

    # Away: nothing switches on
    api(page, st, "/api/mode", "POST", {"mode": "away"})
    wait_api(page, st, "/api/presence-lighting", lambda d: room(d, "hall")["phase"] == "away")
    n = len(ha.calls("turn_on"))
    ha.set(DOOR, "on"); ha.set(DOOR, "off")
    page.wait_for_timeout(1500)
    assert len(ha.calls("turn_on")) == n


@pytest.mark.parametrize("size,theme", [("desktop", "light"), ("phone", "dark")])
def test_member_read_only_guest_none(clock_stack, open_page, size, theme):
    st = clock_stack
    admin = open_page(st)
    api(admin, st, "/api/presence-lighting/settings", "PUT", {"enabled": True})
    for name, role in (("mel", "member"), ("gil", "guest")):
        api(admin, st, "/api/users", "POST", {"username": name, "password": "password-1", "role": role})

    page = open_page(st, size, signed_in=False, color_scheme=theme)
    login(page, st.url, "mel", "password-1")
    page.locator("#plan .marker").first.wait_for()
    open_sheet(page)
    expect(page.locator("#plSheet .pl-role")).to_be_visible()
    expect(page.locator("#plOn")).to_be_checked()
    assert page.evaluate("getComputedStyle(document.getElementById('plOn')).pointerEvents") == "none"
    r = page.request.put(st.url + "/api/presence-lighting/settings", data=json.dumps({"enabled": False}),
                         headers={"Content-Type": "application/json"})
    assert r.status == 403
    shot(page, f"plight-member-{size}-{theme}")

    guest = open_page(st, size, signed_in=False, color_scheme=theme)
    login(guest, st.url, "gil", "password-1")
    guest.locator("#plan .marker").first.wait_for()
    tap(guest, "#moreBtn")
    expect(guest.locator("#moreMenu")).to_be_visible()
    expect(guest.locator("#plBtn")).to_be_hidden()
    assert guest.request.get(st.url + "/api/presence-lighting").status == 403
    assert api(admin, st, "/api/presence-lighting")["enabled"] is True
