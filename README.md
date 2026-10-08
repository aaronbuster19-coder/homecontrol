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
The ↻ button (or `POST /api/devices/refresh`) re-discovers immediately. State is polled every 5 s.

**Not shown until they're in HA:** the 3 plugs and 1 valve that aren't added to Home Assistant yet.
Add them in HA, then press ↻.

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
signed in for 90 days (an HttpOnly cookie, `Secure` when served over https). Sign out with the exit-arrow button in the header.
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

## Using it

- **View:** tap a light or plug to switch it on/off straight away. Tap a valve or sensor (marker or list row)
  for its sheet: toggle / temperature +/− / open-closed.
  Colours: amber = on, grey = off, orange = valve heating, blue = valve at target, red = door open, green = closed.
- **Edit:** Edit → “+ Room”, then drag a rectangle on the plan and name it (sizes are pre-filled and can be tweaked), drag rooms and markers (a room carries the devices inside it), select a room and drag its handles to resize,
  drag unplaced devices from the side list onto the plan (or tap one, then tap the plan), select + Delete,
  “Edit room” (or double-click) to rename or resize, then Save.
- **L-shaped rooms:** select a room and press “L-shape” to cut out a corner; press again to move the cut to the
  next corner (NE → SE → SW → NW → off). Drag the white handle at the inner corner to size the cut.
- **Doors and windows:** “+ Door” / “+ Window”, then tap near a wall (inner L walls too). Drag one to slide it along
  its wall, drag its end handles to change its length, select + Delete to remove it. With a door selected,
  “Link sensor” ties it to a door sensor so the door turns red (open) / green (closed) in view mode.
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
- Units: m/ft selector. The layout is always stored in metres; the selector changes how sizes are shown and entered.

## Live updates, power and battery

- **Live:** the server keeps one websocket open to Home Assistant (`HA_URL` with `http`→`ws` / `https`→`wss`,
  path `/api/websocket`, same token) and pushes every change to the browser over Server-Sent Events
  (`GET /api/events`), so changes made from wall switches or the Tapo app show up within about a second.
  The status line shows “● live” while the stream is connected. If the websocket is unreachable the server
  polls `/api/states` every 10 s (reconnecting with 1 s → 60 s backoff), and if the browser's stream drops
  the page polls every 5 s until it reconnects. Behind Cloudflare/Caddy nothing extra is needed
  (keep-alive pings every 20 s; `X-Accel-Buffering: no`).
- **Power:** P110/TP11 plugs show current watts under the marker and in the list (“on · 12 W”), the sheet shows
  today's kWh, and the header shows the total of all plugs (“⚡ 143 W”).
- **Battery:** T110 sensors and KE100 valves get a red dot on the marker when HA reports battery low or
  the level is below 20 %; the list shows “🔋 low” / “🔋 15 %” and the sheet shows the battery.
- These values come from the other entities of the same HA device, picked by `device_class`/unit
  (power W, energy kWh with “today” in its id or name, battery %, battery binary sensor) — not by entity-id
  patterns. Use ↻ (refresh) after adding devices in HA.

## API

`GET /healthz` · `POST /api/login` `{"username","password"}` · `POST /api/logout` · `GET /api/me` · `GET /api/devices` · `GET /api/events` (SSE: `snapshot`, then `device` events) · `POST /api/devices/refresh` · `POST /api/devices/{entity_id}/toggle` ·
`POST /api/devices/{entity_id}/temperature` `{"temperature": 21.0}` · `GET /api/layout` · `PUT /api/layout`

Devices carry `power` (W), `energy_today` (kWh), `battery` (%) and `battery_low` (bool) when HA knows them.

The layout is a single JSON document in SQLite (`DB_PATH`, default `/data/layout.db`).

## Development

CI: `.github/workflows/test.yml` runs on every push and same-repo PR on a **self-hosted** runner
(`runs-on: self-hosted`). It needs Docker on the runner (the runner user must be in the `docker` group);
it runs pytest via `docker build --target test` and then builds the production image. Fork PRs are skipped.

```sh
pip install -r requirements-dev.txt
python -m pytest backend/tests          # HA is mocked; no real devices touched
docker build --target test .            # same, inside the image
HA_URL=… HA_TOKEN=… APP_USER=u APP_PASSWORD=p DB_PATH=./data/layout.db \
  uvicorn backend.app:create_app --factory --reload
```
