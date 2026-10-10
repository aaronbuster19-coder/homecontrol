"""Browser tests for the standby saver (plug sheet + ⋯ → Energy): fake HA (e2e/fake_ha.py: the TV idles at 4 W
overnight) and a movable server clock (`clock_stack`, e2e/clock_app.py), so 01:00 and 07:00 come at once.

    python -m pytest -q e2e/test_standby.py      # SHOTS=dir to keep screenshots
"""
import json
import re
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from conftest import WAIT, hold, marker, shot

LONDON = ZoneInfo("Europe/London")
TV = "switch.tv"


def night():
    """Tomorrow's 01:00 and 07:00 in London (epoch seconds)."""
    d = datetime.now(LONDON).date() + timedelta(days=1)
    return tuple(datetime.combine(d, dtime(h), tzinfo=LONDON).timestamp() for h in (1, 7))


def put(page, stack, path, body):
    r = page.request.put(stack.url + path, data=json.dumps(body), headers={"Content-Type": "application/json"})
    assert r.ok, r.text()
    return r.json()


def saver(page, stack):
    r = page.request.get(stack.url + "/api/standby")
    assert r.ok, r.text()
    return r.json()


def wait_log(page, stack, action, timeout=WAIT):
    end = time.time() + timeout
    while True:
        log = saver(page, stack)["log"]
        if log and log[0]["action"] == action:
            return log[0]
        if time.time() > end:
            raise AssertionError(f"no {action!r} in {log}")
        page.wait_for_timeout(100)


def switch_calls(ha):
    return [c for c in ha.calls() if c["domain"] == "switch" and TV in json.dumps(c["data"])]


def plug_sheet(page, eid):
    hold(page, marker(page, eid).locator("circle"), 700)
    expect(page.locator("#sheet")).to_be_visible()


@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_standby_saver_night_and_morning(clock_stack, open_page, size):
    st, ha = clock_stack, clock_stack.ha
    t_off, t_on = night()
    st.set_clock(t_off - 600)  # 00:50
    page = open_page(st, size)
    put(page, st, "/api/energy/settings", {"rate_p": 24.5, "standing_p": None})
    ha.set("sensor.tv_power", "4.0")  # TV in standby
    page.reload()
    expect(marker(page, TV)).to_be_visible()

    plug_sheet(page, TV)
    row = page.locator("#standbyRow")
    expect(row.locator("input[data-standby]")).not_to_be_checked()
    expect(row).to_contain_text("would save ≈ £2.15/year")  # 4 W × 6 h × 365 = 8.76 kWh × 24.5p
    row.locator("input[data-standby]").check()
    expect(row.locator(".sb-info")).to_have_text("Off at 01:00 if below 9 W for 15 min, back on at 07:00 · Saves ≈ £2.15/year")
    expect(page.locator("#sbThreshold")).to_have_value("9")
    expect(row).to_contain_text("Suggested threshold 9 W (standby 4 W + 5 W).")
    shot(page, f"standby-{size}-plug-sheet")
    page.click("#sheetClose")
    assert not switch_calls(ha)

    # 01:00, idle for 15 min: one switch-off
    st.set_clock(t_off + 1)
    ha.wait_call(lambda c: c["domain"] == "switch" and c["service"] == "turn_off" and c["data"] == {"entity_id": [TV]})
    assert wait_log(page, st, "off")["note"] == "standby 4.0 W"
    page.click("#moreBtn")
    page.click("#energyBtn")
    sec = page.locator("#standbySaver")
    expect(sec.locator(f'li[data-plug="{TV}"]')).to_contain_text("Off by the saver since 01:00 — back on at 07:00.")
    sec.locator("#standbyLog summary").click()
    expect(sec.locator("#standbyLog .sum-row").first).to_contain_text("Turned off: TV — standby 4.0 W")
    shot(page, f"standby-{size}-energy-sheet")
    page.click("#sheetClose")
    assert len(switch_calls(ha)) == 1

    # 07:00: it was ours and nobody touched it -> back on, once
    st.set_clock(t_on + 1)
    ha.wait_call(lambda c: c["domain"] == "switch" and c["service"] == "turn_on" and c["data"] == {"entity_id": [TV]})
    wait_log(page, st, "on")
    page.wait_for_timeout(500)
    assert [c["service"] for c in switch_calls(ha)] == ["turn_off", "turn_on"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_in_use_at_night_is_never_cut(clock_stack, open_page):
    st, ha = clock_stack, clock_stack.ha
    t_off, t_on = night()
    st.set_clock(t_off - 600)
    page = open_page(st, "desktop")
    put(page, st, f"/api/standby/{TV}", {"enabled": True})
    ha.set("sensor.tv_power", "120")  # someone is watching at 01:00
    st.set_clock(t_off + 1)
    log = wait_log(page, st, "skipped")
    assert log["note"].startswith("in use (120.0 W")
    st.set_clock(t_on + 1)
    page.wait_for_timeout(800)
    assert not switch_calls(ha)  # never off, so never on either
    page.click("#moreBtn")
    page.click("#energyBtn")
    page.locator("#standbyLog summary").click()
    expect(page.locator("#standbyLog .sum-row").first).to_contain_text("Skipped: TV — in use (120.0 W, threshold 9 W)")


def test_keep_on_plug_cannot_be_enabled(clock_stack, open_page):
    st = clock_stack
    page = open_page(st, "phone")
    L = page.request.get(st.url + "/api/layout").json()
    put(page, st, "/api/layout", {**L, "settings": {**(L.get("settings") or {}), "keep_on": ["switch.kettle"]}})
    page.reload()
    expect(marker(page, "switch.kettle")).to_be_visible()
    plug_sheet(page, "switch.kettle")
    row = page.locator("#standbyRow")
    expect(row.locator("input[data-standby]")).to_be_disabled()
    expect(row).to_contain_text("Keep-on plugs always stay on.")
    r = page.request.put(st.url + "/api/standby/switch.kettle", data=json.dumps({"enabled": True}),
                         headers={"Content-Type": "application/json"})
    assert r.status == 400
    shot(page, "standby-phone-keep-on")
