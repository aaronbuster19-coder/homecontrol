"""Shared fixtures for the browser tests: fake HA + the real app on free ports, logged-in pages, failure artifacts.

    pip install -r requirements-e2e.txt && python -m playwright install chromium   # once
    python -m pytest e2e                                                            # everything

Each test module gets its own fake HA, app and temp database (`stack`); tests that change server state beyond the
layout (Away mode, rate limits) use `fresh_stack`. The fake HA's states and call log are reset before every test.
On failure a screenshot of every open page (and with E2E_TRACE=1 a Playwright trace) is saved to $E2E_ARTIFACTS (default
e2e/artifacts). Set SHOTS=dir to also keep the screenshots the tests take on purpose (default e2e/screenshots).
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOTS = Path(os.environ.get("SHOTS", ROOT / "e2e" / "screenshots"))
ARTIFACTS = Path(os.environ.get("E2E_ARTIFACTS", ROOT / "e2e" / "artifacts"))
TRACE = os.environ.get("E2E_TRACE") == "1"  # record a Playwright trace per context, kept for failed tests
HOST = "127.0.0.1"
USER, PASSWORD = "me", "pw-for-tests"

# name -> browser context options
SIZES = {
    "desktop": {"viewport": {"width": 1280, "height": 800}},
    "tablet": {"viewport": {"width": 1280, "height": 800}},
    "portrait": {"viewport": {"width": 768, "height": 1024}, "has_touch": True},
    "phone": {"viewport": {"width": 390, "height": 844}, "has_touch": True},
    # a real phone: touch, mobile viewport handling, 2x pixels
    "mobile": {"viewport": {"width": 390, "height": 844}, "has_touch": True, "is_mobile": True, "device_scale_factor": 2},
}

LAYOUT = {
    "unit": "m",
    "rooms": [
        {"id": "lounge", "name": "Lounge", "x": 0, "y": 0, "w": 5.2, "h": 4.2},
        {"id": "kitchen", "name": "Kitchen", "x": 5.2, "y": 0, "w": 3.2, "h": 2.6},
        {"id": "hall", "name": "Hall", "x": 5.2, "y": 2.6, "w": 3.2, "h": 1.6},
        {"id": "bed", "name": "Bedroom", "x": 0, "y": 4.2, "w": 4.4, "h": 3.4},
        {"id": "bath", "name": "Bathroom", "x": 4.4, "y": 4.2, "w": 2.4, "h": 2.2},
    ],
    "placements": [
        {"entity_id": "light.lounge", "x": 2.4, "y": 2.0},
        {"entity_id": "light.strip", "x": 1.0, "y": 3.5},
        {"entity_id": "switch.tv", "x": 4.4, "y": 3.4},
        {"entity_id": "climate.lounge_valve", "x": 0.5, "y": 0.9},
        {"entity_id": "light.kitchen", "x": 6.8, "y": 1.3},
        {"entity_id": "switch.kettle", "x": 7.8, "y": 0.6},
        {"entity_id": "light.bedroom", "x": 2.2, "y": 5.9},
        {"entity_id": "climate.bedroom_valve", "x": 0.5, "y": 7.0},
        {"entity_id": "binary_sensor.contact_sensor_door", "x": 7.9, "y": 3.6},
    ],
    "openings": [
        {"id": "o1", "type": "door", "x": 8.4, "y": 3.0, "len": 0.9, "orient": "v",
         "entity_id": "binary_sensor.contact_sensor_door"},
        {"id": "o2", "type": "window", "x": 1.4, "y": 0, "len": 1.6, "orient": "h"},
    ],
}


# ---------- servers ----------
def free_port():
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def wait_http(url, proc, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            raise RuntimeError(f"{proc.args} exited with {proc.returncode}")
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except urllib.error.HTTPError:
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"{url} did not come up")


def stop(*procs):
    for proc in procs:  # open SSE streams keep uvicorn from a graceful exit
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


class FakeHA:
    def __init__(self, base):
        self.base = base

    def _req(self, path, body=None):
        req = urllib.request.Request(self.base + path, method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=5))

    def calls(self, service=None):
        return [c for c in self._req("/fake/calls") if service is None or c["service"] == service]

    def reset(self):
        self._req("/fake/reset", {})

    def set(self, entity_id, state=None, **attributes):
        body = {"entity_id": entity_id, "attributes": attributes}
        if state is not None:
            body["state"] = state
        return self._req("/fake/set", body)

    def wait_call(self, pred, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            hit = [c for c in self.calls() if pred(c)]
            if hit:
                return hit
            time.sleep(0.05)
        raise AssertionError(f"no matching call in {self.calls()}")

    def wait_calls(self, n, service=None, timeout=5):
        """Wait until at least n calls (of that service) arrived, then a little longer to catch extras."""
        end = time.time() + timeout
        while time.time() < end and len(self.calls(service)) < n:
            time.sleep(0.05)
        time.sleep(0.4)
        return self.calls(service)


class Stack:
    """A fake HA and the app (temp DB) on free ports of 127.0.0.1."""

    def __init__(self, tmp: Path):
        ha_port, app_port = free_port(), free_port()
        self.ha_proc = subprocess.Popen([sys.executable, str(ROOT / "e2e" / "fake_ha.py"), str(ha_port), HOST], cwd=ROOT)
        wait_http(f"http://{HOST}:{ha_port}/fake/calls", self.ha_proc)
        env = {**os.environ, "HA_URL": f"http://{HOST}:{ha_port}", "HA_TOKEN": "test-token", "APP_USER": USER,
               "APP_PASSWORD": PASSWORD, "DB_PATH": str(tmp / "layout.db"), "TZ_NAME": "Europe/London"}
        self.app_proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "backend.app:create_app", "--factory", "--host", HOST,
             "--port", str(app_port), "--log-level", "warning"], cwd=ROOT, env=env)
        try:
            wait_http(f"http://{HOST}:{app_port}/healthz", self.app_proc)
        except Exception:
            stop(self.ha_proc)
            raise
        self.ha = FakeHA(f"http://{HOST}:{ha_port}")
        # "localhost" is a secure context, so the service worker and push APIs are available.
        self.url = f"http://localhost:{app_port}"

    def close(self):
        stop(self.app_proc, self.ha_proc)


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    s = Stack(tmp_path_factory.mktemp("stack"))
    yield s
    s.close()


@pytest.fixture
def fresh_stack(tmp_path_factory):
    s = Stack(tmp_path_factory.mktemp("fresh"))
    yield s
    s.close()


@pytest.fixture(scope="module")
def servers(stack):
    """The old e2e/test_wall.py shape: {"ha": fake HA base URL, "app": app base URL}."""
    return {"ha": stack.ha.base, "app": stack.url}


@pytest.fixture
def ha(stack):
    stack.ha.reset()
    return stack.ha


# ---------- browser ----------
@pytest.fixture(scope="module")
def browser():
    # Module scope: a test module with its own sync_playwright() fixtures never overlaps this one.
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def login(page, base, user=USER, password=PASSWORD):
    page.goto(base + "/login.html")
    page.fill("[name=username]", user)
    page.fill("[name=password]", password)
    page.click("button[type=submit]")
    page.wait_for_url(re.compile(r"/(\?.*)?$"))


def put_layout(page, base, layout):
    r = page.request.put(base + "/api/layout", data=json.dumps(layout), headers={"Content-Type": "application/json"})
    assert r.ok, r.text()
    return r.json()


@pytest.fixture
def open_page(browser, request):
    """open_page(stack, size="desktop", layout=LAYOUT, goto="/", clock=None, login=True, **context_options) -> page.
    Every page fails the test on uncaught page errors (page.errors) unless page.allow_errors is set."""
    pages, contexts = [], []

    def make(stack, size="desktop", layout=LAYOUT, goto="/", clock=None, signed_in=True, **kw):
        ctx = browser.new_context(**{**SIZES[size], "timezone_id": "Europe/London", "locale": "en-GB", **kw})
        if TRACE:
            ctx.tracing.start(screenshots=True, snapshots=True)
        contexts.append(ctx)
        page = ctx.new_page()
        page.errors, page.allow_errors, page.size = [], False, size
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        pages.append(page)
        if clock:
            page.clock.install(time=clock)
        if signed_in:
            login(page, stack.url)
            if layout is not None:
                put_layout(page, stack.url, layout)
            if goto:
                page.goto(stack.url + goto)
                if goto == "/" and layout and layout.get("placements"):
                    page.locator("#plan .marker").first.wait_for()
        return page

    yield make
    failed = getattr(request.node, "rep_call", None) and request.node.rep_call.failed
    if failed:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        for i, page in enumerate(pages):
            try:
                page.screenshot(path=str(ARTIFACTS / f"{request.node.name}-{i}.png"))
            except Exception:
                pass
    for i, ctx in enumerate(contexts):
        try:
            if TRACE:
                ctx.tracing.stop(path=str(ARTIFACTS / f"{request.node.name}-{i}-trace.zip") if failed else None)
        except Exception:
            pass
        ctx.close()
    errors = [e for p in pages if not p.allow_errors for e in p.errors]
    if not failed:
        assert not errors, f"page errors: {errors}"


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    rep = yield
    setattr(item, "rep_" + rep.when, rep)
    return rep


# ---------- helpers ----------
def shot(page, name):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(250)
    page.screenshot(path=str(SHOTS / f"{name}.png"))


def marker(page, eid):
    return page.locator(f'#plan .marker[data-dev="{eid}"]')


def center(locator):
    b = locator.bounding_box()
    return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2


def marker_center(page, eid):
    return center(marker(page, eid).locator("circle").first)


def plan_xy(page, x, y):
    """Plan coordinates (metres) -> screen pixels."""
    return page.evaluate("([x, y]) => { const m = document.getElementById('plan').getScreenCTM(); return [m.a * x + m.e, m.d * y + m.f]; }", [x, y])


def hold(page, locator, ms=1200):
    x, y = center(locator)
    page.mouse.move(x, y)
    page.mouse.down()
    page.wait_for_timeout(ms)
    page.mouse.up()


class Touch:
    """Real touch input through CDP (Input.dispatchTouchEvent), for gestures Playwright's tap() can't do."""

    def __init__(self, page):
        self.page, self.cdp = page, page.context.new_cdp_session(page)

    def _send(self, type_, pts):
        self.cdp.send("Input.dispatchTouchEvent", {"type": type_, "touchPoints": [{"x": x, "y": y} for x, y in pts]})

    def tap(self, x, y):
        self._send("touchStart", [(x, y)])
        self._send("touchEnd", [])
        self.page.wait_for_timeout(50)

    def hold(self, x, y, ms=700):
        self._send("touchStart", [(x, y)])
        self.page.wait_for_timeout(ms)
        self._send("touchEnd", [])
        self.page.wait_for_timeout(50)

    def drag(self, x0, y0, x1, y1=None, steps=10, during=None):
        """(x1, y1), or x1 a function returning them, evaluated after the touch went down."""
        self._send("touchStart", [(x0, y0)])
        if callable(x1):
            x1, y1 = x1()
        for i in range(1, steps + 1):
            self._send("touchMove", [(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps)])
        r = during() if during else None
        self._send("touchEnd", [])
        self.page.wait_for_timeout(50)
        return r


def toggles(ha, eid):
    return [c for c in ha.calls() if c["service"] in ("toggle", "turn_on", "turn_off") and eid in json.dumps(c["data"])]
