"""Helpers from the original wall-mode tests, kept for test_dehumidifier.py and test_energy.py
(the shared fixtures now live in conftest.py)."""
import json
import re
import socket
import time
import urllib.request

from conftest import PASSWORD, ROOT, USER, hold, marker, marker_center, shot  # noqa: F401  (re-exported)

SIZES = {"tablet": (1280, 800), "portrait": (768, 1024), "phone": (390, 844)}

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


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except urllib.error.HTTPError:
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"{url} did not come up")


class HA:
    def __init__(self, base):
        self.base = base

    def calls(self):
        return json.load(urllib.request.urlopen(f"{self.base}/_calls"))

    def reset(self):
        urllib.request.urlopen(urllib.request.Request(f"{self.base}/_reset", method="POST"))

    def wait_call(self, pred, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            hit = [c for c in self.calls() if pred(c)]
            if hit:
                return hit
            time.sleep(0.1)
        raise AssertionError(f"no matching call in {self.calls()}")


def new_page(browser, servers, size="tablet", clock=None, **kw):
    w, h = SIZES[size]
    ctx = browser.new_context(viewport={"width": w, "height": h}, timezone_id="Europe/London", locale="en-GB",
                              has_touch=size != "tablet", **kw)
    page = ctx.new_page()
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.errors = []
    if clock:
        page.clock.install(time=clock)
    page.goto(servers["app"] + "/login.html")
    page.fill("[name=username]", USER)
    page.fill("[name=password]", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(re.compile(r"/(\?.*)?$"))
    r = page.request.put(servers["app"] + "/api/layout", data=json.dumps({"furniture": [], **LAYOUT}), headers={"Content-Type": "application/json"})
    assert r.ok, r.text()
    return page
