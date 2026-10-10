"""Browser tests for Auto Away (⋯ → Auto Away…): fake HA people (e2e/fake_ha.py) and a movable server clock
(e2e/clock_app.py via the `clock_stack` fixture), so "everyone left 5 min ago" takes no real waiting.

    python -m pytest -q e2e/test_presence.py      # SHOTS=dir to keep screenshots
"""
import json
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from conftest import WAIT, shot

ALEX, SAM = "person.alex", "person.sam"
LONDON = ZoneInfo("Europe/London")


def api(page, stack, path):
    r = page.request.get(stack.url + path)
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


def tap(page, loc):
    loc = page.locator(loc) if isinstance(loc, str) else loc
    loc.tap() if page.size in ("phone", "mobile") else loc.click()


def open_auto_away(page):
    tap(page, "#moreBtn")
    tap(page, "#autoAwayBtn")
    expect(page.locator("#presSheet")).to_be_visible()


def reopen(page):
    page.locator("#presSheet .close").click()
    open_auto_away(page)


@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_auto_away_everyone_leaves_and_comes_back(clock_stack, open_page, size):
    st, ha = clock_stack, clock_stack.ha
    page = open_page(st, size)
    open_auto_away(page)
    expect(page.locator("#presStatus")).to_have_text(re.compile(r"^Off"))
    people = page.locator("#presPeople label")
    expect(people).to_have_count(2)
    expect(people.nth(0)).to_contain_text("Alex")
    expect(people.nth(0)).to_contain_text("Home")
    expect(page.locator("#presOpts")).to_have_class(re.compile(r"\boff\b"))
    tap(page, "#presOn")
    expect(page.locator("#presMsg")).to_have_text("Saved.")
    page.fill("#presMins", "5")
    page.locator("#presMins").dispatch_event("change")
    expect(page.locator("#presMsg")).to_have_text("Saved.")
    assert api(page, st, "/api/presence")["away_minutes"] == 5
    expect(page.locator("#presStatus")).to_have_text("Auto: Alex & Sam are home.")

    # both leave (Sam's phone reports a zone): the server notices, the status counts down
    ha.set(ALEX, "not_home")
    ha.set(SAM, "Work")
    d = wait_api(page, st, "/api/presence", lambda d: d["everyone_out"])
    assert d["away_at"] - d["out_since"] == 5 * 60_000
    reopen(page)
    since = datetime.fromtimestamp(d["out_since"] / 1000, LONDON).strftime("%H:%M")  # the browser is in London
    expect(page.locator("#presStatus")).to_have_text(re.compile(rf"^Auto: everyone out since {since}, Away in [45] min\.$"))
    expect(page.locator("#presPeople label").nth(1)).to_contain_text("Away (Work)")
    shot(page, f"presence-{size}-pending")
    assert not [c for c in ha.calls() if c["service"] in ("turn_off", "set_temperature")]

    # 5 minutes later: exactly Away from the ⋯ menu — lights + plugs off, radiators 16°
    st.set_clock(d["away_at"] / 1000 + 1)
    ha.wait_call(lambda c: c["service"] == "set_temperature" and c["data"].get("temperature") == 16)
    offs = ha.calls("turn_off")
    assert {c["domain"] for c in offs} == {"light", "switch"}
    assert "switch.tv" in json.dumps(offs) and "light.lounge" in json.dumps(offs)
    assert wait_api(page, st, "/api/mode", lambda m: m["mode"] == "away")
    reopen(page)
    expect(page.locator("#presStatus")).to_have_text(re.compile(rf"^Auto: everyone out since {since} — Away\.$"))
    expect(page.locator("#presLog")).to_contain_text("Switched to Away — everyone out since")
    shot(page, f"presence-{size}-away")
    page.locator("#presSheet .close").click()
    page.reload()
    expect(page.locator("#awayBadge")).to_be_visible()
    n_calls = len(ha.calls())
    page.wait_for_timeout(500)
    assert len(ha.calls()) == n_calls  # no repeats

    # Alex comes home: after a minute it's Home again, radiators back to their targets
    ha.set(ALEX, "home")
    d = wait_api(page, st, "/api/presence", lambda d: d["arrival_at"])
    st.set_clock(d["arrival_at"] / 1000 + 1)
    ha.wait_call(lambda c: c["service"] == "set_temperature" and c["data"].get("temperature") == 21)
    assert wait_api(page, st, "/api/mode", lambda m: m["mode"] == "home")
    open_auto_away(page)
    expect(page.locator("#presStatus")).to_have_text("Auto: Alex is home.")
    expect(page.locator("#presLog .sum-row").first).to_contain_text("Switched to Home — Alex arrived")
    shot(page, f"presence-{size}-home")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_manual_wins_and_unavailable_ignored(clock_stack, open_page):
    st, ha = clock_stack, clock_stack.ha
    page = open_page(st, "desktop")
    r = page.request.put(st.url + "/api/presence/settings", data=json.dumps({"enabled": True, "away_minutes": 2}),
                         headers={"Content-Type": "application/json"})
    assert r.ok
    # Away pressed by hand, then "I'm home" by hand
    tap(page, "#moreBtn")
    tap(page, "#awayBtn")
    page.click("#modeOk")
    expect(page.locator("#awayBadge")).to_be_visible()
    tap(page, "#moreBtn")
    tap(page, "#awayBtn")
    page.click("#modeOk")
    expect(page.locator("#awayBadge")).to_be_hidden()
    ha.reset()
    ha.set(ALEX, "not_home")
    ha.set(SAM, "unavailable")  # Sam's phone is off: unchanged (home), so not everyone is out
    page.wait_for_timeout(300)
    d = api(page, st, "/api/presence")
    assert not d["everyone_out"]
    ha.set(SAM, "not_home")
    d = wait_api(page, st, "/api/presence", lambda d: d["everyone_out"])
    st.set_clock(d["away_at"] / 1000 + 1)
    d = wait_api(page, st, "/api/presence", lambda d: d["blocked"] == "manual")
    open_auto_away(page)
    expect(page.locator("#presStatus")).to_have_text(re.compile(r"set by hand, so not before \d\d:\d\d\.$"))
    page.wait_for_timeout(300)
    assert not ha.calls("turn_off") and api(page, st, "/api/mode")["mode"] == "home"
    # the Away dialog links here too
    page.locator("#presSheet .close").click()
    tap(page, "#moreBtn")
    tap(page, "#awayBtn")
    page.click("#modeAuto")
    expect(page.locator("#modeDialog")).to_be_hidden()
    expect(page.locator("#presSheet")).to_be_visible()


def test_no_people_explains_companion_app(clock_stack, open_page):
    st = clock_stack
    page = open_page(st, "phone")
    page.route("**/api/presence", lambda route: route.fulfill(json={**route.fetch().json(), "people": []}))
    open_auto_away(page)
    expect(page.locator("#presNone")).to_contain_text("Home Assistant companion app")
    shot(page, "presence-phone-no-people")


def test_bell_sheet_push_toggle(clock_stack, open_page):
    st = clock_stack
    page = open_page(st, "desktop")
    page.click("#alertsBtn")
    box = page.locator("#presNotify")
    expect(box).to_be_checked()
    box.uncheck()
    expect(page.locator("#alertMsg")).to_have_text("Saved.")
    assert api(page, st, "/api/presence")["notify"] is False
