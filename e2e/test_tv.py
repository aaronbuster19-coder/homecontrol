"""TV / media players end to end: a Samsung TV media_player (as the Samsung Smart TV integration shows it, with a
SmartThings duplicate on the same device that must not appear) and a speaker. Linked TV furniture with a lit screen and
label, tap opens the TV sheet (never a toggle), power, throttled volume, mute, transport, sources, sound mode, live
now-playing with proxied artwork, the "can't turn on" hint, All off leaving TVs alone by default, Link TV in edit mode,
placing a media player, wall mode and room view. Desktop mouse and a 390px touch phone.

Runs on its own stack (conftest.tv_stack, FAKE_HA_TV=1)."""
import copy

import pytest
from playwright.sync_api import expect

from conftest import LAYOUT, Touch, center, marker, plan_xy, shot

TV, SPEAKER, DUP = "media_player.samsung_tv", "media_player.kitchen_speaker", "media_player.samsung_tv_2"
TV_FEATURES = 1 | 4 | 8 | 16 | 32 | 128 | 256 | 1024 | 2048 | 16384 | 65536
PIECE = {"id": "tv1", "type": "tv", "x": 2.6, "y": 4.05, "w": 1.25, "h": 0.25, "rot": 180, "media": TV, "plug": "switch.tv"}
LINKED = {**copy.deepcopy(LAYOUT), "furniture": [PIECE],
          "placements": LAYOUT["placements"] + [{"entity_id": SPEAKER, "x": 7.4, "y": 1.9}]}


@pytest.fixture
def tha(tv_stack):
    tv_stack.ha.reset()
    return tv_stack.ha


@pytest.fixture(params=["desktop", "mobile"])
def size(request):
    return request.param


def tap(page, loc_or_xy):
    x, y = loc_or_xy if isinstance(loc_or_xy, tuple) else center(loc_or_xy)
    if page.size == "mobile":
        Touch(page).tap(x, y)
    else:
        page.mouse.click(x, y)


def press(page, loc):
    loc.tap() if page.size == "mobile" else loc.click()


def media_calls(ha, service=None):
    return [c for c in ha.calls() if c["domain"] == "media_player" and (service is None or c["service"] == service)]


def no_hscroll(page):
    assert page.evaluate("document.scrollingElement.scrollWidth <= innerWidth"), "horizontal scroll"


def tv_piece(page):
    return page.locator('#appliances .tvp[data-tv="tv1"]')


def open_tv(page):
    tap(page, tv_piece(page).locator(".tv-hit"))
    expect(page.locator("#sheet .tv-sheet")).to_be_visible()


def sheet(page):
    return page.locator("#sheet .tv-sheet")


def test_linked_tv_on_plan_and_sheet(tv_stack, tha, open_page, size):
    page = open_page(tv_stack, size, layout=LINKED)
    piece = tv_piece(page)
    expect(piece).to_be_visible()
    expect(piece).to_have_attribute("class", "fur tvp fu-tv on")
    expect(piece.locator(".tv-lit")).to_have_count(1)
    expect(page.locator('#applTags .tv-tag[data-tag="tv1"]')).to_have_text("Netflix · 86.4 W")
    expect(marker(page, TV)).to_have_count(0)                # the furniture is the control
    expect(marker(page, "switch.tv")).to_have_count(0)       # and its plug's marker is hidden as usual
    expect(marker(page, DUP)).to_have_count(0)
    assert page.evaluate(f"!st.devices.has('{DUP}') && !st.devices.has('switch.samsung_tv')")  # SmartThings duplicates
    open_tv(page)
    s = sheet(page)
    expect(page.locator("#sheetContent h3")).to_have_text("Samsung TV")
    expect(s.locator(".tv-title")).to_have_text("The Crown")
    expect(s.locator(".tv-subtitle")).to_have_text("Season 2 · Netflix · TV")
    expect(s.locator(".tv-state")).to_have_text("On")
    img = s.locator(".tv-art img")
    expect(img).to_be_visible()
    page.wait_for_function("document.querySelector('#sheet .tv-art img')?.naturalWidth > 0")
    src = img.get_attribute("src")
    assert src.startswith(f"/api/media/{TV}/artwork?v=") and "token" not in src
    expect(s.locator('[data-tv="power"]')).to_have_text("Turn off")
    expect(s.locator(".tv-src.cur")).to_have_text("TV")
    expect(s.locator('[data-tv="plug"]')).to_contain_text("86.4 W")
    expect(s.locator(".tv-alloff input")).not_to_be_checked()
    assert media_calls(tha) == [] and not [c for c in tha.calls() if c["service"] == "toggle"]  # a tap never switches
    no_hscroll(page)
    shot(page, f"tv-sheet-{size}")
    page.locator("#sheetClose").click()
    shot(page, f"tv-plan-{size}")


def test_power_off_and_on(tv_stack, tha, open_page, size):
    page = open_page(tv_stack, size, layout=LINKED)
    open_tv(page)
    press(page, sheet(page).locator('[data-tv="power"]'))
    tha.wait_call(lambda c: c["service"] == "turn_off" and c["data"] == {"entity_id": TV})
    expect(tv_piece(page)).to_have_attribute("class", "fur tvp fu-tv")  # screen dark
    expect(page.locator('#applTags .tv-tag[data-tag="tv1"]')).to_have_count(1)  # the plug still draws
    expect(sheet(page).locator(".tv-title")).to_have_text("Off")
    expect(sheet(page).locator(".tv-art img")).to_have_count(0)
    power = sheet(page).locator('[data-tv="power"]')
    expect(power).to_have_text("Turn on")
    press(page, power)
    tha.wait_call(lambda c: c["service"] == "turn_on" and c["data"] == {"entity_id": TV})
    expect(sheet(page).locator(".tv-title")).to_have_text("The Crown")  # back on: now playing again
    expect(tv_piece(page)).to_have_attribute("class", "fur tvp fu-tv on")


def test_volume_throttled_mute_and_steps(tv_stack, tha, open_page, size):
    page = open_page(tv_stack, size, layout=LINKED)
    open_tv(page)
    vol = sheet(page).locator('input[data-tv="volume"]')
    expect(vol).to_have_value("0.24")
    # 30 slider moves in quick succession: a few volume_set calls, the last one exactly where it ended
    page.evaluate("""() => { const r = document.querySelector('#sheet input[data-tv="volume"]');
        for (let i = 1; i <= 30; i++) { r.value = (0.24 + i * 0.01).toFixed(2); r.dispatchEvent(new Event('input', {bubbles: true})); }
        r.dispatchEvent(new Event('change', {bubbles: true})); }""")
    calls = tha.wait_calls(1, "volume_set")
    assert 1 <= len(calls) <= 3, calls
    assert calls[-1]["data"] == {"entity_id": TV, "volume_level": 0.54}
    expect(sheet(page).locator(".tv-slider .ctl-val")).to_have_text("54")
    n = len(calls)
    press(page, sheet(page).locator('[data-tv="vol-up"]'))
    tha.wait_call(lambda c: c["service"] == "volume_set" and c["data"]["volume_level"] == 0.59)
    assert len(tha.calls("volume_set")) == n + 1
    # mute
    mute = sheet(page).locator('[data-tv="mute"]')
    expect(mute).to_have_attribute("aria-pressed", "false")
    press(page, mute)
    tha.wait_call(lambda c: c["service"] == "volume_mute" and c["data"] == {"entity_id": TV, "is_volume_muted": True})
    expect(sheet(page).locator('[data-tv="mute"]')).to_have_attribute("aria-pressed", "true")
    expect(page.locator('#applTags .tv-tag[data-tag="tv1"]')).to_be_visible()
    if size == "mobile":
        no_hscroll(page)


def test_source_transport_and_sound_mode(tv_stack, tha, open_page, size):
    page = open_page(tv_stack, size, layout=LINKED)
    open_tv(page)
    press(page, sheet(page).locator('.tv-src[data-source="HDMI1"]'))
    tha.wait_call(lambda c: c["service"] == "select_source" and c["data"] == {"entity_id": TV, "source": "HDMI1"})
    expect(sheet(page).locator(".tv-src.cur")).to_have_text("HDMI1")
    expect(sheet(page).locator(".tv-title")).to_have_text("HDMI1")
    expect(page.locator('#applTags .tv-tag[data-tag="tv1"]')).to_have_text("HDMI1 · 86.4 W")
    press(page, sheet(page).locator('[data-tv="play_pause"]'))
    tha.wait_call(lambda c: c["service"] == "media_play_pause")
    expect(sheet(page).locator(".tv-state")).to_have_text("Playing")
    expect(tv_piece(page)).to_have_attribute("class", "fur tvp fu-tv on playing")
    press(page, sheet(page).locator('[data-tv="next"]'))
    press(page, sheet(page).locator('[data-tv="previous"]'))
    tha.wait_call(lambda c: c["service"] == "media_next_track")
    tha.wait_call(lambda c: c["service"] == "media_previous_track")
    sheet(page).locator('select[data-tv="sound_mode"]').select_option("Movie")
    tha.wait_call(lambda c: c["service"] == "select_sound_mode" and c["data"] == {"entity_id": TV, "sound_mode": "Movie"})
    assert not media_calls(tha, "turn_off") and not media_calls(tha, "turn_on")


def test_now_playing_live_updates(tv_stack, tha, open_page):
    page = open_page(tv_stack, "desktop", layout=LINKED)
    open_tv(page)
    first = sheet(page).locator(".tv-art img").get_attribute("src")
    tha.set(TV, "playing", app_name="Disney+", source="Disney+", media_title="Andor", media_series_title="Season 1",
            entity_picture=f"/api/media_player_proxy/{TV}?token=fakeproxytoken&cache=2")
    expect(sheet(page).locator(".tv-title")).to_have_text("Andor")
    expect(sheet(page).locator(".tv-subtitle")).to_have_text("Season 1 · Disney+")
    expect(sheet(page).locator(".tv-state")).to_have_text("Playing")
    expect(sheet(page).locator(".tv-art img")).not_to_have_attribute("src", first)  # cache-busted
    page.wait_for_function("document.querySelector('#sheet .tv-art img')?.naturalWidth > 0")
    expect(page.locator('#applTags .tv-tag[data-tag="tv1"]')).to_have_text("Disney+ · 86.4 W")
    tha.set(TV, "standby")
    expect(sheet(page).locator(".tv-title")).to_have_text("Standby")
    expect(page.locator(".tvp.on")).to_have_count(0)


def test_no_turn_on_support_shows_hint(tv_stack, tha, open_page, size):
    tha.set(TV, "off", supported_features=TV_FEATURES & ~128)
    page = open_page(tv_stack, size, layout=LINKED)
    open_tv(page)
    hint = sheet(page).locator('[data-tv="no-turn-on"]')
    expect(hint).to_be_visible()
    expect(hint).to_contain_text("Wake-on-LAN")
    expect(sheet(page).locator('[data-tv="power"]')).to_have_count(0)
    expect(sheet(page).locator('[data-tv="volume"]')).to_have_count(0)  # nothing to control while it's off
    assert media_calls(tha) == []
    if size == "mobile":
        no_hscroll(page)
        shot(page, "tv-sheet-off-hint-mobile")


def test_all_off_leaves_tv_unless_included(tv_stack, tha, open_page):
    page = open_page(tv_stack, "desktop", layout=LINKED)
    page.once("dialog", lambda d: d.accept())
    page.locator("#allOff").click()
    calls = tha.wait_calls(1, "turn_off")
    assert calls and not media_calls(tha), calls
    tha.reset()
    page.reload()
    open_tv(page)
    sheet(page).locator(".tv-alloff input").check()
    expect(page.locator("#status")).to_contain_text("also turns off Samsung TV")
    assert page.request.get(tv_stack.url + "/api/layout").json()["settings"]["all_off_include"] == [TV]
    page.locator("#sheetClose").click()
    page.once("dialog", lambda d: d.accept())
    page.locator("#allOff").click()
    tha.wait_call(lambda c: c["domain"] == "media_player" and c["service"] == "turn_off" and c["data"] == {"entity_id": [TV]})


def test_speaker_marker_opens_sheet_not_toggle(tv_stack, tha, open_page, size):
    page = open_page(tv_stack, size, layout=LINKED)
    m = marker(page, SPEAKER)
    expect(m.locator("use")).to_have_attribute("href", "#ic-media-speaker")
    tap(page, m.locator("circle"))
    expect(page.locator("#sheetContent h3")).to_have_text("Kitchen speaker")
    s = sheet(page)
    expect(s.locator('[data-tv="power"]')).to_have_count(0)        # a Sonos can't be switched off through HA
    expect(s.locator('[data-tv="play_pause"]')).to_be_visible()
    expect(s.locator(".tv-alloff")).to_have_count(0)
    assert media_calls(tha) == []
    # the list groups it under TV & media
    if size == "desktop":
        expect(page.locator("#list .group", has_text="TV & media")).to_be_visible()
        expect(page.locator(f'#list li[data-dev="{SPEAKER}"] .val')).to_have_text("on")


def test_link_tv_and_place_media_player_in_edit_mode(tv_stack, tha, open_page, size):
    start = {**copy.deepcopy(LAYOUT), "furniture": [{**PIECE, "media": None, "plug": None}]}  # null: unlinked
    page = open_page(tv_stack, size, layout=start)
    expect(page.locator(".tvp")).to_have_count(0)
    press(page, page.locator("#editToggle"))
    # the speaker is unplaced: tap it in the list, then tap the plan
    if size == "desktop":
        page.locator(f'#list li[data-dev="{SPEAKER}"]').click()
        page.mouse.click(*plan_xy(page, 7.4, 1.9))
        expect(marker(page, SPEAKER)).to_have_count(1)
    # select the TV piece and link it
    x, y = plan_xy(page, 2.6, 4.05)
    tap(page, (x, y))
    link = page.locator("#furMedia")
    expect(link).to_be_visible()
    expect(link).to_have_text("Link TV")
    press(page, link)
    opts = page.locator("#mediaList .plug-opt")
    expect(opts.nth(1)).to_contain_text("Samsung TV")      # TVs first, the speaker after
    expect(page.locator(f'#mediaList [data-media="{DUP}"]')).to_have_count(0)
    press(page, page.locator(f'#mediaList [data-media="{TV}"]'))
    expect(link).to_contain_text("TV: Samsung TV")
    expect(page.locator('#furniture [data-fur="tv1"] .tv-linkdot')).to_have_count(1)
    press(page, page.locator("#save"))
    expect(page.locator("#status")).to_contain_text("Saved")
    stored = page.request.get(tv_stack.url + "/api/layout").json()
    assert stored["furniture"][0]["media"] == TV
    if size == "desktop":
        sp = next(p for p in stored["placements"] if p["entity_id"] == SPEAKER)
        assert abs(sp["x"] - 7.4) < 0.06 and abs(sp["y"] - 1.9) < 0.06, sp
    expect(tv_piece(page)).to_be_visible()
    expect(tv_piece(page)).to_have_attribute("class", "fur tvp fu-tv on")
    no_hscroll(page)
    # a Save from an older app (no "media" key) keeps the link
    old = {**stored, "furniture": [{k: v for k, v in stored["furniture"][0].items() if k != "media"}]}
    assert page.request.put(tv_stack.url + "/api/layout", data=old).json()["furniture"][0]["media"] == TV


def test_wall_mode_chip_and_room_view(tv_stack, tha, open_page):
    page = open_page(tv_stack, "desktop", layout=LINKED, goto="/?wall")
    chip = page.locator("#wallTv")
    expect(chip).to_be_visible()
    expect(chip).to_have_text("TV · Netflix")
    tha.set(TV, "off")
    expect(chip).to_be_hidden()
    tha.set(TV, "on")
    expect(chip).to_be_visible()
    page2 = open_page(tv_stack, "desktop", layout=None, goto="/#room=lounge")
    expect(page2.locator('.room-facts .rf[data-fact="tv"]')).to_have_text("Samsung TV · Netflix")
    expect(page2.locator(f'#list li[data-dev="{TV}"]')).to_be_visible()  # listed in its room via the furniture
