"""History charts, the door log, the bell (alerts + automations) sheet and the weekly summary preview."""
import re

import pytest
from playwright.sync_api import expect

from conftest import Touch, center, hold, marker, marker_center, shot

WEEKDAY = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)$")


def open_history(page):
    box = page.locator("#sheetContent details.hist")
    if not box.evaluate("d => d.open"):
        box.locator("summary > span").first.click()
    return box


@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_plug_history_chart_24h_and_7d(stack, ha, open_page, size):
    page = open_page(stack, size)
    ranges = []
    page.on("request", lambda r: ranges.append(r.url.split("range=")[1]) if "/api/history/" in r.url else None)
    el = marker(page, "switch.tv").locator("circle").first
    if size == "mobile":
        Touch(page).hold(*center(el))
    else:
        hold(page, el, 700)
    expect(page.locator("#sheetContent h3")).to_have_text("TV")
    box = open_history(page)
    chart = box.locator("svg.hist-chart")
    expect(chart.locator("path.line.s0")).to_have_count(1)
    expect(box.locator(".hist-readout")).to_have_text(re.compile(r"^\d+\.\d\d kWh in 24h · peak \d+(\.\d)? W$"))
    ticks = chart.locator("text.tick:not(.ylab)").all_text_contents()
    assert len(ticks) >= 3 and all(re.fullmatch(r"\d\d:00", t) for t in ticks), ticks
    assert chart.locator("path.line.s0").get_attribute("d").count("L") > 20  # a real line, not a stub
    shot(page, f"history-plug-24h-{size}")
    box.locator(".seg button", has_text="7d").click()
    expect(box.locator(".hist-readout")).to_have_text(re.compile(r"kWh in 7d"))
    ticks = chart.locator("text.tick:not(.ylab)").all_text_contents()
    assert len(ticks) >= 6 and all(WEEKDAY.match(t) for t in ticks), ticks
    expect(box.locator(".seg button.active")).to_have_text("7d")
    assert ranges == ["24h", "7d"]
    # Tap / hover on the chart shows the value at that time.
    b = chart.bounding_box()
    page.mouse.move(b["x"] + b["width"] * 0.6, b["y"] + b["height"] / 2)
    if size == "mobile":
        Touch(page).tap(b["x"] + b["width"] * 0.6, b["y"] + b["height"] / 2)
    expect(box.locator(".hist-readout")).to_have_class(re.compile(r"\bactive\b"))
    expect(box.locator(".hist-readout")).to_have_text(re.compile(r"^\w{3} \d+ \w{3} \d\d:\d\d · \d+(\.\d)? W$"))
    shot(page, f"history-plug-7d-{size}")


def test_valve_and_light_history(stack, ha, open_page):
    page = open_page(stack)
    page.mouse.click(*marker_center(page, "climate.lounge_valve"))
    box = open_history(page)
    expect(box.locator("path.line.s0")).to_have_count(1)  # current
    expect(box.locator("path.line.s1.step")).to_have_count(1)  # target, as steps
    expect(box.locator(".hist-legend")).to_contain_text("Target")
    expect(box.locator(".hist-readout")).to_have_text(re.compile(r"^Current \d+(\.\d)?–\d+(\.\d)?°$"))
    page.click("#sheetClose")
    # The open state is remembered: a light's sheet shows its on/off bars straight away.
    hold(page, marker(page, "light.kitchen").locator("circle").first, 700)
    box = page.locator("#sheetContent details.hist")
    expect(box.locator("rect.bar.light.on").first).to_be_attached()
    expect(box.locator(".hist-readout")).to_have_text(re.compile(r"^On .+ in total · \d+ times?$"))
    shot(page, "history-light")


@pytest.mark.parametrize("size", ["desktop", "mobile"])
def test_door_log(stack, ha, open_page, size):
    page = open_page(stack, size)
    log = page.locator("#list li.group button.group-act", has_text="Log")
    (log.tap if size == "mobile" else log.click)()
    expect(page.locator("#sheetContent h3")).to_have_text("Door log")
    doors = page.locator("#sheetContent section.door")
    expect(doors).to_have_count(2)
    expect(doors.locator(".door-head .name")).to_have_text(["Bedroom window", "Front door"])  # by name
    front = doors.nth(1)
    expect(front.locator(".badge")).to_have_text("Closed")
    expect(front.locator("li.ev").first).to_be_visible()
    n24 = front.locator("li.ev").count()
    assert n24 >= 4
    expect(front.locator("li.ev.closed .what").first).to_have_text(re.compile(r"^Closed · open for (\d+ min|<1 min)$"))
    expect(front.locator(".sub")).to_have_text(re.compile(r"^\d+ opens? today"))
    shot(page, f"door-log-{size}")
    page.locator("#sheetContent .seg button", has_text="7d").click()
    expect(page.locator("#sheetContent .seg button.active")).to_have_text("7d")
    page.wait_for_function(f"document.querySelectorAll('#sheetContent section.door')[1].querySelectorAll('li.ev').length > {n24}")
    # From a sensor's own sheet too.
    page.click("#sheetClose")
    el = marker(page, "binary_sensor.contact_sensor_door").locator("circle").first
    (Touch(page).tap(*center(el)) if size == "mobile" else el.click())
    expect(page.locator("#sheetContent .badge")).to_have_text("Closed")
    page.click("#sheetContent button.linkish")
    expect(page.locator("#sheetContent h3")).to_have_text("Door log")


def test_bell_sheet_sections_and_settings(fresh_stack, open_page):
    page = open_page(fresh_stack)
    page.click("#alertsBtn")
    sheet = page.locator("#alertSheet")
    expect(sheet).to_be_visible()
    expect(sheet.locator("h4")).to_have_text(["Doors & windows", "Device health", "Weekly summary"])
    expect(page.locator("#winHeat")).to_be_checked()
    expect(page.locator("#healthBattery")).to_be_checked()
    expect(page.locator("#weeklySummary")).to_be_checked()
    expect(page.locator("#winMins")).to_have_value("2")
    expect(page.locator("#winInfo")).to_have_text(re.compile("Link window sensors in Edit mode"))
    shot(page, "bell-sheet")
    page.uncheck("#healthBattery")
    expect(page.locator("#alertMsg")).to_have_text("Saved.")
    s = page.request.get(fresh_stack.url + "/api/alerts/settings").json()
    assert s["health_battery"] is False and s["window_heating_enabled"] is True
    page.fill("#winMins", "99")
    page.locator("#winMins").dispatch_event("change")
    expect(page.locator("#alertMsg")).to_have_text("Window minutes must be 1–30.")
    page.click("#alertClose")
    expect(sheet).to_be_hidden()


def test_summary_preview_sheet(stack, ha, open_page):
    page = open_page(stack, "mobile")
    page.tap("#alertsBtn")
    page.tap("#summaryPreview")
    sheet = page.locator("#summarySheet")
    expect(sheet).to_be_visible(timeout=10000)
    expect(page.locator("#alertSheet")).to_be_hidden()
    expect(sheet.locator("h3")).to_have_text("This week")
    expect(page.locator("#summarySub")).to_have_text(re.compile(r"^Preview · \w{3},? \d+ \w{3} – \w{3},? \d+ \w{3}$"))
    expect(sheet.locator(".sum-big b")).to_have_text(re.compile(r"^\d+\.\d kWh$"))
    heads = sheet.locator(".sum-sec h4").all_text_contents()
    assert heads == ["Energy by plug", "Doors & windows opened", "Average temperature"], heads
    expect(sheet.locator(".sum-sec").nth(0).locator(".sum-row")).to_have_count(2)  # TV and kettle
    shot(page, "summary-preview")
    page.tap("#summaryClose")
    expect(sheet).to_be_hidden()
    assert ha.calls() == []  # a preview never switches anything
