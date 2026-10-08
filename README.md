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

Login is HTTP Basic auth (`APP_USER` / `APP_PASSWORD`). If either is empty, every request is refused.
`/healthz` is unauthenticated.

### Dockge

`dockge/compose.yaml` builds the image straight from GitHub. Paste it into a new Dockge stack and set
`HA_URL` (e.g. `http://host.docker.internal:8123`), `HA_TOKEN`, `APP_USER`, `APP_PASSWORD` in the stack's `.env`.
It listens on `127.0.0.1:8078` (override with `WEB_PORT`), so point a host Caddy at it:
`reverse_proxy 127.0.0.1:8078`. To pick up new code: `docker compose build --no-cache && docker compose up -d`.

## Using it

- **View:** tap a marker or a list row → sheet with toggle / temperature +/− / open-closed.
  Colours: amber = on, grey = off, orange = valve heating, blue = valve at target, red = door open, green = closed.
- **Edit:** Edit → “+ Room”, then drag a rectangle on the plan and name it (sizes are pre-filled and can be tweaked), drag rooms and markers (a room carries the devices inside it), select a room and drag its handles to resize,
  drag unplaced devices from the side list onto the plan (or tap one, then tap the plan), select + Delete,
  “Edit room” (or double-click) to rename or resize, then Save.
- Units: m/ft selector. The layout is always stored in metres; the selector changes how sizes are shown and entered.

## API

`GET /healthz` · `GET /api/devices` · `POST /api/devices/refresh` · `POST /api/devices/{entity_id}/toggle` ·
`POST /api/devices/{entity_id}/temperature` `{"temperature": 21.0}` · `GET /api/layout` · `PUT /api/layout`

The layout is a single JSON document in SQLite (`DB_PATH`, default `/data/layout.db`).

## Development

```sh
pip install -r requirements-dev.txt
python -m pytest backend/tests          # HA is mocked; no real devices touched
docker build --target test .            # same, inside the image
HA_URL=… HA_TOKEN=… APP_USER=u APP_PASSWORD=p DB_PATH=./data/layout.db \
  uvicorn backend.app:create_app --factory --reload
```
