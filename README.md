# homecontrol

Floor plan of the flat with live device state. Draw rooms, place devices, tap to control.
Everything goes through Home Assistant's REST API; the app never talks to Tapo/Kasa directly.

## What it shows

| Kind | HA entities | Control |
|---|---|---|
| Lights (L530, L630, L430C) | `light.*`, TP-Link | on/off |
| Plugs (P110, TP11, …) | `switch.<name>`, TP-Link, plug model; settings switches (`_led`, `_auto_off_enabled`, …) skipped | on/off |
| Radiator valves (KE100) | `climate.*` | target temp, shows current |
| Door/window sensors (T110) | `binary_sensor.contact_sensor_door*` (diagnostic ones like `_cloud_connection` skipped) | read-only, open/closed |

Discovery uses `POST /api/template` to read each entity's device model, and caches the result for 5 minutes.
*Refresh devices* in the ⋯ menu (or `POST /api/devices/refresh`) re-discovers immediately. State is polled every 5 s.

**Not shown until they're in HA:** the 3 plugs and 1 valve that aren't added to Home Assistant yet.
Add them in HA, then ⋯ → *Refresh devices*.

## Deploy on dockerbox

```sh
git clone … homecontrol && cd homecontrol
cp .env.example .env        # fill in HA_URL, HA_TOKEN, APP_USER, APP_PASSWORD
mkdir -p data && sudo chown 10001 data   # container runs as uid 10001
docker compose up -d --build
```

- `HA_TOKEN`: HA → your profile → Security → Long-lived access tokens.
- `HA_URL` must be reachable *from inside the container*. If HA is a container on the same Docker network, use
  `http://homeassistant:8123`. If HA uses host networking, uncomment `extra_hosts` in `docker-compose.yml` and use
  `http://host.docker.internal:8123`.
- `docker-compose.yml` joins an external network called `caddy`. Rename it to whatever network your Caddy container uses.
- Add `Caddyfile.snippet` to your Caddyfile with your hostname(s) and reload Caddy. Choose LAN-only or LAN + Tailscale there.

### Login

Sign in on `/login.html` with `APP_USER` / `APP_PASSWORD` (if either is empty, nobody can sign in). You then stay
signed in for 90 days (an HttpOnly cookie, `Secure` when served over https). Sign out from the ⋯ menu in the header.
- `SESSION_SECRET` (optional) signs the cookie. If unset, a random one is generated once and kept in `session_secret`
  next to `DB_PATH` (e.g. `/data/session_secret`), so sessions survive restarts.
- Changing `APP_PASSWORD` (or `SESSION_SECRET`) signs everyone out.
- 10 wrong passwords from one client within 10 minutes → locked out for the rest of that window (HTTP 429).
- `curl -u user:pass` (HTTP Basic) still works for the API; the browser never gets a Basic popup.
- Public without login: `/healthz`, the login page, manifest, icons, service worker.

### Install the app

It's a PWA, so it installs like an app and opens full-screen:
- **Chrome desktop:** the install icon at the right of the address bar (or ⋮ → Cast, save and share → Install).
- **Android (Chrome):** ⋮ → *Install app* / *Add to Home screen*.
- **iPhone (Safari):** Share → *Add to Home Screen*.

The last loaded plan and device states are cached, so it opens offline ("Offline — showing last known state").

### Dockge

`dockge/compose.yaml` builds the image straight from GitHub. Paste it into a new Dockge stack and set
`HA_URL` (e.g. `http://host.docker.internal:8123`), `HA_TOKEN`, `APP_USER`, `APP_PASSWORD` in the stack's `.env`.
It listens on `127.0.0.1:8078` (override with `WEB_PORT`), so point a host Caddy at it:
`reverse_proxy 127.0.0.1:8078`. To pick up new code: `docker compose build --no-cache && docker compose up -d`.

### Auto deploy

When a push to `main` passes the tests, the `deploy` job in `.github/workflows/test.yml` (same self-hosted runner, which must
run on dockerbox) builds that exact commit, tags it `homecontrol:latest`, and recreates the stack with
`docker compose up -d --no-build` in `/opt/stacks/homecontrol`. It waits for the container's healthcheck; if the new
container isn't healthy within ~2.5 min it puts the previous image (`homecontrol:previous`) back and fails the job.

- The runner's user needs Docker access and read access to the stack folder (including its `.env`).
- Different folder: set the repository variable `DEPLOY_DIR`. Switch auto deploy off: repository variable `AUTO_DEPLOY=false`
  (GitHub → Settings → Secrets and variables → Actions → Variables).
- Manual rollback: `docker tag homecontrol:previous homecontrol:latest && docker compose up -d --no-build --force-recreate`.

## Using it

- **View:** tap a light or plug to switch it on/off straight away. Tap a valve or sensor (marker or list row)
  for its sheet: toggle / temperature +/− / open-closed.
  Colours: amber = on, grey = off, orange = valve heating, blue = valve at target, red = door open, green = closed.
- **Edit:** Edit → “+ Room”, then drag a rectangle on the plan and name it (sizes are pre-filled and can be tweaked), drag rooms and markers (a room carries the devices inside it), select a room and drag its handles to resize,
  drag unplaced devices from the side list onto the plan (or tap one, then tap the plan), select + Delete,
  “Edit room” (or double-click) to rename or resize, then Save.
- **Snapping:** while you draw, move or resize a room (or drag an L's inner corner), its edges snap to the walls of
  the other rooms — flush against a neighbour, or in line with an edge further away (e.g. top edges level) — when
  within about 12 screen pixels (at most 0.4 m). Thin blue guide lines show the active snap. Hold **Alt** while
  dragging to switch it off for that drag; with nothing nearby, sizes snap to the 5 cm grid as before.
- **Tidy up** (edit toolbar): closes gaps and overlaps under 0.3 m between rooms that nearly touch by moving
  rooms (with their devices, doors and windows) or stretching one side. It shows the old outlines dashed:
  *Keep* or *Undo*; nothing is stored until Save, and Cancel throws it away.
- **Shared walls:** where two rooms touch, the wall between them is drawn once, so the plan reads as one flat;
  outside walls are drawn a little heavier. Doors and windows work on shared walls like on any other wall.
- **L-shaped rooms:** select a room and press “L-shape” to cut out a corner; press again to move the cut to the
  next corner (NE → SE → SW → NW → off). Drag the white handle at the inner corner to size the cut.
- **Doors and windows:** “+ Door” / “+ Window”, then tap near a wall (inner L walls too). Drag one to slide it along
  its wall, drag its end handles to change its length, select + Delete to remove it. With a door selected,
  “Link sensor” ties a door or window to a door/window sensor so it turns red (open) / green (closed) in view mode.
- **Brightness and colour:** long-press (about half a second) a light — marker or list row — for its sheet: on/off,
  brightness, white temperature (if the bulb has it) and colour swatches plus a colour picker (colour bulbs only).
  A lit colour bulb's marker shows its current colour.
- **Rooms:** in view mode tap a room's name to switch all lights inside it: any on → all off, otherwise all on.
- **All off** (power button in the header): turns off every light and plug after a confirmation, except plugs marked
  “Keep on” (long-press a plug to open its sheet and tick it; stored in the layout as `settings.keep_on`).
- **Heating** (thermometer button in the header): all radiator valves with current/target temperature, +/− per valve,
  and an “All radiators” target with Apply to set every valve at once.
- API: `POST /api/devices/{id}/light` (`brightness_pct`, `hs_color`, `rgb_color`, `color_temp_kelvin`),
  `POST /api/bulk` (`action` turn_on/turn_off, `entity_ids` lights/plugs), `POST /api/valves/temperature`
  (`temperature`, optional `entity_ids`).
- **History:** every device sheet (long-press a light/plug, tap a valve/sensor) has a collapsible *History* section with
  24h / 7d / 30d: plug power (W) with energy used in the range, valve current temperature (line) and target (dashed),
  light on/off and door open/closed bars. Tap or hover the chart for the value at that time. Data comes from HA's
  recorder (`/api/history/period`), downsampled to ≤300 points and cached 60 s (24h) / 10 min (7d, 30d); it's only
  fetched when the section is open.
- **Door log:** “Log” on the *Door / window sensors* header in the device list (or “Door log →” in a sensor's sheet):
  every open/close per door, newest first, with “open for 3 min” durations, opens today, longest open and
  “open since”; 24h / 7d. Built from HA history, so it covers time the app wasn't open.
- Units: m/ft selector in the ⋯ menu. The layout is always stored in metres; the selector changes how sizes are shown and entered.

## More menu (⋯), temperatures, Away/Home, backup

The ⋯ button at the right of the header holds: Away / I'm home, *Show temperatures on plan*, Export layout,
Import layout, Units (m/ft), Refresh devices and Sign out. It closes on a tap outside or Escape.

- **Temperatures on the plan:** every room with a radiator valve in it (L-shapes respected) is tinted by the
  valve's current temperature — blue at 16° or less, neutral around 19–20°, orange at 23° or more — with the
  temperature next to the room name (several valves in a room are averaged). Updates live; hidden in edit mode.
  The toggle is remembered per device (browser storage), default on.
- **Away / Home:** *Away…* shows what will happen and lets you set the radiator temperature while away
  (5–25°, default 16°). Away turns off all lights and plugs except “keep on” plugs, remembers each radiator's
  target and sets them all to the away temperature, and turns door alerts on. An “AWAY” badge shows in the
  status line and the menu item becomes *I'm home*, which puts every radiator back to its remembered target
  (vanished valves skipped, clamped 5–35°) and restores the previous alerts on/off setting; lights stay off.
  Pressing Away twice keeps the first remembered targets. The state is in SQLite, so it survives restarts.
  API: `GET /api/mode` → `{"mode","since","away_temp"}`, `POST /api/mode` `{"mode":"away"|"home"}` (returns a
  summary: `turned_off`, `kept_on`, `valves`, `alerts_enabled`), `PUT /api/mode/settings` `{"away_temp": 5–25}`.
- **Backup:** *Export layout* downloads the saved plan as `homecontrol-layout-YYYY-MM-DD.json`. *Import layout…*
  reads such a file, shows rooms / placed devices / doors-windows and any devices not in Home Assistant now, and
  replaces the plan on confirm. If the server rejects unknown devices you can *Import without unknown devices*
  (drops their placements, sensor links and keep-on entries).

## Wall tablet mode

A full-screen, always-on view for a tablet on the wall. Open it from ⋯ → *Wall mode*, or bookmark `/?wall`
(works offline too: the service worker serves the cached app).

- **Screen:** no header, edit toolbar or device list — the plan fills the screen with bigger markers and room names.
  A slim top bar shows a big clock and date, total power (⚡), the average indoor temperature from the radiator
  valves, a *Reconnecting…* / *Offline* pill when the live stream is down, a Home/Away button (same confirmation
  as ⋯ → Away) and *All off* (same confirmation as the header button). Taps work exactly as in the normal view:
  tap a light/plug to toggle it, long-press for its sheet, tap a valve or sensor for its sheet, tap a room name.
- **Dimming:** after 2 min without a touch, and always from 23:00 to 07:00, the screen goes black with a dim clock,
  power and temperature (plus any open door). The first touch only wakes it — it never switches anything. At night
  it dims again after 30 s. The dim clock moves a little every minute to avoid burn-in.
- **Exit / settings:** hold the clock for about a second → *Exit wall mode* or *Wall settings…* (night start/end,
  idle timeout in seconds, 0 = never; night re-dim delay). Settings are stored on that tablet only. On a computer,
  Escape also exits. Exiting removes `?wall` from the address.
- **Keeps running:** asks the browser to keep the screen on (Screen Wake Lock, re-acquired when the tab comes back),
  the login lasts 90 days and a signed-out tablet goes back to wall mode after signing in, the live stream
  reconnects by itself, and after 24 h the page quietly reloads during the next dim period to pick up new versions.

**Old tablet setup:** install the app (see *Install the app*; on an old iPad use Safari → *Add to Home Screen*),
sign in, open ⋯ → *Wall mode*, keep it on its charger, and turn off the OS auto-lock / screen timeout if the
browser has no Screen Wake Lock (Safari before iOS 16.4, old Android WebViews) — on iPad: Settings → Display &
Brightness → Auto-Lock → Never; on Android: Settings → Display → Screen timeout (or Developer options →
*Stay awake* while charging). Guided Access (iPad) or screen pinning (Android) keeps it in the app.

## Live updates, power and battery

- **Live:** the server keeps one websocket open to Home Assistant (`HA_URL` with `http`→`ws` / `https`→`wss`,
  path `/api/websocket`, same token) and pushes every change to the browser over Server-Sent Events
  (`GET /api/events`), so changes made from wall switches or the Tapo app show up within about a second.
  The status line shows “● live” while the server's HA websocket is up, and “↻ 10 s” while it is only polling. If the websocket is unreachable the server
  polls `/api/states` every 10 s (reconnecting with 1 s → 60 s backoff), and if the browser's stream drops
  the page polls every 5 s until it reconnects. Behind Cloudflare/Caddy nothing extra is needed
  (keep-alive pings every 20 s; `X-Accel-Buffering: no`).
- **Power:** P110/TP11 plugs show current watts under the marker and in the list (“on · 12 W”), the sheet shows
  today's kWh, and the header shows the total of all plugs (“⚡ 143 W”).
- **Battery:** T110 sensors and KE100 valves get a red dot on the marker when HA reports battery low or
  the level is below 20 %; the list shows “🔋 low” / “🔋 15 %” and the sheet shows the battery.
- These values come from the other entities of the same HA device, picked by `device_class`/unit
  (power W, energy kWh with “today” in its id or name, battery %, battery binary sensor) — not by entity-id
  patterns. Use ⋯ → *Refresh devices* after adding devices in HA.

## Alerts

Push notification on your phone when a door/window sensor stays open (works with the app closed).

- **Enable per device:** tap the bell in the header → *Enable on this device* and allow notifications.
  *Send test notification* checks it works. Each phone/browser is enabled separately; settings (on/off,
  minutes 1–120, default 5, “also notify when it closes”) are shared.
- **iPhone/iPad:** Web Push only works for the app added to the Home Screen (iOS 16.4+): Safari → Share →
  *Add to Home Screen*, open it from there, then enable alerts. If you once tapped “Don't allow”, re-allow
  notifications in the browser/iOS settings for the site.
- One notification per opening, “Front door has been open for 5 min” (checked every 15 s); closing resets it.
  Unavailable sensors don't count as open. After a restart, an open door's timer starts from HA's `last_changed`.
- **VAPID keys** are generated on first start and kept next to the database (`vapid_private.pem`, mode 600).
  Optional env overrides: `VAPID_PRIVATE_KEY` (PEM or base64url raw/DER key), `VAPID_PUBLIC_KEY`,
  `VAPID_SUBJECT` (contact sent to push services, default `mailto:admin@localhost` — set it to your
  `mailto:` address). Changing the key means every device must enable alerts again.

## Automations

These run on the server (every 15 s, sooner after a contact sensor changes), so they work with the app closed.
Each one has its own switch in the bell sheet, and settings are shared by all devices.

- **Window open → radiators down** (*Doors & windows* → “Radiators down while a window is open”, default on).
  When a **window** linked to a contact sensor (Edit → select the window → *Link sensor*) has been open for
  2 min (1–30), every radiator valve placed in that window's room goes to 7° (5–15). The room is the one whose
  wall (L-shaped inner walls included) the window sits on, within 5 cm; a window on a wall shared by two rooms
  affects both. Doors never trigger this. When every window affecting a radiator is closed again it goes back to
  the target it had — or to the away temperature if Away is on. Pressing *I'm home* while the window is still open
  keeps that radiator low and restores the home target once it closes; changing a held radiator yourself is
  respected (nothing is sent, and your value is used when the window closes). A sensor that is unavailable counts
  as still open. Optional push “Bedroom window open — radiator off” (default on). Held radiators survive a
  restart (SQLite); no temperature is ever sent twice in a row, and a failed call is retried after 5 min, not
  in a loop. Switching the automation off puts held radiators back straight away.
- **Device health** (default on): *Low battery* — one push per device when HA reports battery low or below 15 %,
  repeated at most weekly while it stays low, reset once it reads 20 % or more. *Offline* — one push when a device's
  main entity has been `unavailable` for 30 min (10–240) without a break, and “back online” once it has been
  available for 2 min (only if the offline push was sent). Short dropouts restart the timer, so flapping is quiet.
- **Weekly summary** (default on): Sunday 19:00 local time (`TZ_NAME`, default `Europe/London`; DST-safe) a push
  “Your week at home”: plug energy this week vs last (kWh, integrated from the plugs' power history), the plug that
  used most, the door/window opened most and the average temperature of each room with a radiator valve. Tapping it
  opens the *This week* sheet. If the server was down at 19:00 it is sent on the next start until Monday 12:00,
  never twice for the same week. *Preview* in the bell sheet builds one for the last 7 days without sending it;
  *Last summary* shows the last one sent.

To turn everything off: untick the four switches in the bell sheet (radiators down, low battery, offline,
weekly summary). Disabling alerts on a phone only stops pushes to that phone; the radiator automation still runs.

## API

`GET /healthz` · `POST /api/login` `{"username","password"}` · `POST /api/logout` · `GET /api/me` · `GET /api/devices` · `GET /api/events` (SSE: `snapshot`, `status` `{"ws": bool}`, then `device` events) · `POST /api/devices/refresh` · `POST /api/devices/{entity_id}/toggle` ·
`POST /api/devices/{entity_id}/temperature` `{"temperature": 21.0}` · `GET /api/layout` · `PUT /api/layout` ·
`GET /api/push/key` · `POST /api/push/subscribe` (PushSubscription JSON) · `POST /api/push/unsubscribe` `{"endpoint"}` ·
`POST /api/push/test` · `GET`/`PUT /api/alerts/settings` `{"enabled", "door_open_minutes", "notify_on_close",
"window_heating_enabled", "window_open_minutes", "window_off_temp", "window_notify", "health_battery", "health_unavailable",
"health_unavailable_minutes", "weekly_summary"}` (partial updates) · `GET /api/automations/status` (`windows_linked`, `held`) ·
`GET /api/summary/latest` (404 until the first one) · `POST /api/summary/preview` ·
`GET /api/history/{entity_id}?range=24h|7d|30d` (`series` `[{name, unit, points: [[t_ms, v|null]]}]`, `timeline` `[{state, start, end}]`, plugs: `energy_kwh`) ·
`GET /api/doors/log?range=24h|7d&tz=Europe/London` (per door: `events` `[{t, state, open_ms}]` newest first, `summary`)

Devices carry `power` (W), `energy_today` (kWh), `battery` (%) and `battery_low` (bool) when HA knows them.

The layout is a single JSON document in SQLite (`DB_PATH`, default `/data/layout.db`).

## Development

CI: `.github/workflows/test.yml` runs on every push and same-repo PR on a **self-hosted** runner
(`runs-on: self-hosted`). It needs Docker on the runner (the runner user must be in the `docker` group);
it runs pytest via `docker build --target test` and then builds the production image. Fork PRs are skipped.

```sh
pip install -r requirements-dev.txt
python -m pytest backend/tests          # HA is mocked; no real devices touched
pip install playwright && python -m pytest e2e   # browser tests (wall mode) against e2e/fake_ha.py
docker build --target test .            # same, inside the image
node --test tests/*.test.js            # snapping / wall maths (frontend/snap.js), plain Node, no npm
HA_URL=… HA_TOKEN=… APP_USER=u APP_PASSWORD=p DB_PATH=./data/layout.db \
  uvicorn backend.app:create_app --factory --reload
```
