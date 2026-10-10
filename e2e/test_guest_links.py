"""Guest links: an admin makes a link + QR code for one room's lights (⋯ → Guest links…), a visitor opens it on their
phone without an account, switches and dims only those lights live, and loses access the moment it's revoked or runs
out. Desktop and a 390px touch phone, light and dark."""
import json
import re
import time

import pytest
from playwright.sync_api import expect

from conftest import shot

SIZES_THEMES = [("desktop", "dark"), ("phone", "light"), ("desktop", "light"), ("phone", "dark")]
# What a Kitchen link must never show: every other device in the fake HA.
OTHERS = ("Lounge lamp", "Bedroom", "TV strip", "Kettle", "TV", "Lounge radiator", "Front door", "Bedroom window",
          "Dehumidifier", "Other lamp", "light.lounge", "switch.kettle", "climate.")


def press(page, locator):
    locator.tap() if page.size in ("phone", "mobile") else locator.click()


def make_link(page, base, **body):
    body = {"label": "Sam", "room": "kitchen", "minutes": 120, **body}
    if "devices" in body:
        body.pop("room")
    r = page.request.post(base + "/api/guest/links", data=json.dumps(body), headers={"Content-Type": "application/json"})
    assert r.ok, r.text()
    return r.json()


def open_guest(open_page, stack, url, size="phone", theme="dark"):
    page = open_page(stack, size, signed_in=False, goto=None, color_scheme=theme)
    page.goto(url)
    return page


def card(page, eid):
    return page.locator(f'#guestDevices .gd[data-dev="{eid}"]')


@pytest.mark.parametrize("size,theme", SIZES_THEMES)
def test_admin_makes_a_link_with_a_qr_code(stack, ha, open_page, size, theme):
    page = open_page(stack, size, color_scheme=theme)
    page.click("#moreBtn")
    expect(page.locator("#guestLinksBtn")).to_be_visible()
    page.click("#guestLinksBtn")
    sheet = page.locator("#guestSheet")
    expect(sheet).to_be_visible()
    expect(sheet.locator("h3")).to_have_text("Guest links")
    f = sheet.locator("#guestForm")
    # Rooms with lights only (the hall and bathroom have none), and the chosen-devices option.
    opts = f.locator("[name=what] option").all_text_contents()
    assert opts[:3] == ["Lounge — 2 lights", "Kitchen — 1 light", "Bedroom — 1 light"] and opts[-1].startswith("Lights and plugs")
    label = f"Sam {size} {theme}"
    f.locator("[name=label]").fill(label)
    f.locator("[name=what]").select_option("room:kitchen")
    f.locator("[name=minutes]").select_option(label="3 days")
    press(page, f.locator("button[type=submit]"))
    new = sheet.locator("#guestNew")
    expect(new).to_be_visible()
    expect(new.locator("h4")).to_have_text(f"Scan to open “{label}”")
    url = sheet.locator("#guestUrl").input_value()
    assert re.fullmatch(re.escape(stack.url) + r"/guest\.html#[A-Za-z0-9_-]{43}", url), url
    qr = new.locator("#guestQr svg")
    expect(qr).to_be_visible()
    box = qr.bounding_box()
    assert box["width"] >= 150 and abs(box["width"] - box["height"]) < 1  # big enough to scan, square
    # The QR is drawn in the browser from exactly this URL, dark on light in both themes.
    same = page.evaluate("(u) => hcQR.svg(u, {ecl: 'M'}).match(/ d=\"([^\"]+)\"/)[1] === document.querySelector('#guestQr .qr-fg').getAttribute('d')", url)
    assert same
    bg = page.evaluate("getComputedStyle(document.querySelector('#guestQr .qr-bg')).fill")
    fg = page.evaluate("getComputedStyle(document.querySelector('#guestQr .qr-fg')).fill")
    assert bg == "rgb(255, 255, 255)" and fg == "rgb(17, 17, 17)"
    row = sheet.locator("#guestList .gl-row").filter(has_text=label)
    expect(row.locator(".gl-pill")).to_have_text("Active")
    expect(row.locator(".gl-info")).to_contain_text("Kitchen · 1 light · until ")
    expect(row.locator(".gl-info")).to_contain_text("not opened yet")
    # No horizontal overflow on a phone.
    assert page.evaluate("document.querySelector('#guestSheet .sheet-body').scrollWidth <= document.querySelector('#guestSheet .sheet-body').clientWidth + 1")
    shot(page, f"guest-links-new-{size}-{theme}")

    # Chosen devices: lights and plugs, never a protected plug (none here), with a validation message for none ticked.
    press(page, sheet.locator("#guestDone"))
    expect(sheet.locator("#guestNew")).to_have_count(0)
    f.locator("[name=label]").fill(f"Pick {size} {theme}")
    f.locator("[name=what]").select_option("devices")
    expect(sheet.locator("#guestDevs")).to_be_visible()
    press(page, f.locator("button[type=submit]"))
    expect(sheet.locator("#guestMsg")).to_have_text("Tick at least one light or plug")
    sheet.locator("#guestDevs label").filter(has_text="Kettle").locator("input").check()
    sheet.locator("#guestDevs label").filter(has_text="TV strip").locator("input").check()
    press(page, f.locator("button[type=submit]"))
    expect(sheet.locator("#guestNew")).to_be_visible()
    picked = sheet.locator("#guestList .gl-row").filter(has_text=f"Pick {size} {theme}")
    expect(picked.locator(".gl-info")).to_contain_text("TV strip, Kettle")
    shot(page, f"guest-links-list-{size}-{theme}")

    # Revoke (confirm).
    page.once("dialog", lambda d: d.accept())
    press(page, row.locator(".gl-revoke"))
    expect(row.locator(".gl-pill")).to_have_text("Revoked")
    expect(row.locator(".gl-revoke")).to_have_count(0)
    expect(sheet.locator("#guestMsg")).to_have_text(f"“{label}” revoked")
    assert ha.calls() == []


@pytest.mark.parametrize("size,theme", SIZES_THEMES)
def test_guest_switches_only_their_lights(stack, ha, open_page, size, theme):
    admin = open_page(stack, "desktop")
    link = make_link(admin, stack.url, label=f"Sam {size}{theme}")
    page = open_guest(open_page, stack, stack.url + link["path"], size, theme)
    expect(page.locator("#guestTitle")).to_have_text("Kitchen lights")
    expect(page.locator("#guestSub")).to_contain_text(f"For Sam {size}{theme} · until ")
    assert "#" not in page.url  # the token left the address bar
    kitchen = card(page, "light.kitchen")
    expect(kitchen).to_be_visible()
    expect(page.locator("#guestDevices .gd")).to_have_count(1)
    expect(kitchen.locator(".gd-state")).to_have_text("Off")
    body = page.content()
    for other in OTHERS:
        assert other not in body, other

    # Tap: toggles that light in HA; the live stream brings the new state (with brightness).
    press(page, kitchen.locator(".gd-toggle"))
    ha.wait_call(lambda c: c["service"] == "toggle" and c["data"].get("entity_id") == "light.kitchen")
    expect(kitchen).to_have_class(re.compile(r"\bon\b"))
    ha.set("light.kitchen", "on", brightness=128, supported_color_modes=["brightness"])
    expect(kitchen.locator(".gd-state")).to_have_text("On · 50%")
    slider = kitchen.locator(".gd-bright input")
    expect(slider).to_be_visible()
    slider.evaluate("(el) => { el.value = '80'; el.dispatchEvent(new Event('change', { bubbles: true })); }")
    ha.wait_call(lambda c: c["service"] == "turn_on" and c["data"].get("brightness_pct") == 80)
    shot(page, f"guest-page-on-{size}-{theme}")
    # Someone switches it off at the wall: the guest sees it.
    ha.set("light.kitchen", "off")
    expect(kitchen.locator(".gd-state")).to_have_text("Off")
    shot(page, f"guest-page-{size}-{theme}")

    # The guest's browser can reach nothing else.
    for path in ("/api/devices", "/api/layout", "/api/me", "/api/events?x=1"):
        r = page.request.get(stack.url + path)
        assert r.status == 401, path
    r = page.request.post(stack.url + "/api/guest/session/devices/light.lounge/toggle")
    assert r.status == 404
    r = page.request.post(stack.url + "/api/devices/light.lounge/toggle")
    assert r.status == 401
    assert [c["data"]["entity_id"] for c in ha.calls() if c["service"] in ("toggle", "turn_on", "turn_off")] == \
        ["light.kitchen", "light.kitchen"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_revoke_closes_an_open_guest_page(stack, ha, open_page):
    admin = open_page(stack, "desktop")
    link = make_link(admin, stack.url, label="Revoke me")
    page = open_guest(open_page, stack, stack.url + link["path"])
    expect(card(page, "light.kitchen")).to_be_visible()
    r = admin.request.delete(stack.url + f"/api/guest/links/{link['id']}")
    assert r.ok
    expect(page.locator("#guestGone")).to_be_visible()
    expect(page.locator("#guestGone h2")).to_have_text("This link has stopped working")
    expect(page.locator("#guestDevices .gd")).to_have_count(0)
    # A reload (cookie still there) and the original link stay refused.
    page.reload()
    expect(page.locator("#guestGone")).to_be_visible()
    page.goto(stack.url + link["path"])
    expect(page.locator("#guestGone")).to_be_visible()
    shot(page, "guest-page-revoked-phone-dark")
    r = page.request.post(stack.url + "/api/guest/session/devices/light.kitchen/toggle")
    assert r.status == 401
    assert ha.calls() == []


def test_a_bad_or_missing_token_shows_stopped(stack, ha, open_page):
    page = open_guest(open_page, stack, stack.url + "/guest.html#" + "x" * 43, "desktop", "light")
    expect(page.locator("#guestGone")).to_be_visible()
    assert "#" not in page.url
    page = open_guest(open_page, stack, stack.url + "/guest.html", "phone", "light")
    expect(page.locator("#guestGone")).to_be_visible()
    shot(page, "guest-page-gone-phone-light")


def test_link_runs_out_while_open(clock_stack, open_page):
    stack = clock_stack
    admin = open_page(stack, "desktop")
    link = make_link(admin, stack.url, minutes=15, label="Short")
    page = open_guest(open_page, stack, stack.url + link["path"], "phone", "light")
    expect(card(page, "light.kitchen")).to_be_visible()
    stack.set_clock(link["expires"] + 5)
    # The open stream notices within its check interval and the page says so; nothing can be switched after.
    expect(page.locator("#guestGone")).to_be_visible(timeout=20000)
    r = page.request.post(stack.url + "/api/guest/session/devices/light.kitchen/toggle")
    assert r.status == 401
    rows = admin.request.get(stack.url + "/api/guest/links").json()["links"]
    assert [x["active"] for x in rows] == [False]


def test_members_and_guests_see_no_guest_links_menu(stack, ha, open_page):
    admin = open_page(stack, "desktop")
    for name, role in (("mia_gl", "member"), ("gus_gl", "guest")):
        r = admin.request.post(stack.url + "/api/users", data=json.dumps({"username": name, "password": "password-1", "role": role}),
                               headers={"Content-Type": "application/json"})
        assert r.ok or "taken" in r.text(), r.text()
        page = open_page(stack, "phone", signed_in=False, goto=None)
        page.goto(stack.url + "/login.html")
        page.fill("[name=username]", name)
        page.fill("[name=password]", "password-1")
        page.click("button[type=submit]")
        page.wait_for_url(re.compile(r"/(\?.*)?$"))
        page.locator("#plan .marker").first.wait_for()
        page.wait_for_function(f"document.documentElement.dataset.role === '{role}'")
        page.tap("#moreBtn")
        expect(page.locator("#moreMenu")).to_be_visible()
        expect(page.locator("#guestLinksBtn")).to_be_hidden()
        assert page.request.get(stack.url + "/api/guest/links").status == 403
