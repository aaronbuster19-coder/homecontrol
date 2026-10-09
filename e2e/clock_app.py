"""Test-only app factory for browser tests: the real app, with a server clock the test can move forward.

    python -m uvicorn --app-dir e2e clock_app:create --factory ...
POST /_test/clock {"t": epoch seconds} sets the clock (it keeps ticking from there) and wakes the automations loop.
Production never imports this file; backend/app.py has no clock backdoor, only a `clock` argument for tests.
"""
import time

from fastapi.responses import JSONResponse
from starlette.routing import Route

import backend.automations as automations
import backend.climate as climate
from backend.app import create_app


class Clock:
    def __init__(self):
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset


def create():
    automations.STARTUP_DELAY = 0.5
    climate.STARTUP_DELAY = 0.5
    clock = Clock()
    app = create_app(clock=clock)

    async def set_clock(request):
        clock.offset = float((await request.json())["t"]) - time.time()
        app.state.automations.wake.set()
        app.state.climate.wake.set()  # smart preheat / damp (backend/climate.py) run their own loop
        return JSONResponse({"now": clock()})

    app.router.routes.insert(0, Route("/_test/clock", set_clock, methods=["POST"]))  # before the static "/" mount
    return app
