"""Light / dark theme: Auto follows the system, ⋯ → Theme overrides it per device, applied before first paint;
every screen readable in both (WCAG AA text contrast in light), wall mode dark at night."""
import re

import pytest
from playwright.sync_api import expect

from conftest import LAYOUT, PASSWORD, USER, hold, marker, shot
from test_furniture import FURNISHED

SIZES = ["desktop", "phone"]
BG = {"light": "rgb(238, 235, 230)", "dark": "rgb(20, 23, 28)"}
META = {"light": "#eeebe6", "dark": "#14171c"}

# Contrast ratio between an element's text colour and the background behind it (first non-transparent ancestor,
# or an explicit backdrop element), from computed styles.
CONTRAST_JS = """([sel, bgSel, prop]) => {
  const parse = (c) => { const m = c.match(/rgba?\\(([^)]+)\\)/); if (!m) return null;
    const v = m[1].split(/[ ,/]+/).filter(Boolean).map(Number); return { rgb: v.slice(0, 3), a: v.length > 3 ? v[3] : 1 }; };
  const lum = ([r, g, b]) => { const f = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const el = document.querySelector(sel); if (!el) return null;
  const fg = parse(getComputedStyle(el)[prop || "color"]);
  let bg = null;
  if (bgSel) { const b = document.querySelector(bgSel), cs = getComputedStyle(b); bg = parse(b instanceof SVGElement ? cs.fill : cs.backgroundColor); }
  else for (let e = el; e && !bg; e = e.parentElement) { const p = parse(getComputedStyle(e).backgroundColor); if (p && p.a > 0.9) bg = p; }
  if (!bg) bg = parse(getComputedStyle(document.body).backgroundColor);
  const [a, b] = [lum(fg.rgb), lum(bg.rgb)].sort((x, y) => y - x);
  return (a + 0.05) / (b + 0.05);
}"""


def theme(page):
    return page.evaluate("document.documentElement.dataset.theme")


def set_theme(page, value):
    page.click("#moreBtn")
    expect(page.locator("#moreMenu")).to_be_visible()
    page.select_option("#themeSel", value)
    page.keyboard.press("Escape")
    expect(page.locator("#moreMenu")).to_be_hidden()


def contrast(page, sel, bg=None, prop="color"):
    r = page.evaluate(CONTRAST_JS, [sel, bg, prop])
    assert r is not None, f"{sel} not found"
    return r


def open_sheet(page, eid):
    hold(page, marker(page, eid).locator("circle").first, 700)
    expect(page.locator("#sheet")).to_be_visible()


def open_history(page):
    box = page.locator("#sheetContent details.hist")
    if not box.evaluate("d => d.open"):
        box.locator("summary > span").first.click()
    expect(box.locator("svg.hist-chart path.line.s0")).to_have_count(1)
    return box


# ---------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("size", SIZES)
def test_auto_follows_the_system(stack, ha, open_page, size):
    page = open_page(stack, size, color_scheme="light")
    assert theme(page) == "light"
    assert page.evaluate("document.documentElement.dataset.themeMode") == "auto"
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == BG["light"]
    assert page.locator('meta[name="theme-color"]').get_attribute("content") == META["light"]
    expect(page.locator("#themeSel")).to_have_value("auto")
    # The system switches to dark while the page is open.
    page.emulate_media(color_scheme="dark")
    page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == BG["dark"]
    assert page.locator('meta[name="theme-color"]').get_attribute("content") == META["dark"]


@pytest.mark.parametrize("size", SIZES)
def test_manual_override_persists(stack, ha, open_page, size):
    page = open_page(stack, size, color_scheme="dark")
    assert theme(page) == "dark"
    set_theme(page, "light")
    assert theme(page) == "light"
    assert page.evaluate("localStorage.getItem('hc.theme')") == "light"
    page.reload()
    marker(page, "light.lounge").wait_for()
    assert theme(page) == "light"
    expect(page.locator("#themeSel")).to_have_value("light")
    # Light stays light when the system changes; Dark on a light system; Auto follows again.
    page.emulate_media(color_scheme="light")
    set_theme(page, "dark")
    assert theme(page) == "dark"
    page.reload()
    marker(page, "light.lounge").wait_for()
    assert theme(page) == "dark"
    set_theme(page, "auto")
    assert theme(page) == "light"
    page.reload()
    marker(page, "light.lounge").wait_for()
    assert theme(page) == "light" and page.evaluate("localStorage.getItem('hc.theme')") == "auto"


@pytest.mark.parametrize("size", SIZES)
def test_no_flash_theme_set_before_first_paint(stack, ha, open_page, size):
    page = open_page(stack, size, goto=None, color_scheme="dark")
    page.evaluate("localStorage.setItem('hc.theme', 'light')")
    # Recorded at DOMContentLoaded and before the first body element is even parsed.
    page.add_init_script("""
      window.__seen = {};
      new MutationObserver((_, o) => { if (document.body) { window.__seen.body = document.documentElement.dataset.theme; o.disconnect(); } })
        .observe(document, { childList: true, subtree: true });
      document.addEventListener('DOMContentLoaded', () => {
        window.__seen.dcl = document.documentElement.dataset.theme;
        window.__seen.bg = getComputedStyle(document.body).backgroundColor;
      });""")
    for path in ("/", "/login.html"):
        page.goto(stack.url + path)
        page.wait_for_function("window.__seen && window.__seen.dcl")
        seen = page.evaluate("window.__seen")
        assert seen == {"body": "light", "dcl": "light", "bg": BG["light"]}, (path, seen)


@pytest.mark.parametrize("size", SIZES)
def test_colours_differ_and_light_text_contrast(stack, ha, open_page, size):
    page = open_page(stack, size, color_scheme="light")
    page.locator("#list li[data-dev]").first.wait_for()
    probes = {
        "body": ("body", "backgroundColor"), "text": ("body", "color"), "muted": (".hint, #side h2", "color"),
        "button": ("#editToggle", "backgroundColor"), "room": (".room rect", "fill"), "wall": ("#walls .wall", "stroke"),
        "marker-text": (".marker text", "fill"), "off-marker": ('.marker[data-dev="light.kitchen"] circle', "fill"),
    }
    js = "p => Object.fromEntries(Object.entries(p).map(([k, [s, prop]]) => [k, getComputedStyle(document.querySelector(s))[prop]]))"
    ha.set("light.kitchen", "off")
    expect(marker(page, "light.kitchen")).to_be_visible()
    light = page.evaluate(js, probes)
    page.emulate_media(color_scheme="dark")
    page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
    dark = page.evaluate(js, probes)
    for k in probes:
        assert light[k] != dark[k], (k, light[k])
    page.emulate_media(color_scheme="light")
    page.wait_for_function("document.documentElement.dataset.theme === 'light'")
    # Body text and labels: AA (4.5:1) against what's behind them.
    checks = [("header h1" if size == "desktop" else "#status", None), ("#sideTitle", None), ("#list li .val", None),
              ("#list .name", None), (".room text", ".room rect")]
    for sel, bg in checks:
        assert contrast(page, sel, bg, "fill" if sel.startswith(".room") else "color") >= 4.5, sel
    # Sheet text: title, muted sub line, history readout and chart ticks on the panel.
    open_sheet(page, "switch.tv")
    open_history(page)
    for sel in ("#sheetContent h3", "#sheetContent .sub", ".hist-readout", ".hist summary"):
        assert contrast(page, sel) >= 4.5, sel
    assert contrast(page, ".hist-chart text.tick", ".sheet-body", "fill") >= 4.5
    page.click("#sheetClose")
    # Menu items and an amber warning-style status line.
    page.click("#moreBtn")
    for sel in ("#awayBtn", ".more-row"):
        assert contrast(page, sel) >= 4.5, sel
    page.keyboard.press("Escape")
    page.evaluate("document.getElementById('status').classList.add('offline')")
    assert contrast(page, "#status") >= 4.5


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_every_screen_renders(stack, ha, open_page, size, scheme):
    """Screenshots of the main screens in both themes (looked at by hand), plus a few colour checks."""
    page = open_page(stack, size, layout=FURNISHED, color_scheme=scheme)
    tag = f"{scheme}-{size}"
    assert theme(page) == scheme
    expect(page.locator("#furniture .fur").first).to_be_visible()
    fur = page.locator("#furniture .fu-b").first.evaluate("e => [getComputedStyle(e).fill, getComputedStyle(e).stroke]")
    assert fur == (["rgb(231, 227, 221)", "rgb(122, 115, 103)"] if scheme == "light" else ["rgb(44, 50, 61)", "rgb(102, 113, 138)"])
    shot(page, f"theme-plan-{tag}")
    # Device sheet with its history chart.
    open_sheet(page, "switch.tv")
    open_history(page)
    grid = page.locator(".hist-chart .grid").first.evaluate("e => getComputedStyle(e).stroke")
    assert grid == ("rgb(211, 206, 198)" if scheme == "light" else "rgb(52, 58, 69)")
    shot(page, f"theme-sheet-history-{tag}")
    page.click("#sheetClose")
    open_sheet(page, "light.lounge")
    shot(page, f"theme-sheet-light-{tag}")
    page.click("#sheetClose")
    open_sheet(page, "climate.lounge_valve")
    shot(page, f"theme-sheet-valve-{tag}")
    page.click("#sheetClose")
    # Menu, bell sheet, rooms, a dialog.
    page.click("#moreBtn")
    shot(page, f"theme-menu-{tag}")
    page.click("#roomsBtn")
    expect(page.locator("#roomsSheet")).to_be_visible()
    shot(page, f"theme-rooms-{tag}")
    page.keyboard.press("Escape")
    expect(page.locator("#roomsSheet")).to_be_hidden()
    page.click("#alertsBtn")
    expect(page.locator("#alertSheet")).to_be_visible()
    shot(page, f"theme-alerts-{tag}")
    page.click("#alertClose")
    page.click("#moreBtn"); page.click("#awayBtn")
    expect(page.locator("#modeDialog")).to_be_visible()
    shot(page, f"theme-dialog-{tag}")
    page.click("#modeDialog button[value=cancel]")
    # Edit mode, room view.
    page.click("#editToggle")
    expect(page.locator("#editbar")).to_be_visible()
    shot(page, f"theme-edit-{tag}")
    page.click("#cancelEdit")
    page.click("#moreBtn"); page.click("#roomsBtn")
    page.click('#roomsList [data-room="lounge"]')
    expect(page.locator("#roomBar")).to_be_visible()
    shot(page, f"theme-roomview-{tag}")
    # Wall mode by day follows the theme.
    page.goto(stack.url + "/?wall")
    expect(page.locator("#wallTime")).to_be_visible()
    assert theme(page) == scheme
    shot(page, f"theme-wall-{tag}")
    # Login page.
    page.context.clear_cookies()
    page.goto(stack.url + "/login.html")
    assert theme(page) == scheme
    page.fill("[name=username]", USER)
    page.fill("[name=password]", "wrong")
    page.click("button[type=submit]")
    expect(page.locator("#loginError")).to_have_text("Wrong username or password.")
    if scheme == "light":
        for sel in (".login label", ".login h1", "#loginError"):
            assert contrast(page, sel) >= 4.5, sel
    shot(page, f"theme-login-{tag}")


def test_wall_mode_dark_at_night(stack, ha, open_page):
    page = open_page(stack, "tablet", goto=None, clock="2026-10-09T22:59:20+01:00", color_scheme="light")
    page.goto(stack.url + "/?wall")
    expect(page.locator("#wallTime")).to_have_text("22:59")
    assert theme(page) == "light"
    page.clock.run_for(52_000)                     # 23:00:12: night, and dimmed after 30 s idle
    expect(page.locator("#wallDim")).to_be_visible()
    assert theme(page) == "dark"
    assert page.locator("#wallDim").evaluate("e => getComputedStyle(e).backgroundColor") == "rgb(0, 0, 0)"
    page.locator("#wallDim").click()
    expect(page.locator("#wallDim")).to_be_hidden()
    assert theme(page) == "dark"                   # woken at night: still dark
    shot(page, "theme-wall-night-tablet")
    # Leaving wall mode at night gives the chosen theme back.
    page.keyboard.press("Escape")
    expect(page.locator("body")).not_to_have_class(re.compile(r"\bwall\b"))
    assert theme(page) == "light"
    page.goto(stack.url + "/?wall")
    expect(page.locator("#wallTime")).to_be_visible()
    assert theme(page) == "dark"
    page.clock.set_system_time("2026-10-10T07:05:00+01:00")
    page.clock.run_for(2_000)
    page.wait_for_function("document.documentElement.dataset.theme === 'light'")


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_hoover_and_tv_follow_the_theme(appliance_stack, tv_stack, open_page, scheme):
    """Colours added with the hoover / desktop PC / home server drawings and the TV sheet are theme variables."""
    from test_appliances import GADGETS
    from test_tv import LINKED, open_tv, sheet
    appliance_stack.ha.reset()
    page = open_page(appliance_stack, "desktop", layout=GADGETS, color_scheme=scheme)
    hoover = page.locator('#appliances .appl[data-appl="hoover"]')
    expect(hoover).to_be_visible()
    # Force the charged look (the charge sequence itself is test_appliances.py's job).
    page.evaluate("""() => { const a = document.querySelector('#appliances .appl[data-appl="hoover"]');
      a.classList.add('on', 'charged'); document.querySelector('#applTags [data-tag="hoover"]').classList.add('on', 'charged'); }""")
    cs = page.evaluate("""() => ({
      bolt: getComputedStyle(document.querySelector('#appliances .appl[data-appl="hoover"] .fu-bolt')).fill,
      tag: getComputedStyle(document.querySelector('#applTags [data-tag="hoover"] .s') || document.querySelector('#applTags [data-tag="hoover"] text')).fill })""")
    green = {"light": ("rgb(46, 168, 98)", "rgb(22, 115, 62)"), "dark": ("rgb(95, 212, 122)", "rgb(155, 230, 173)")}[scheme]
    assert cs["bolt"] == green[0], cs
    if page.locator('#applTags [data-tag="hoover"] .s').count():
        assert cs["tag"] == green[1], cs
        if scheme == "light":
            assert contrast(page, '#applTags [data-tag="hoover"] .s', '#applTags [data-tag="hoover"] .bg', "fill") >= 4.5
    shot(page, f"theme-gadgets-{scheme}-desktop")
    # TV sheet: playing/state chip text readable, buttons' text on the accent.
    tv_stack.ha.reset()
    page = open_page(tv_stack, "desktop", layout=LINKED, color_scheme=scheme)
    open_tv(page)
    s = sheet(page)
    expect(s.locator(".tv-title")).to_have_text("The Crown")
    if scheme == "light":
        for sel in ("#sheet .tv-title", "#sheet .tv-subtitle", "#sheet .tv-state"):
            assert contrast(page, sel) >= 4.5, sel
    play = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--tv-play').trim()")
    assert play == ("#5b45d6" if scheme == "light" else "#8b7bff")
    shot(page, f"theme-tv-sheet-{scheme}-desktop")
    page.click("#sheetClose")
    shot(page, f"theme-tv-plan-{scheme}-desktop")
