# homecontrol v10 — orchestrator brief

You are the **orchestrator** for v10 of homecontrol. You do not write the features yourself. Split the work across **worker sessions** (create them with `mcp__claude-code-remote__create_session`, one per work package below, all in parallel; use in-process subagents only for small read-only lookups). Your job:

1. Hand out the tasks.
2. Monitor the workers.
3. Review and merge their branches.
4. Fix integration races.
5. Get CI green.
6. Ask the user before anything reaches `main`.

## Project context
- Repo: `aaronbuster19-coder/homecontrol`. FastAPI backend (`backend/`), vanilla JS frontend (`frontend/`, no build step, no CDN libs), SVG floor plan of the user's flat with Home Assistant (HA) devices. User is in the UK (Europe/London).
- **Pushing to `main` deploys to the LIVE app** (control.aaronsmith.live, on the user's real HA).
  - `.github/workflows/test.yml` runs `test` (`docker build --target test .` = pytest) and `e2e` (`e2e/docker.sh`, Playwright image `mcr.microsoft.com/playwright/python:v1.56.0-noble`).
  - When both pass, `publish` pushes the image to `ghcr.io/aaronbuster19-coder/homecontrol:latest`.
  - Watchtower on dockerbox (`dockge/compose.yaml`) pulls it within about 5 minutes.
  - There is NO runner on dockerbox. CI runs on three Hyper-V VMs (`[self-hosted, homecontrol-ci]`, built by `ci-runner/`), which cannot reach the user's LAN.
- The Hyper-V runners are slow and contended. Timing-sensitive e2e tests have flaked there (brightness throttle, import dialog, theme socket timeout).
  - Tests must wait on real conditions; never use fixed sleeps or fixed coordinates.
  - "Flake" is not a root cause.
- Read README.md first. Key files: `backend/{app,auth,store,live,alerts,ha,discovery,modes,summary,history,appliances,media,automations,disco}.py` (and the v9 brief/climate/tiles/users/underlay modules), `backend/tests/`, `e2e/` (incl. `fake_ha.py`, `conftest.py`), `frontend/*.js`, `frontend/sw.js`, `frontend/style.css`, `dockge/compose.yaml`, `ci-runner/`.
- Conventions:
  - Layout PUTs that omit stored keys keep them (`store.carry_settings`); keep layout JSON backward compatible.
  - A new frontend file goes in the `sw.js` SHELL, and VERSION is bumped (the orchestrator resolves clashes).
  - The header is full at 390px; new entry points go in the ⋯ More menu or existing sheets.
  - Colours must use the theme CSS variables (light and dark).
  - Every route enforces the v9 roles (admin / member / guest) server-side.
- Safety principles:
  - No unrequested automatic switching. Every automation is opt-in, off by default, debounced, never loops service calls, and has an off switch.
  - The fridge, keep-on plugs and the home server are never switched off.
  - Unit-test timing logic with an injected clock.
  - Never contact the real HA or real devices from tests.

## Work packages (one worker each, branch names exactly as given)
- **v10/ops — Steadier CI + automatic rollback** (merge FIRST)
  - **CI:** make the e2e suite reliable on the contended Hyper-V runners. Find the timing-sensitive tests (measure where the browser sends, not where the fake HA receives; wait for all calls to land before a test ends; give slow saves a generous wait). Consider pytest-level isolation between tests. Add a CI step that runs the suite under CPU pressure (e.g. `--cpus=1`) and fix what fails.
    - If `ci-runner/` capacity settings (CPUs or RAM per VM, runner count) are the real cause, document a recommendation in `ci-runner/README.md`, but do not apply infrastructure changes.
  - **Rollback:** Watchtower doesn't roll back. Add automatic rollback on dockerbox without a GitHub runner there. For example, a small guard container in `dockge/compose.yaml` that:
    - watches the app container's health after Watchtower updates it;
    - if the container stays unhealthy for about 2.5 minutes, re-runs the previous image digest and pauses updates until a newer image is published;
    - is fully tested with fakes (no real Docker socket in tests).
    Document how to see and undo a rollback in the README.
  - Editing `.github/workflows/test.yml` and `dockge/compose.yaml` is expected here.
- **v10/scenes — Scenes + sleep timer**
  - **Scenes:** user-defined one-tap presets (lights on/off, brightness/colour/kelvin, plugs, TV on/off/source, optional "start disco"). Build them by capturing the current state of chosen devices, or edit them by hand. Run them from ⋯, a room sheet and the v9 quick tiles. Each scene is one batched set of HA calls, and protected devices are never switched off.
  - **Sleep timer:** from any light, plug, TV or room sheet, "off in 15 / 30 / 60 / custom minutes". Runs server-side, shows a visible countdown, can be cancelled, and survives restarts.
- **v10/presence — Presence-based lighting**
  - Opt-in per room, off by default. When a linked door contact opens after dusk (sun position from HA `sun.sun` or computed for Europe/London), the room's chosen lights come on. They go off after a configurable quiet period with no door activity.
  - It never fights a manual change: a manual on/off pauses it for that room until the next quiet period. It never runs while Away, and it is debounced.
- **v10/tariff — Octopus tariff + appliance run log**
  - **Tariff:** Octopus Agile/Go rates from the public Octopus API (region and product configurable; cached; works offline with the last-known rates). Show today's and tomorrow's rates, and use them in energy costs.
    - Suggest the cheapest window to run the washer or dishwasher, based on its typical run length from the v7 appliance stats. An optional reminder notification is off by default.
    - Never switch appliances on automatically.
  - **Run log:** each appliance run is logged with start and end time, energy and cost, plus an optional note added from the appliance sheet. Shown as a history list per appliance.
- **v10/guest — Guest QR access** (merge LAST)
  - An admin creates a time-limited guest link or QR code (shown on screen; generate the QR client-side with no CDN, e.g. a small vendored encoder). It grants access to one room's lights only, or to chosen devices. It expires automatically and can be revoked.
  - It builds on the v9 guest role, so a guest session can't reach anything else. Enforce this server-side, with tests per endpoint. Treat it as security-sensitive: unguessable tokens, hashed at rest, rate-limited, and never exposing other device names or state.

## Worker rules (put these in every worker prompt)
- Start from the latest `main`. Work on and push ONLY your assigned `v10/...` branch. **NEVER push to `main` or any other branch, never merge into main, never open a PR.** (Pushing to main deploys to the live app.)
- Commit messages end with exactly:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: <your session URL>
  ```
  No model names or identifiers in code, comments or commits.
- Never contact the user's real HA or real devices. Use mocks and `e2e/fake_ha.py` only (extend it as needed). External APIs (Octopus) are mocked in tests.
- Keep edits to shared files (app.py, store.py, app.js, index.html, style.css, sw.js, fake_ha.py, conftest.py) small and localized; put new code in new files.
- `docker build --target test .` must pass. Run the full e2e suite (`e2e/docker.sh`) and add e2e tests (desktop + 390px touch phone, both themes, plus a guest-role check where relevant). Look at the screenshots and fix anything ugly. Update README.md.
- Finish with a report: branch + commits, files touched, real test output, merge notes (shared-file edits, new deps/env/API), caveats.

## Orchestrator procedure
1. Read the repo, then create the 5 worker sessions in parallel with self-contained prompts (context + package + worker rules). Tag them `homecontrol-v10`.
2. Monitor them with `get_session`, `list_events(kinds=["result"])`, `git ls-remote origin 'refs/heads/v10/*'` and `send_later` check-ins (do not busy-poll). Answer worker questions; steer workers that drift.
3. Integrate on your own branch in this order: **ops** → scenes → presence → tariff → **guest last**.
   - After each merge: resolve conflicts, take the union of sw.js SHELL and set VERSION one above every branch's, then run pytest and the full e2e suite.
   - After guest: check that every new v10 endpoint enforces the right role.
   - Fix races by waiting for real conditions, never with sleeps or skipped tests.
4. Open ONE pull request from your branch to `main` and get its CI green.
5. Then give the user a short summary of each feature plus any setup they need (Octopus region/product, which rooms get presence lighting, how to create a guest link, and anything to change on dockerbox for rollback, such as redeploying the stack with the new compose file). **Ask before merging.** Merge the PR only with their explicit OK. Then confirm the `publish` job succeeded, which means Watchtower will deploy within about 5 minutes.
