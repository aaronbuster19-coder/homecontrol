# homecontrol v9 — orchestrator brief

You are the **orchestrator** for v9 of homecontrol. You do not write the features yourself. Split the work across **worker sessions** (create them with `mcp__claude-code-remote__create_session`, one per work package below, all in parallel; use in-process subagents only for small read-only lookups). Your job:

1. Hand out the tasks.
2. Monitor the workers.
3. Review and merge their branches.
4. Fix integration races.
5. Get CI green.
6. Ask the user before pushing to `main`.

## Project context
- Repo: `aaronbuster19-coder/homecontrol`. FastAPI backend (`backend/`), vanilla JS frontend (`frontend/`, no build step, no CDN libs), SVG floor plan of the user's flat with Home Assistant (HA) devices. User is in the UK (Europe/London).
- It is LIVE on the user's real HA (control.aaronsmith.live). `main` auto-deploys via `.github/workflows/test.yml`: jobs `test` (`docker build --target test .` = pytest) and `e2e` (`e2e/docker.sh`, Playwright image `mcr.microsoft.com/playwright/python:v1.56.0-noble`) must both pass before `deploy`. The deploy health-checks the new container and rolls back if it is unhealthy.
- CI runs on ONE self-hosted runner (`dockerbox1`). Jobs queue behind each other; if a run sits queued for 20+ minutes with nothing running, tell the user the runner may need restarting.
- Read README.md first. Key files: `backend/{app,auth,store,live,alerts,ha,discovery,modes,summary,history,appliances,media,automations}.py`, `backend/tests/`, `e2e/` (incl. `fake_ha.py`, `conftest.py`), `frontend/{index.html,app.js,floorplan.js,furniture.js,modes.js,theme.js,activity.js,weather.js,energy.js,sw.js,style.css}`.
- Existing conventions workers must follow:
  - Layout PUTs that omit `names/hidden/energy/all_off_include/furniture` keep the stored value (`store.carry_settings`); keep layout JSON backward compatible.
  - Static files are served with `Cache-Control: no-cache`, and `sw.js` fetches with `cache: "no-cache"`. A new frontend file must be added to `sw.js` SHELL and the VERSION bumped (currently `hc-v21`; the orchestrator resolves clashes).
  - The header is full at 390px. New entry points go in the ⋯ More menu or existing sheets, never new header buttons.
  - Colours must use the theme's CSS variables (light/dark), not hard-coded hex.
  - e2e fixtures send `"furniture": []` for a clean layout. Wait for elements/bounding boxes rather than fixed coordinates (a fixed tap point broke when the More menu grew).
- Safety principles: no unrequested automatic switching; every automation is opt-in, off by default, debounced, never loops service calls, and has an off switch. The fridge, keep-on plugs and the home server are protected. Unit-test timing logic with an injected clock.

## Work packages (one worker each, branch names exactly as given)
- **v9/brief — Morning brief card + monthly energy report**
  - Brief card (shown once per morning, dismissable, also opened from ⋯): overnight door log, today's weather (reuse weather.js/backend), yesterday's energy cost, anything left on.
  - Monthly report: per-appliance share of energy cost, compared with last month. Reuse the existing energy/cost and summary code.
- **v9/climate — Smart preheat + damp/mould warnings**
  - Preheat: learn each room's warm-up rate from valve/temperature history and start the valves early so the room reaches the scheduled setpoint on time. Opt-in per room; caps on how early it can start; never runs while Away or with a window open.
  - Damp: a sustained high-humidity + low-temperature risk raises an alert (push via existing alerts.py, with cooldown) and suggests the dehumidifier. Thresholds are configurable.
- **v9/tiles — Home-screen quick tiles**
  - A compact PWA view (e.g. `/tiles` or `?view=tiles`) of user-pinned favourite controls, plus manifest shortcuts.
  - Pinning is done from existing device sheets, and pins are stored server-side.
- **v9/users — Multi-user logins**
  - Multiple accounts with roles: admin (everything), member (control devices, no settings/layout/user changes), guest (lights and limited controls only, optional expiry).
  - Admin UI for adding, removing and resetting users. Existing single-user setups must migrate seamlessly, so the current login becomes admin.
  - Enforce roles server-side on EVERY endpoint, with tests per role. Treat this as security-sensitive: hashed passwords, no user enumeration, sessions invalidated on removal or password change.
- **v9/underlay — Floor-plan photo underlay**
  - In edit mode, upload an image (validate type and size; store next to DB_PATH; serve it with auth).
  - Position, scale, rotate and set the opacity of the image under the plan, so walls can be traced over it. Toggle it off in normal view.
  - The stored layout needs no changes, or a backward-compatible optional field.

## Worker rules (put these in every worker prompt)
- Start from the latest `main`. Work on and push ONLY your assigned `v9/...` branch. **NEVER push to `main` or any other branch, never merge into main, never open a PR.** (In v8 a worker pushed to main itself; it must not happen again.)
- Commit messages end with exactly:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: <your session URL>
  ```
  No model names or identifiers in code, comments or commits.
- Never contact the user's real HA or real devices. Use mocks and `e2e/fake_ha.py` only (extend it as needed).
- Keep edits to shared files (app.py, store.py, app.js, index.html, style.css, sw.js, fake_ha.py, conftest.py) small and localized, because four other sessions edit the same repo. Put new code in new files.
- `docker build --target test .` must pass. Run the full e2e suite (`e2e/docker.sh`) and add e2e tests for the feature (desktop + 390px touch phone, both themes). Look at the screenshots and fix anything ugly. Update README.md.
- Finish with a report: branch + commits, files touched, real test output, merge notes (shared-file edits, new deps/env/API), caveats.

## Orchestrator procedure
1. Read the repo, then create the 5 worker sessions in parallel with self-contained prompts (context + package + worker rules). Tag them `homecontrol-v9`.
2. Monitor them with `get_session`, `list_events(kinds=["result"])`, `git ls-remote origin 'refs/heads/v9/*'` and `send_later` check-ins (do not busy-poll). Answer worker questions; steer workers that drift.
3. Integrate on your own branch in this order: brief → climate → underlay → tiles → **users last**. Users touches auth on every endpoint, so after merging it, check that every new v9 endpoint enforces the right role.
   - After each merge: resolve conflicts, take the union of sw.js SHELL and set VERSION one above every branch's, then run pytest and the full e2e suite.
   - Fix races by waiting for real conditions, never with sleeps or skipped tests.
4. Push your branch and confirm CI (test + e2e) is green on it.
5. Then give the user a short summary of what each feature does, plus any setup they need (e.g. adding users, uploading the photo, enabling preheat per room). **Ask before pushing to `main`.** Push to main only with their explicit OK (`git merge-base --is-ancestor origin/main HEAD`, then `git push origin HEAD:main`), then confirm the deploy job succeeded.
