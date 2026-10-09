"""Multi-user logins: the admin's Users sheet, what a member and a guest see and can do, and being signed out when
removed. Desktop and a 390px touch phone, light and dark."""
import json
import re

import pytest
from playwright.sync_api import expect

from conftest import LAYOUT, login, marker, shot

SIZES_THEMES = [("desktop", "dark"), ("phone", "light"), ("desktop", "light"), ("phone", "dark")]


def add_user(page, base, name, role, pw="password-1", **kw):
    r = page.request.post(base + "/api/users", data=json.dumps({"username": name, "password": pw, "role": role, **kw}),
                          headers={"Content-Type": "application/json"})
    assert r.ok, r.text()
    return pw


def open_as(open_page, stack, name, pw, size="desktop", theme="dark"):
    page = open_page(stack, size, signed_in=False, color_scheme=theme)
    login(page, stack.url, name, pw)
    page.locator("#plan .marker").first.wait_for()
    return page


def press(page, locator):
    locator.tap() if page.size == "phone" else locator.click()


def menu(page):
    page.click("#moreBtn")
    expect(page.locator("#moreMenu")).to_be_visible()


@pytest.mark.parametrize("size,theme", SIZES_THEMES)
def test_admin_users_sheet(stack, ha, open_page, size, theme):
    page = open_page(stack, size, color_scheme=theme)
    tag = f"{size[0]}{theme[0]}"  # the module's stack is shared: one set of names per run
    menu(page)
    expect(page.locator("#usersBtn")).to_have_text("Users…")
    page.click("#usersBtn")
    sheet = page.locator("#usersSheet")
    expect(sheet).to_be_visible()
    expect(sheet.locator("h3")).to_have_text("Users")
    expect(sheet.locator(".users-me")).to_contain_text("Signed in as me · Admin")
    expect(sheet.locator('li.user-row[data-user="me"] .user-info')).to_have_text("Set by APP_USER / APP_PASSWORD in .env")
    expect(sheet.locator('li.user-row[data-user="me"] select.user-role')).to_be_disabled()

    # Add a member, then a guest for a week.
    f = sheet.locator("#userAddForm")
    f.locator("[name=username]").fill(f"mia{tag}")
    f.locator("[name=password]").fill("member-pass-1")
    f.locator("button[type=submit]").click()
    expect(sheet.locator("#usersMsg")).to_have_text(f"mia{tag} added — give them the password: member-pass-1")
    row = sheet.locator(f'li.user-row[data-user="mia{tag}"]')
    expect(row.locator("select.user-role")).to_have_value("member")
    f.locator("[name=username]").fill(f"gus{tag}")
    expect(f.locator("[name=expires]")).to_be_hidden()
    f.locator("[name=role]").select_option("guest")
    f.locator("[name=expires]").select_option(label="1 week")
    f.locator("button[type=submit]").click()
    guest = sheet.locator(f'li.user-row[data-user="gus{tag}"]')
    expect(guest.locator(".user-info")).to_contain_text("Until ")
    shot(page, f"users-sheet-{size}-{theme}")

    # Promote the guest; reset the member's password (prompt); remove the guest (confirm).
    guest.locator("select.user-role").select_option("member")
    expect(sheet.locator("#usersMsg")).to_have_text(f"gus{tag} is now member")
    page.once("dialog", lambda d: d.accept("brand-new-pass"))
    row.locator(".user-reset").click()
    expect(sheet.locator("#usersMsg")).to_have_text(f"New password set for mia{tag} — they're signed out everywhere")
    other = open_page(stack, signed_in=False, goto=None)
    for pw, ok in (("member-pass-1", False), ("brand-new-pass", True)):
        r = other.request.post(stack.url + "/api/login", data=json.dumps({"username": f"mia{tag}", "password": pw}),
                               headers={"Content-Type": "application/json"})
        assert r.ok == ok
    asked = []
    page.once("dialog", lambda d: (asked.append(d.message), d.accept()))
    sheet.locator(f'li.user-row[data-user="gus{tag}"] .user-remove').click()
    expect(sheet.locator(f'li.user-row[data-user="gus{tag}"]')).to_have_count(0)
    assert asked == [f"Remove gus{tag}? They are signed out at once."]
    # The sheet fits the phone: no sideways scrolling.
    assert page.evaluate("document.querySelector('#usersSheet .sheet-body').scrollWidth <= document.querySelector('#usersSheet .sheet-body').clientWidth")


@pytest.mark.parametrize("size,theme", SIZES_THEMES)
def test_member_controls_without_settings(stack, ha, open_page, size, theme):
    admin = open_page(stack)
    name = f"mem{size[0]}{theme[0]}"
    pw = add_user(admin, stack.url, name, "member")
    page = open_as(open_page, stack, name, pw, size, theme)
    expect(page.locator("#editToggle")).to_be_hidden()
    expect(page.locator("#heating")).to_be_visible()
    expect(page.locator("#allOff")).to_be_visible()
    menu(page)
    expect(page.locator("#usersBtn")).to_have_text("Account…")
    for hidden in ("#importLayout", "#hiddenBtn", "#weatherBtn", "#schedulesBtn", "#unit"):
        expect(page.locator(hidden)).to_be_hidden()
    for shown in ("#awayBtn", "#activityBtn", "#energyBtn", "#exportLayout", "#signOut"):
        expect(page.locator(shown)).to_be_visible()
    shot(page, f"users-member-menu-{size}-{theme}")
    page.click("#moreBtn")
    expect(page.locator("#moreMenu")).to_be_hidden()
    press(page, marker(page, "switch.kettle"))  # plugs toggle for members
    ha.wait_call(lambda c: c["service"] == "toggle" and c["data"]["entity_id"] == "switch.kettle")
    # Settings are refused by the server too.
    r = page.request.put(stack.url + "/api/layout", data=json.dumps({**LAYOUT, "furniture": []}), headers={"Content-Type": "application/json"})
    assert r.status == 403 and r.json() == {"detail": "Only an admin can change settings."}


def test_member_sheet_and_alerts_read_only(stack, ha, open_page):
    admin = open_page(stack)
    pw = add_user(admin, stack.url, "memsheet", "member")
    page = open_as(open_page, stack, "memsheet", pw)
    page.evaluate("openSheet('switch.kettle')")
    expect(page.locator("#sheet")).to_be_visible()
    expect(page.locator("#sheet .big")).to_be_visible()
    expect(page.locator("#sheet .dev-meta")).to_be_hidden()
    page.click("#sheetClose")
    page.click("#alertsBtn")
    expect(page.locator("#alertSheet")).to_be_visible()
    expect(page.locator("#alertSheet .role-note")).to_be_visible()
    assert page.evaluate("getComputedStyle(document.getElementById('alertEnabled')).pointerEvents") == "none"
    shot(page, "users-member-alerts")


@pytest.mark.parametrize("size,theme", SIZES_THEMES)
def test_guest_lights_only(stack, ha, open_page, size, theme):
    admin = open_page(stack)
    name = f"gst{size[0]}{theme[0]}"
    pw = add_user(admin, stack.url, name, "guest")
    page = open_as(open_page, stack, name, pw, size, theme)
    for hidden in ("#editToggle", "#heating", "#allOff", "#alertsBtn"):
        expect(page.locator(hidden)).to_be_hidden()
    expect(page.locator("#moreBtn")).to_be_visible()
    menu(page)
    visible = [b for b in page.locator("#moreMenu > button").all() if b.is_visible()]
    assert [b.inner_text() for b in visible] == ["Rooms…", "Wall mode", "Favourites", "Account…", "Sign out"]
    shot(page, f"users-guest-menu-{size}-{theme}")
    page.click("#moreBtn")
    expect(page.locator("#moreMenu")).to_be_hidden()
    # Lights toggle; everything else is shown but inert.
    press(page, marker(page, "light.lounge"))
    ha.wait_call(lambda c: c["service"] == "toggle" and c["data"]["entity_id"] == "light.lounge")
    for eid in ("switch.kettle", "climate.lounge_valve", "binary_sensor.contact_sensor_door"):
        assert page.evaluate("(e) => getComputedStyle(document.querySelector(`#plan .marker[data-dev='${e}']`)).pointerEvents", eid) == "none"
    shot(page, f"users-guest-plan-{size}-{theme}")
    r = page.request.post(stack.url + "/api/devices/switch.kettle/toggle")
    assert r.status == 403 and r.json() == {"detail": "Guests can only switch the lights."}
    assert not [c for c in ha.calls() if c["data"].get("entity_id") == "switch.kettle"]


def test_guest_expiry_shown_and_own_password(stack, ha, open_page):
    admin = open_page(stack)
    pw = add_user(admin, stack.url, "gstpw", "guest", expires=admin.evaluate("Date.now() / 1000 + 86400"))
    page = open_as(open_page, stack, "gstpw", pw, "phone")
    menu(page)
    page.click("#usersBtn")
    sheet = page.locator("#usersSheet")
    expect(sheet.locator("h3")).to_have_text("Account")
    expect(sheet.locator(".users-me")).to_contain_text(re.compile(r"Signed in as gstpw · Guest · until "))
    expect(sheet.locator("#usersList")).to_have_count(0)
    f = sheet.locator("#ownPwForm")
    f.locator("[name=current]").fill("wrong-one")
    f.locator("[name=password]").fill("new-guest-pass")
    f.locator("button[type=submit]").click()
    expect(sheet.locator("#usersMsg")).to_have_text("current password is wrong")
    f.locator("[name=current]").fill(pw)
    f.locator("button[type=submit]").click()
    expect(sheet.locator("#usersMsg")).to_have_text("Password changed — your other devices are signed out")
    shot(page, "users-account-phone")
    page.reload()  # still signed in with the fresh cookie
    page.locator("#plan .marker").first.wait_for()
    assert page.request.get(stack.url + "/api/me").json()["user"] == "gstpw"


def test_removed_member_is_signed_out(stack, ha, open_page):
    admin = open_page(stack)
    pw = add_user(admin, stack.url, "leaver", "member")
    page = open_as(open_page, stack, "leaver", pw)
    assert admin.request.delete(stack.url + "/api/users/leaver").ok
    page.reload()
    page.wait_for_url(stack.url + "/login.html")
    page.fill("[name=username]", "leaver")
    page.fill("[name=password]", pw)
    page.click("button[type=submit]")
    expect(page.locator("#loginError")).to_have_text("Wrong username or password.")
