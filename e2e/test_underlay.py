"""Floor-plan photo underlay: upload in edit mode, line it up (drag, scroll, pinch, the panel), opacity, saved apart
from the layout, hidden outside edit mode unless asked, bad files refused. Mouse on a desktop, CDP touch on a 390px
phone, both themes."""
import struct
import zlib

import pytest
from playwright.sync_api import expect

from conftest import WAIT, hold, plan_xy, shot


def plan_png(w=480, h=360) -> bytes:
    """A black-on-white "scan": an outer wall and a few inner ones, like a floor plan from an estate agent."""
    px = [[255] * w for _ in range(h)]

    def box(x0, y0, x1, y1, t=4):
        for y in range(y0, y1):
            for x in range(x0, x1):
                if x < x0 + t or x >= x1 - t or y < y0 + t or y >= y1 - t:
                    px[y][x] = 30
    box(10, 10, w - 10, h - 10, 6)
    box(10, 10, 260, 190)
    box(256, 10, w - 10, 130)
    box(10, 186, 220, h - 10)
    raw = b"".join(b"\x00" + bytes(v for p in row for v in (p, p, p)) for row in px)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def server(page, stack):
    r = page.request.get(stack.url + "/api/underlay")
    assert r.ok
    return r.json()


def clean(page, stack):
    assert page.request.delete(stack.url + "/api/underlay").ok


def open_panel(page):
    page.click("#editToggle")
    expect(page.locator("#editbar")).to_be_visible()
    page.click("#ulBtn")
    expect(page.locator("#ulPanel")).to_be_visible()


def upload(page, tmp_path, data=None, name="plan.png", mime="image/png"):
    f = tmp_path / name
    f.write_bytes(plan_png() if data is None else data)
    page.set_input_files("#ulFile", files=[{"name": name, "mimeType": mime, "buffer": f.read_bytes()}])


def wait_saved(page, stack, pred, timeout=WAIT * 1000):  # a slow save on a busy runner
    page.wait_for_function("() => !ul.timer && !Object.keys(ul.pending).length", timeout=timeout)
    end = page.evaluate("Date.now()") + timeout
    while True:
        s = server(page, stack)
        if pred(s):
            return s
        assert page.evaluate("Date.now()") < end, f"server never matched: {s}"
        page.wait_for_timeout(50)


def image_box(page):
    loc = page.locator("#underlay image")
    expect(loc).to_be_visible()
    return loc.bounding_box()


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_desktop_upload_line_up_and_hide(open_page, stack, tmp_path, scheme):
    page = open_page(stack, "desktop", color_scheme=scheme)
    clean(page, stack)
    page.reload(); page.locator("#plan .marker").first.wait_for()
    open_panel(page)
    expect(page.locator("#ulEmpty")).to_be_visible()
    shot(page, f"underlay-empty-desktop-{scheme}")

    upload(page, tmp_path)
    expect(page.locator("#ulCtl")).to_be_visible()
    expect(page.locator("#ulAdjust")).to_have_attribute("aria-pressed", "true")  # a first photo: ready to line up
    expect(page.locator("#underlay .ul-outline")).to_be_visible()
    # Fitted over the rooms (0–8.4 × 0–7.6 m; the photo is 4:3, so as wide as the plan).
    s = wait_saved(page, stack, lambda s: s.get("width") == 8.4)
    assert (s["x"], s["y"], s["rot"], s["opacity"], s["show_view"]) == (4.2, 3.8, 0, 0.5, False)
    assert s["image"]["w"] == 480 and s["image"]["h"] == 360
    shot(page, f"underlay-adjust-desktop-{scheme}")

    # Drag moves the photo, not the room under the pointer.
    lounge = page.evaluate("st.draft.rooms.find((r) => r.id === 'lounge').x")
    x0, y0 = plan_xy(page, 2.0, 2.0)
    x1, y1 = plan_xy(page, 3.0, 2.5)
    page.mouse.move(x0, y0); page.mouse.down(); page.mouse.move((x0 + x1) / 2, (y0 + y1) / 2, steps=4)
    page.mouse.move(x1, y1, steps=4); page.mouse.up()
    s = wait_saved(page, stack, lambda s: abs(s["x"] - 5.2) < 0.02 and abs(s["y"] - 4.3) < 0.02)
    assert page.evaluate("st.draft.rooms.find((r) => r.id === 'lounge').x") == lounge
    assert page.evaluate("st.sel") is None

    # Scroll scales around the pointer.
    cx, cy = plan_xy(page, s["x"], s["y"])
    page.mouse.move(cx, cy); page.mouse.wheel(0, -200)
    s = wait_saved(page, stack, lambda s: s["width"] > 8.5)
    assert abs(s["x"] - 5.2) < 0.05 and abs(s["y"] - 4.3) < 0.05

    # The panel: rotation, width, opacity.
    page.fill("#ulRotN", "-2.5"); page.press("#ulRotN", "Enter")
    page.fill("#ulWidth", "9"); page.press("#ulWidth", "Enter")
    page.locator("#ulOpacity").fill("80")
    s = wait_saved(page, stack, lambda s: s["rot"] == -2.5 and s["width"] == 9 and s["opacity"] == 0.8)
    expect(page.locator("#ulOpacityV")).to_have_text("80 %")
    assert "rotate(-2.5)" in page.get_attribute("#underlay", "transform")
    assert float(page.locator("#underlay image").evaluate("e => getComputedStyle(e).opacity")) == pytest.approx(0.8)
    # Rooms go see-through over the photo.
    assert float(page.locator("#plan .room rect").first.evaluate("e => getComputedStyle(e).fillOpacity")) < 0.5

    # Done moving: the plan edits rooms again (a drag moves the room).
    page.click("#ulAdjust")
    expect(page.locator("#ulAdjust")).to_have_attribute("aria-pressed", "false")
    page.click("#ulFit")
    wait_saved(page, stack, lambda s: s["width"] == 8.4 and s["rot"] == 0)
    page.fill("#ulRotN", "-2.5"); page.press("#ulRotN", "Enter")
    shot(page, f"underlay-panel-desktop-{scheme}")

    # Save the layout: the photo isn't part of it, and outside edit mode it's hidden by default.
    page.click("#save")
    expect(page.locator("#editbar")).to_be_hidden()
    expect(page.locator("#ulPanel")).to_be_hidden()
    expect(page.locator("#underlay")).to_be_hidden()
    layout = page.request.get(stack.url + "/api/layout").json()
    assert "underlay" not in layout
    shot(page, f"underlay-view-hidden-desktop-{scheme}")

    # Also show outside edit mode (everyone): visible after a reload.
    open_panel(page)
    page.check("#ulShowView")
    wait_saved(page, stack, lambda s: s["show_view"] is True)
    page.click("#cancelEdit")
    expect(page.locator("#underlay image")).to_be_visible()
    page.reload(); page.locator("#plan .marker").first.wait_for()
    expect(page.locator("#underlay image")).to_be_visible()
    # Markers still take taps on top of it.
    expect(page.locator("#plan .marker").first).to_be_visible()
    shot(page, f"underlay-view-shown-desktop-{scheme}")
    # Never on the wall tablet.
    page.goto(stack.url + "/?wall"); page.locator("#plan .marker").first.wait_for()
    expect(page.locator("body.wall")).to_have_count(1)
    expect(page.locator("#underlay")).to_be_hidden()
    hold(page, page.locator("#wallClock"))  # the clock: hold for the exit menu
    page.click("#wallExit")
    expect(page.locator("#underlay image")).to_be_visible()

    # Show while editing off (this device only).
    open_panel(page)
    page.uncheck("#ulShowEdit")
    expect(page.locator("#underlay")).to_be_hidden()
    page.check("#ulShowEdit")
    expect(page.locator("#underlay image")).to_be_visible()

    # Remove.
    page.once("dialog", lambda d: d.accept())
    page.click("#ulRemove")
    expect(page.locator("#ulEmpty")).to_be_visible()
    expect(page.locator("#underlay")).to_be_hidden()
    assert server(page, stack) == {"image": None}


def test_bad_files_refused(open_page, stack, tmp_path):
    page = open_page(stack, "desktop")
    clean(page, stack)
    open_panel(page)
    upload(page, tmp_path, b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", "evil.png", "image/png")
    expect(page.locator("#status")).to_contain_text("Photo:")
    assert server(page, stack) == {"image": None}
    expect(page.locator("#ulEmpty")).to_be_visible()
    # Too big for the server (the app shrinks real photos first, so this only happens to a crafted request).
    r = page.request.post(stack.url + "/api/underlay/image", data=b"\x89PNG" + b"\x00" * (10 * 1024 * 1024 + 1),
                          headers={"Content-Type": "image/png"})
    assert r.status == 413


def test_big_photo_is_shrunk(open_page, stack, tmp_path):
    """A camera-sized photo is re-encoded in the browser to at most 4000 px (and EXIF rotation baked in)."""
    page = open_page(stack, "desktop")
    clean(page, stack)
    open_panel(page)
    upload(page, tmp_path, plan_png(4400, 300), "big.png")
    s = wait_saved(page, stack, lambda s: bool(s.get("image")))
    assert s["image"]["type"] == "jpeg" and s["image"]["w"] == 4000 and s["image"]["h"] == 273


def pinch(page, c, d0, d1, steps=8):
    """Two fingers on the plan around c, from d0 to d1 px apart (horizontal), through CDP touch events."""
    cdp = page.context.new_cdp_session(page)
    pts = lambda d: [{"x": c[0] - d / 2, "y": c[1], "id": 1}, {"x": c[0] + d / 2, "y": c[1], "id": 2}]
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": pts(d0)})
    for i in range(1, steps + 1):
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": pts(d0 + (d1 - d0) * i / steps)})
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


def drag_touch(page, a, b, steps=8):
    cdp = page.context.new_cdp_session(page)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": a[0], "y": a[1], "id": 1}]})
    for i in range(1, steps + 1):
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [
            {"x": a[0] + (b[0] - a[0]) * i / steps, "y": a[1] + (b[1] - a[1]) * i / steps, "id": 1}]})
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_phone_touch(open_page, stack, tmp_path, scheme):
    page = open_page(stack, "mobile", color_scheme=scheme)
    clean(page, stack)
    page.reload(); page.locator("#plan .marker").first.wait_for()
    page.tap("#editToggle")
    expect(page.locator("#editbar")).to_be_visible()
    btn = page.locator("#ulBtn")
    expect(btn).to_be_visible()
    b = btn.bounding_box()
    assert b["x"] >= 0 and b["x"] + b["width"] <= 390  # fits the 390px edit bar
    btn.tap()
    expect(page.locator("#ulPanel")).to_be_visible()
    p = page.locator("#ulPanel").bounding_box()
    assert p["x"] >= 0 and p["x"] + p["width"] <= 390 and p["y"] + p["height"] <= 844
    upload(page, tmp_path)
    expect(page.locator("#ulAdjust")).to_have_attribute("aria-pressed", "true")
    s = wait_saved(page, stack, lambda s: s.get("width") == 8.4)
    shot(page, f"underlay-phone-adjust-{scheme}")

    # Pinch out: bigger, centred where the fingers are.
    c = plan_xy(page, s["x"], s["y"])
    w0 = image_box(page)["width"]
    pinch(page, c, 60, 120)
    s = wait_saved(page, stack, lambda s: s["width"] > 12)
    assert s["width"] == pytest.approx(16.8, rel=0.08)
    assert image_box(page)["width"] > w0 * 1.6
    # One finger drags it.
    a = plan_xy(page, s["x"], s["y"])
    drag_touch(page, a, (a[0] - 30, a[1] + 20))
    s2 = wait_saved(page, stack, lambda t: t["x"] < s["x"] - 0.1)
    assert s2["y"] > s["y"]
    # While moving, the phone panel is just Done / Fit so the plan stays in view.
    assert page.locator("#ulPanel").bounding_box()["height"] < 200
    expect(page.locator("#ulOpacity")).to_be_hidden()
    page.tap("#ulAdjust")
    expect(page.locator("#ulAdjust")).to_have_attribute("aria-pressed", "false")
    # The opacity slider works by touch too (pointer events, not the plan).
    page.locator("#ulOpacity").fill("35")
    wait_saved(page, stack, lambda s: s["opacity"] == 0.35)
    page.tap("#ulFit")
    wait_saved(page, stack, lambda s: s["width"] == 8.4)
    shot(page, f"underlay-phone-panel-{scheme}")
    page.tap("#ulClose")
    expect(page.locator("#ulPanel")).to_be_hidden()
    shot(page, f"underlay-phone-trace-{scheme}")
    page.tap("#cancelEdit")
    expect(page.locator("#underlay")).to_be_hidden()
    clean(page, stack)
