"""Octopus tariff and the appliance run log end to end, with the fake Octopus in e2e/fake_ha.py (never the real API):
switching it on, today's / tomorrow's prices, the cheapest time to run the washer (sheet + appliance sheet), changing
region, stale prices when Octopus is down, costs in the Energy sheet, a scripted washer cycle logged with energy and
cost, adding a note to it, and what members and guests get. Desktop mouse and a 390px touch phone, light and dark.

Runs on its own stack: e2e/clock_app.py (movable server clock) and a fake HA with the appliance plugs (switch.washer…).
Each test moves the server clock to its own later day, so tests never see each other's runs."""
import copy
import json
import re
import time
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from conftest import LAYOUT, WAIT, Stack, Touch, login, plan_xy, shot

LONDON = ZoneInfo("Europe/London")
WASHER = {"id": "washer", "type": "washer", "x": 6.75, "y": 1.35, "w": 0.6, "h": 0.6, "rot": 0, "plug": "switch.washer"}
KETTLE = {"id": "kettle", "type": "kettle", "x": 7.6, "y": 1.95, "w": 0.25, "h": 0.25, "rot": 0, "plug": "switch.kettle"}
LINKED = {**copy.deepcopy(LAYOUT), "furniture": [WASHER, KETTLE]}
SIZES_THEMES = [("desktop", "dark"), ("phone", "light"), ("desktop", "light"), ("phone", "dark")]
OFF = {"enabled": False, "product": "AGILE-24-10-01", "region": "C", "use_for_costs": True, "remind": False}


@pytest.fixture(scope="module")
def tstack(tmp_path_factory):
    s = Stack(tmp_path_factory.mktemp("tariff"), clock=True, appliances=True)
    yield s
    s.close()


@pytest.fixture
def tha(tstack):
    tstack.ha.reset()
    return tstack.ha


def local_ts(days_ahead: int, hh: int, mm: int = 0) -> float:
    d = date.today() + timedelta(days=days_ahead)
    return datetime.combine(d, dtime(hh, mm), tzinfo=LONDON).timestamp()


def slots_in(days_ahead: int) -> int:
    d = date.today() + timedelta(days=days_ahead)
    a = datetime.combine(d, dtime(0), tzinfo=LONDON)
    b = datetime.combine(d + timedelta(days=1), dtime(0), tzinfo=LONDON)
    return int((b.timestamp() - a.timestamp()) // 1800)


def api(page, stack, method, path, body=None):
    r = page.request.fetch(stack.url + path, method=method, data=json.dumps(body) if body is not None else None,
                           headers={"Content-Type": "application/json"})
    return r


def reset_tariff(page, stack):
    assert api(page, stack, "PUT", "/api/tariff/settings", OFF).ok


def octo(stack, body):
    stack.ha._req("/fake/octopus", body)


def octo_calls(stack):
    return stack.ha._req("/fake/octopus_calls")


def press(page, locator):
    locator.tap() if page.size in ("phone", "mobile") else locator.click()


def open_tariff(page):
    press(page, page.locator("#moreBtn"))
    expect(page.locator("#moreMenu")).to_be_visible()
    press(page, page.locator("#tariffMenuBtn"))
    expect(page.locator("#tariffSheet")).to_be_visible()


def no_hscroll(page, sel=None):
    assert page.evaluate("document.scrollingElement.scrollWidth <= innerWidth"), "horizontal page scroll"
    if sel:
        assert page.evaluate(f"(() => {{ const b = document.querySelector('{sel}'); return b.scrollWidth <= b.clientWidth; }})()"), f"{sel} scrolls sideways"


def long_press(page, x, y):
    if page.size in ("phone", "mobile"):
        Touch(page).hold(x, y, ms=700)
    else:
        page.mouse.move(x, y)
        page.mouse.down()
        page.wait_for_timeout(700)
        page.mouse.up()


# ---------------- the tariff sheet ----------------
@pytest.mark.parametrize("size,theme", SIZES_THEMES)
def test_switch_on_prices_and_cheapest_time(tstack, tha, open_page, size, theme):
    day = 1 + SIZES_THEMES.index((size, theme))  # each run on its own day
    tstack.set_clock(local_ts(day, 22, 10))
    page = open_page(tstack, size, layout=LINKED, color_scheme=theme)
    reset_tariff(page, tstack)
    open_tariff(page)
    expect(page.locator("#tariffSettings")).to_be_visible()
    expect(page.locator("#tariffEnabled")).not_to_be_checked()
    expect(page.locator("#tariffNow")).to_have_count(0)
    assert octo_calls(tstack) == []  # off by default: nothing fetched
    press(page, page.locator("#tariffEnabled"))
    expect(page.locator("#tariffNow b")).to_have_text(re.compile(r"^\d+p$"))
    expect(page.locator("#tariffContent > .sub")).to_have_text("Agile Octopus · London (C)")
    calls = octo_calls(tstack)
    assert len(calls) == 1 and calls[0]["code"] == "E-1R-AGILE-24-10-01-C" and calls[0]["page_size"] == "1500"
    expect(page.locator("#tariffBars .tf-bar")).to_have_count(slots_in(day))
    expect(page.locator("#tariffBars .tf-bar.now")).to_have_count(1)
    # the washer: a 2 h run (from its history), cheapest 01:00–03:00 tomorrow at 7.5p
    row = page.locator('#tariffSuggest li[data-appl="washer"]')
    expect(row.locator(".tf-when")).to_have_text("01:00–03:00 tomorrow")
    expect(row.locator(".tf-row-sub")).to_contain_text("avg 7.5p/kWh")
    expect(row.locator(".tf-row-src")).to_have_text(re.compile(r"Typical run 2 h"))
    expect(page.locator('#tariffSuggest li[data-appl="kettle"]')).to_have_count(0)  # only cycle appliances
    # tomorrow's prices; a bar's price on tap / hover
    press(page, page.locator("#tariffSheet .seg button", has_text="Tomorrow"))
    expect(page.locator("#tariffBars .tf-bar")).to_have_count(slots_in(day + 1))
    expect(page.locator("#tariffSheet .tf-sum")).to_contain_text("Low 7.5p at 01:00")
    bar = page.locator("#tariffBars .tf-bar").nth(2)  # 01:00
    press(page, bar)
    expect(page.locator("#tariffCap")).to_have_text("01:00–01:30 · 7.5p/kWh")
    expect(page.locator("#tariffBars .tf-bar.b-cheap")).to_have_count(6)
    expect(page.locator("#tariffBars .tf-bar.b-dear")).to_have_count(6)
    shot(page, f"tariff-sheet-{size}-{theme}")
    if size == "phone":
        no_hscroll(page, "#tariffSheet .sheet-body")
    # another region fetches its own prices at once
    page.locator("#tariffRegion").select_option("J")
    expect(page.locator("#tariffContent > .sub")).to_have_text("Agile Octopus · South Eastern England (J)")
    assert octo_calls(tstack)[-1]["code"] == "E-1R-AGILE-24-10-01-J"
    # an unknown product: Octopus says 404, the sheet says so
    page.locator("#tariffProduct").select_option("other")
    page.locator("#tariffCode").fill("FLUX-IMPORT-23-02-14")
    page.locator("#tariffCode").press("Tab")  # change fires on leaving the field
    expect(page.locator("#tariffStatus")).to_contain_text("check the product code")
    page.locator("#tariffProduct").select_option("GO-VAR-22-10-14")
    expect(page.locator("#tariffContent > .sub")).to_have_text("Octopus Go · South Eastern England (J)")
    expect(page.locator("#tariffStatus")).to_have_count(0)
    reset_tariff(page, tstack)
    assert not page.errors


def test_stale_prices_costs_and_reminder_setting(tstack, tha, open_page):
    t = local_ts(6, 12, 0)
    tstack.set_clock(t)
    page = open_page(tstack, "desktop", layout=LINKED)
    reset_tariff(page, tstack)
    assert api(page, tstack, "PUT", "/api/tariff/settings", {"enabled": True}).json()["current"] is not None
    octo(tstack, {"fail": True})
    tstack.set_clock(t + 60)  # forced refreshes are at most every 30 s
    r = api(page, tstack, "POST", "/api/tariff/refresh").json()
    assert r["stale"] and "503" in r["error"]
    open_tariff(page)
    expect(page.locator("#tariffStatus")).to_contain_text("Showing the last known prices")
    expect(page.locator("#tariffNow b")).to_have_text(re.compile(r"^\d+p$"))  # still the rates from before
    # the reminder: off by default, its lead time only while on
    expect(page.locator("#tariffRemind")).not_to_be_checked()
    expect(page.locator("#tariffLead")).to_be_disabled()
    page.click("#tariffRemind")
    expect(page.locator("#tariffLead")).to_be_enabled()
    page.locator("#tariffLead").select_option("30")
    page.wait_for_function("tf.data.settings.remind_lead_min === 30")
    s = api(page, tstack, "GET", "/api/tariff").json()["settings"]
    assert s["remind"] is True and s["remind_lead_min"] == 30
    page.click("#tariffClose")
    # energy costs are priced with the half-hourly rates
    page.click("#moreBtn")
    page.click("#energyBtn")
    expect(page.locator("#sheetContent .energy-body .sub").first).to_have_text("Priced at Agile Octopus half-hourly rates (London).")
    octo(tstack, {})
    reset_tariff(page, tstack)


# ---------------- run log ----------------
@pytest.mark.parametrize("size,theme", [("desktop", "dark"), ("mobile", "light")])
def test_washer_run_logged_with_cost_and_note(tstack, tha, open_page, size, theme):
    day = 8 + (size == "mobile")
    t0 = local_ts(day, 10, 0)
    tstack.set_clock(t0)
    page = open_page(tstack, size, layout=LINKED, color_scheme=theme)
    reset_tariff(page, tstack)
    assert api(page, tstack, "PUT", "/api/tariff/settings", {"enabled": True}).ok

    def runs():
        return api(page, tstack, "GET", "/api/appliances/washer/runs").json()

    before = len(runs()["runs"])
    tha.set("sensor.washer_power", "480")                 # the drum starts
    page.wait_for_function("st.devices.get('switch.washer').power === 480")
    tstack.set_clock(t0 + 130)                            # running after 2 min
    end = time.time() + WAIT
    while time.time() < end and not runs()["current"]:
        page.wait_for_timeout(200)
    assert runs()["current"]["start"] == int(t0 * 1000) or abs(runs()["current"]["start"] - t0 * 1000) < 5000
    tstack.set_clock(t0 + 60 * 60)
    tha.set("sensor.washer_power", "1.1")                 # done
    page.wait_for_function("st.devices.get('switch.washer').power === 1.1")
    tstack.set_clock(t0 + 63 * 60 + 5)                    # quiet for 3 min: finished
    end = time.time() + WAIT
    while time.time() < end and len(runs()["runs"]) == before:
        tstack.set_clock(t0 + 63 * 60 + 10)               # wakes the loops again (the order they run in varies)
        page.wait_for_timeout(300)
    run = runs()["runs"][0]
    assert run["outcome"] == "finished" and 59 <= run["minutes"] <= 61
    assert run["kwh"] == pytest.approx(0.48, abs=0.02) and run["cost_p"] == pytest.approx(0.48 * 22.5, abs=1.0)
    # the appliance sheet: cheapest time to run, the run history, a note
    long_press(page, *plan_xy(page, 6.5, 1.6))
    sheet = page.locator("#sheetContent")
    expect(sheet.locator(".appl-sec")).to_be_visible()
    expect(sheet.locator("#applCheapest .sum-row span").last).to_have_text("01:00–03:00 tomorrow")
    press(page, sheet.locator("#applRuns summary"))
    item = sheet.locator(f'#applRuns li[data-run="{run["id"]}"]')
    expect(item.locator(".tf-run-top")).to_contain_text("10:00–11:00")
    expect(item.locator(".tf-run-sub")).to_have_text(re.compile(r"^0\.4\d kWh · 1\dp$"))
    press(page, item.locator(".tf-note-btn"))
    expect(page.locator("#runNoteDialog")).to_be_visible()
    page.locator("#runNoteForm [name=note]").fill("40° cotton, towels")
    press(page, page.locator("#runNoteOk"))
    expect(page.locator("#runNoteDialog")).to_be_hidden()
    expect(item.locator(".tf-note")).to_have_text("40° cotton, towels")
    assert runs()["runs"][0]["note"] == "40° cotton, towels"
    expect(item.locator(".tf-note-btn")).to_have_text("✎ Edit note")
    item.scroll_into_view_if_needed()
    shot(page, f"tariff-runs-{size}-{theme}")
    if size == "mobile":
        no_hscroll(page, "#sheet .sheet-body")
    reset_tariff(page, tstack)


# ---------------- roles ----------------
def add_user(page, stack, name, role, pw="password-1"):
    r = api(page, stack, "POST", "/api/users", {"username": name, "password": pw, "role": role})
    assert r.ok, r.text()
    return pw


@pytest.mark.parametrize("size,theme", [("desktop", "light"), ("phone", "dark")])
def test_guest_has_no_tariff_or_runs(tstack, tha, open_page, size, theme):
    admin = open_page(tstack, layout=LINKED)
    name = f"gst{size[0]}{theme[0]}"
    pw = add_user(admin, tstack, name, "guest")
    page = open_page(tstack, size, signed_in=False, color_scheme=theme)
    login(page, tstack.url, name, pw)
    page.locator("#plan .marker").first.wait_for()
    press(page, page.locator("#moreBtn"))
    expect(page.locator("#moreMenu")).to_be_visible()
    expect(page.locator("#tariffMenuBtn")).to_be_hidden()
    for method, path, body in (("GET", "/api/tariff", None), ("POST", "/api/tariff/refresh", None),
                               ("PUT", "/api/tariff/settings", {"enabled": True}), ("GET", "/api/appliances/washer/runs", None),
                               ("PUT", "/api/appliances/washer/runs/1", {"note": "x"})):
        assert api(page, tstack, method, path, body).status == 403, path
    shot(page, f"tariff-guest-menu-{size}-{theme}")


def test_member_sees_prices_without_settings(tstack, tha, open_page):
    tstack.set_clock(local_ts(10, 9, 0))
    admin = open_page(tstack, layout=LINKED)
    reset_tariff(admin, tstack)
    assert api(admin, tstack, "PUT", "/api/tariff/settings", {"enabled": True}).ok
    pw = add_user(admin, tstack, "tmem", "member")
    page = open_page(tstack, "phone", signed_in=False, color_scheme="light")
    login(page, tstack.url, "tmem", pw)
    page.locator("#plan .marker").first.wait_for()
    open_tariff(page)
    expect(page.locator("#tariffNow b")).to_have_text(re.compile(r"^\d+p$"))
    expect(page.locator("#tariffSuggest li")).to_have_count(1)
    expect(page.locator("#tariffSettings")).to_have_count(0)
    assert api(page, tstack, "PUT", "/api/tariff/settings", {"enabled": False}).status == 403
    shot(page, "tariff-member-phone-light")
    no_hscroll(page, "#tariffSheet .sheet-body")
    reset_tariff(admin, tstack)
