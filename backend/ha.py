import httpx

DISCOVERY_TEMPLATE = """{% for s in states.light %}{% set d = device_id(s.entity_id) %}light|{{ s.entity_id }}|{{ device_attr(d,'name') if d else '' }}|{{ device_attr(d,'manufacturer') if d else '' }}|{{ device_attr(d,'model') if d else '' }}
{% endfor %}{% for s in states.switch %}{% set d = device_id(s.entity_id) %}switch|{{ s.entity_id }}|{{ device_attr(d,'name') if d else '' }}|{{ device_attr(d,'manufacturer') if d else '' }}|{{ device_attr(d,'model') if d else '' }}
{% endfor %}{% for s in states.climate %}{% set d = device_id(s.entity_id) %}climate|{{ s.entity_id }}|{{ device_attr(d,'name') if d else '' }}|{{ device_attr(d,'manufacturer') if d else '' }}|{{ device_attr(d,'model') if d else '' }}
{% endfor %}{% for s in states.binary_sensor %}{% set d = device_id(s.entity_id) %}binary|{{ s.entity_id }}|{{ device_attr(d,'name') if d else '' }}|{{ device_attr(d,'manufacturer') if d else '' }}|{{ device_attr(d,'model') if d else '' }}
{% endfor %}"""


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
            raise HAError(f"Home Assistant unreachable: {e}") from e
        if r.status_code >= 400:
            raise HAError(f"Home Assistant returned {r.status_code} for {path}")
        return r

    async def render_template(self, template: str) -> str:
        r = await self._request("POST", "/api/template", json={"template": template})
        return r.text

    async def states(self) -> list[dict]:
        return (await self._request("GET", "/api/states")).json()

    async def call_service(self, domain: str, service: str, data: dict) -> None:
        await self._request("POST", f"/api/services/{domain}/{service}", json=data)
