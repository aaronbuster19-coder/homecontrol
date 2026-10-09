import httpx

# One line per record, pipe-separated. Primary lines: domain|entity_id|device name|manufacturer|model.
# Related lines: rel|primary entity_id|entity_id|device_class|unit|state_class|friendly_name, one for every
# sensor/binary_sensor on the same HA device (power, energy, battery are picked in discovery.py).
# Humidifier entities also get dc|entity_id|device_class (HA's dehumidifier/humidifier class).
DISCOVERY_TEMPLATE = """{% macro clean(v) %}{{ (v or '')|string|replace('|','/')|replace('\\n',' ') }}{% endmacro %}
{%- for domain, key in [('light','light'),('switch','switch'),('climate','climate'),('binary_sensor','binary'),('humidifier','humidifier')] %}{% for s in states[domain] %}{% set d = device_id(s.entity_id) %}
{{ key }}|{{ s.entity_id }}|{{ clean(device_attr(d,'name') if d else '') }}|{{ clean(device_attr(d,'manufacturer') if d else '') }}|{{ clean(device_attr(d,'model') if d else '') }}
{%- if key == 'humidifier' %}
dc|{{ s.entity_id }}|{{ clean(state_attr(s.entity_id,'device_class')) }}
{%- endif %}
{%- if d %}{% for e in device_entities(d) if e != s.entity_id and (e.startswith('sensor.') or e.startswith('binary_sensor.')) %}
rel|{{ s.entity_id }}|{{ e }}|{{ clean(state_attr(e,'device_class')) }}|{{ clean(state_attr(e,'unit_of_measurement')) }}|{{ clean(state_attr(e,'state_class')) }}|{{ clean(state_attr(e,'friendly_name')) }}
{%- endfor %}{% endif %}{% endfor %}{% endfor %}
"""


class HAError(Exception):
    pass


class HAClient:
    def __init__(self, base_url: str, token: str, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            r = await self._client.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise HAError(f"Home Assistant unreachable at {self._client.base_url}: {type(e).__name__} {e}".rstrip()) from e
        if r.status_code >= 400:
            raise HAError(f"Home Assistant returned {r.status_code} for {path}")
        return r

    async def render_template(self, template: str) -> str:
        r = await self._request("POST", "/api/template", json={"template": template})
        return r.text

    async def states(self) -> list[dict]:
        return (await self._request("GET", "/api/states")).json()

    async def history(self, start, end, entity_ids: list[str], attributes: bool = False) -> list:
        """GET /api/history/period/<start>. Without attributes: minimal_response + no_attributes (much smaller)."""
        params = {"filter_entity_id": ",".join(entity_ids), "end_time": end.isoformat(timespec="seconds")}
        if not attributes:
            params.update(minimal_response="", no_attributes="")
        r = await self._request("GET", f"/api/history/period/{start.isoformat(timespec='seconds')}", params=params, timeout=30.0)
        return r.json()

    async def call_service(self, domain: str, service: str, data: dict) -> None:
        await self._request("POST", f"/api/services/{domain}/{service}", json=data)
