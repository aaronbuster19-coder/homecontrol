"""Plan zoom on desktop: trackpad pinch / Ctrl + wheel zooms the plan around the pointer, not the page (zoom.js)."""
from playwright.sync_api import expect

from conftest import center, plan_xy
from test_roomview import WHOLE, viewbox, wait_viewbox


def under(page, x, y):
    """The plan point (metres) under screen pixel (x, y)."""
    return page.evaluate("""([x, y]) => { const p = new DOMPoint(x, y).matrixTransform(
        document.getElementById('plan').getScreenCTM().inverse()); return [p.x, p.y]; }""", [x, y])


def ctrl_wheel(page, x, y, dy):
    page.mouse.move(x, y)
    page.keyboard.down("Control")
    page.mouse.wheel(0, dy)
    page.keyboard.up("Control")


def test_pinch_zooms_the_plan_around_the_pointer_then_pans_and_resets(stack, ha, open_page):
    page = open_page(stack)
    wait_viewbox(page, WHOLE)
    # Did the page see the wheel as handled (so the browser doesn't zoom the page)?
    page.evaluate("""() => { window.__wheels = [];
        document.addEventListener('wheel', (e) => window.__wheels.push(e.defaultPrevented), { passive: true }); }""")
    expect(page.locator("#zoomReset")).to_be_hidden()

    x, y = (round(v) for v in plan_xy(page, 2, 2))  # the mouse moves in whole pixels
    p = under(page, x, y)
    ctrl_wheel(page, x, y, -300)
    page.wait_for_function("() => +document.getElementById('plan').getAttribute('viewBox').split(' ')[2] < 9")
    vb = viewbox(page)
    assert vb[2] < WHOLE[2] and abs(vb[2] / vb[3] - WHOLE[2] / WHOLE[3]) < 1e-6, vb
    # The plan point under the pointer stays under it.
    nx, ny = plan_xy(page, *p)
    assert abs(nx - x) < 0.5 and abs(ny - y) < 0.5, (x, y, nx, ny)
    assert page.evaluate("window.__wheels") == [True]
    assert page.evaluate("visualViewport.scale") == 1
    expect(page.locator("#zoomReset")).to_be_visible()

    # Zoomed: a plain scroll pans the plan (and doesn't scroll the page); never past the edge of the home.
    page.mouse.wheel(0, 120)
    page.wait_for_function("(y) => +document.getElementById('plan').getAttribute('viewBox').split(' ')[1] > y", arg=vb[1])
    for _ in range(20):
        page.mouse.wheel(0, 400)
    page.wait_for_timeout(100)
    after = viewbox(page)
    assert abs(after[1] + after[3] - (WHOLE[1] + WHOLE[3])) < 1e-6, after
    assert page.evaluate("scrollY") == 0

    page.click("#zoomReset")
    wait_viewbox(page, WHOLE)
    expect(page.locator("#zoomReset")).to_be_hidden()

    # Zooming out never goes past the whole home; without Ctrl and unzoomed, the wheel is left to the page.
    ctrl_wheel(page, x, y, 500)
    page.mouse.wheel(0, 100)
    page.wait_for_timeout(100)
    wait_viewbox(page, WHOLE)
    assert page.evaluate("window.__wheels")[-1] is False


def test_room_view_starts_unzoomed(stack, ha, open_page):
    page = open_page(stack)
    x, y = center(page.locator('[data-roomopen="lounge"]'))  # zoom around the button, so it stays in view
    ctrl_wheel(page, x, y, -100)
    expect(page.locator("#zoomReset")).to_be_visible()
    page.click('[data-roomopen="lounge"]')
    wait_viewbox(page, [-0.4, -0.4, 6.0, 5.0])  # the lounge (0..5.2 x 0..4.2) with its 0.4 m pad
    expect(page.locator("#zoomReset")).to_be_hidden()
