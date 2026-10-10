"""Morning brief (once per morning, dismissable, ⋯ → Morning brief) and the monthly energy report (⋯ → Energy report),
on a desktop and a 390px touch phone, in both themes. Fake HA history: e2e/fake_ha.py (TV 86.4 W 18–23 h, kettle 08:00)."""
import json
import time
from datetime import datetime

import pytest
from playwright.sync_api import expect

from conftest import Stack, shot

MORNING = "2026-10-09T08:00:00+01:00"
AFTERNOON = "2026-10-09T15:00:00+01:00"


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    """With a movable server clock (e2e/clock_app.py): "last night" and "yesterday" come from the server's clock, so
    a test that pins the browser to a time pins the server there too (on the real clock they failed 00:00-07:00)."""
    s = Stack(tmp_path_factory.mktemp("brief"), clock=True)
    yield s
    s.close()


@pytest.fixture(autouse=True)
def real_time(stack):
    stack.set_clock(time.time())  # every test starts on the real clock unless it pins one


def at(stack, iso):
    stack.set_clock(datetime.fromisoformat(iso).timestamp())
    return iso


def tariff(page, url, rate=24.5, standing=60):
    r = page.request.put(url + "/api/energy/settings", data=json.dumps({"rate_p": rate, "standing_p": standing}),
                         headers={"Content-Type": "application/json"})
    assert r.ok, r.text()


def auto_on(page, url):
    """Browser automation never gets the brief by itself unless hc.brief.auto is "1" (brief.js): opt in, reload."""
    page.evaluate("localStorage.setItem('hc.brief.auto', '1'); localStorage.removeItem('hc.brief.seen')")
    page.goto(url + "/")


def menu(page, item):
    page.click("#moreBtn")
    expect(page.locator("#moreMenu")).to_be_visible()
    page.click(item)


def no_overflow(page, sel):
    body = page.locator(f"{sel} .sheet-body")
    assert body.evaluate("e => e.scrollWidth <= e.clientWidth + 1"), "horizontal overflow"
    box = body.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= page.viewport_size["width"] + 1


def test_brief_opens_once_per_morning_and_can_be_turned_off(stack, ha, open_page):
    page = open_page(stack, "desktop", goto=None, clock=at(stack, MORNING))
    tariff(page, stack.url)
    auto_on(page, stack.url)
    sheet = page.locator("#briefSheet")
    expect(sheet).to_be_visible()
    expect(page.locator("#briefHello")).to_have_text("Good morning")
    data = page.request.get(stack.url + "/api/brief").json()
    expect(page.locator("#briefTitle")).to_have_text(data["label"])
    # weather: Met.no entity 12.4° partly cloudy
    expect(page.locator(".brief-wx .brief-wx-t")).to_have_text("12°")
    expect(page.locator(".brief-wx .brief-wx-c")).to_have_text("Partly cloudy")
    # energy: yesterday, priced
    e = data["energy"]
    assert e["available"] and e["kwh"] > 0.9 and e["cost_p"] == pytest.approx(round(e["kwh"] * 24.5, 2))
    expect(page.locator("#briefCost b")).to_have_text(f"{round(e['cost_p'])}p" if e["cost_p"] < 99.5 else f"£{e['cost_p'] / 100:.2f}")
    expect(page.locator(".brief-energy .brief-top")).to_contain_text("TV")
    # left on: lights on, the TV plug, the dehumidifier — not hidden / off devices
    ids = page.locator(".brief-left .brief-row").evaluate_all("rs => rs.map((r) => r.dataset.dev)")
    assert {"light.lounge", "light.strip", "switch.tv", "humidifier.dehumidifier"} <= set(ids) and "switch.kettle" not in ids
    assert ids == [x["entity_id"] for x in data["left_on"]]
    # last night's door log (generated history) or "All quiet"
    expect(page.locator(".brief-night h4")).to_contain_text("Last night · 22:00–07:00")
    n = data["night"]
    if n["events"]:
        expect(page.locator(".brief-night .ev").first).to_be_visible()
    shot(page, "brief-desktop-dark")
    page.click("#briefDone")
    expect(sheet).to_be_hidden()
    assert page.evaluate("localStorage.getItem('hc.brief.seen')") == "2026-10-09"
    # once per morning: a reload doesn't bring it back
    page.goto(stack.url + "/")
    page.locator("#plan .marker").first.wait_for()
    expect(page.locator("#wxChip")).to_be_visible()
    expect(sheet).to_be_hidden()
    # ⋯ → Morning brief opens it any time; "Show every morning" off is remembered
    menu(page, "#briefBtn")
    expect(sheet).to_be_visible()
    expect(page.locator("#briefAuto")).to_be_checked()
    page.uncheck("#briefAuto")
    assert page.evaluate("localStorage.getItem('hc.brief.auto')") == "0"
    page.keyboard.press("Escape")
    expect(sheet).to_be_hidden()
    page.evaluate("localStorage.removeItem('hc.brief.seen')")
    page.goto(stack.url + "/")
    page.locator("#plan .marker").first.wait_for()
    expect(page.locator("#wxChip")).to_be_visible()
    expect(sheet).to_be_hidden()
    assert not [c for c in ha.calls() if c["domain"] != "weather"]  # read-only: nothing switched


def test_brief_not_in_the_afternoon_and_rows_open_the_device(stack, ha, open_page):
    page = open_page(stack, "phone", goto=None, clock=at(stack, AFTERNOON))
    auto_on(page, stack.url)
    page.locator("#plan .marker").first.wait_for()
    expect(page.locator("#wxChip")).to_be_visible()
    expect(page.locator("#briefSheet")).to_be_hidden()
    menu(page, "#briefBtn")
    expect(page.locator("#briefHello")).to_have_text("Good afternoon")
    row = page.locator('.brief-row[data-dev="switch.tv"]')
    row.scroll_into_view_if_needed()
    row.tap()
    expect(page.locator("#briefSheet")).to_be_hidden()
    expect(page.locator("#sheet")).to_be_visible()
    expect(page.locator("#sheetContent h3")).to_have_text("TV")
    assert not ha.calls("turn_off") and not ha.calls("turn_on") and not ha.calls("toggle")


def test_report_months_and_rows(stack, ha, open_page):
    page = open_page(stack, "desktop")
    tariff(page, stack.url)
    menu(page, "#briefBtn")
    page.locator(".brief-energy .brief-go").click()
    expect(page.locator("#briefSheet")).to_be_hidden()
    sheet = page.locator("#reportSheet")
    expect(sheet).to_be_visible()
    expect(page.locator("#reportTotal")).to_be_visible()
    d = page.request.get(stack.url + "/api/energy/report").json()
    expect(page.locator("#reportMonth")).to_have_text(d["label"])
    rows = page.locator(".report-row")
    expect(rows).to_have_count(len([r for r in d["rows"] if not r["hidden"]]))
    first = d["rows"][0]
    expect(rows.first.locator(".name")).to_contain_text(first["name"])
    expect(rows.first.locator(".share")).to_have_text(f"{round(first['share_pct'])} %")
    assert abs(sum(r["share_pct"] for r in d["rows"]) - 100) < 0.5
    assert d["total_p"] == pytest.approx(round(d["total_kwh"] * 24.5, 2))
    # previous month, and back
    expect(page.locator("#reportNext")).to_be_disabled()
    page.click("#reportPrev")
    expect(page.locator("#reportMonth")).to_have_text(d["previous"]["label"])
    expect(page.locator("#reportTotal")).to_be_visible()
    expect(page.locator("#reportNext")).to_be_enabled()
    page.click("#reportNext")
    expect(page.locator("#reportMonth")).to_have_text(d["label"])
    page.keyboard.press("Escape")
    expect(sheet).to_be_hidden()


@pytest.mark.parametrize("scheme", ["dark", "light"])
@pytest.mark.parametrize("size", ["desktop", "phone"])
def test_looks(stack, ha, open_page, size, scheme):
    page = open_page(stack, size, clock=at(stack, MORNING), color_scheme=scheme)
    tariff(page, stack.url)
    assert page.evaluate("document.documentElement.dataset.theme") == scheme
    menu(page, "#briefBtn")
    expect(page.locator("#briefCost")).to_be_visible()
    expect(page.locator(".brief-wx .brief-wx-t")).to_be_visible()
    no_overflow(page, "#briefSheet")
    # every text in the cards is readable against the card
    bg = page.locator(".brief-card").first.evaluate("e => getComputedStyle(e).backgroundColor")
    assert bg != page.locator("#briefSheet .sheet-body").evaluate("e => getComputedStyle(e).backgroundColor")
    shot(page, f"brief-{size}-{scheme}")
    if size == "phone":
        page.locator(".brief-foot").scroll_into_view_if_needed()
        shot(page, f"brief-{size}-{scheme}-bottom")
    page.locator(".brief-energy .brief-go").click()
    expect(page.locator("#reportTotal")).to_be_visible()
    no_overflow(page, "#reportSheet")
    shot(page, f"report-{size}-{scheme}")
