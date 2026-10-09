"""Login, sign-out, the service worker and offline start."""
import re

import pytest
from playwright.sync_api import expect

from conftest import PASSWORD, USER, marker, shot


@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_login_redirect_and_sign_in(stack, ha, open_page, size):
    page = open_page(stack, size, signed_in=False)
    page.goto(stack.url + "/")
    page.wait_for_url(stack.url + "/login.html")
    expect(page.locator("#loginForm")).to_be_visible()
    assert page.request.get(stack.url + "/api/devices").status == 401
    shot(page, f"login-{size}")
    page.fill("[name=username]", USER)
    page.fill("[name=password]", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(stack.url + "/")
    expect(page.locator("#status")).to_contain_text("devices")
    # Already signed in: the login page sends you straight back.
    page.goto(stack.url + "/login.html")
    page.wait_for_url(stack.url + "/")


def test_wrong_password_message(fresh_stack, open_page):
    page = open_page(fresh_stack, signed_in=False)
    page.goto(fresh_stack.url + "/login.html")
    page.fill("[name=username]", USER)
    page.fill("[name=password]", "not-the-password")
    page.click("button[type=submit]")
    expect(page.locator("#loginError")).to_have_text("Wrong username or password.")
    assert page.url == fresh_stack.url + "/login.html"
    # The password is selected so it can be retyped straight away.
    assert page.evaluate("document.activeElement.name") == "password"
    page.fill("[name=password]", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(fresh_stack.url + "/")


@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_sign_out_from_more_menu(stack, ha, open_page, size):
    page = open_page(stack, size)
    page.click("#moreBtn")
    expect(page.locator("#moreMenu")).to_be_visible()
    asked = []
    page.once("dialog", lambda d: (asked.append(d.message), d.accept()))
    page.click("#signOut")
    page.wait_for_url(stack.url + "/login.html")
    assert asked == ["Sign out?"]
    # The session cookie is gone: the app sends you back to the login page.
    assert page.request.get(stack.url + "/api/me").status == 401
    page.goto(stack.url + "/")
    page.wait_for_url(stack.url + "/login.html")


def test_sign_out_cancelled_stays_signed_in(stack, ha, open_page):
    page = open_page(stack)
    page.click("#moreBtn")
    page.once("dialog", lambda d: d.dismiss())
    page.click("#signOut")
    page.wait_for_timeout(300)
    assert page.url == stack.url + "/"
    assert page.request.get(stack.url + "/api/me").ok


def test_service_worker_registers_and_offline_reload_shows_cached_plan(stack, ha, open_page):
    page = open_page(stack, service_workers="allow")
    scope = page.evaluate("navigator.serviceWorker.ready.then((r) => r.scope)")
    assert scope == stack.url + "/"
    page.reload()  # now controlled: the SW caches the page, layout and devices
    page.wait_for_function("!!navigator.serviceWorker.controller")
    expect(marker(page, "light.lounge")).to_be_visible()
    page.wait_for_timeout(500)
    page.context.set_offline(True)
    page.reload()
    expect(page.locator("#status")).to_have_text("Offline — showing last known state")
    expect(page.locator("#status")).to_have_class(re.compile(r"\boffline\b"))
    expect(marker(page, "light.lounge")).to_be_visible()
    assert page.locator("#plan .marker").count() == 9
    assert page.locator("#rooms .room").count() == 5
    expect(page.locator("#list li[data-dev]")).to_have_count(11)  # incl. the dehumidifier
    shot(page, "offline")
    page.context.set_offline(False)
