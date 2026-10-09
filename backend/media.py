"""TVs and other media players: HA media_player.* entities (Samsung via SmartThings or the Samsung Smart TV integration,
LG webOS, Android TV, Chromecast, speakers…).

- fields(): the extra keys of a media device in /api/devices and SSE (state, volume, source, now playing, capabilities).
  The raw entity_picture is never sent: it can carry an HA access token. The browser gets /api/media/<id>/artwork?v=…
  instead, which the backend fetches from HA (with the token, server side) and serves.
- command(): strict validation of a control request -> (service, data). Only services the entity's supported_features
  allow; everything else is a 400.
"""
import hashlib
import math
import re

# HA's MediaPlayerEntityFeature bits (homeassistant/components/media_player/const.py)
PAUSE = 1
SEEK = 2
VOLUME_SET = 4
VOLUME_MUTE = 8
PREVIOUS_TRACK = 16
NEXT_TRACK = 32
TURN_ON = 128
TURN_OFF = 256
PLAY_MEDIA = 512
VOLUME_STEP = 1024
SELECT_SOURCE = 2048
STOP = 4096
CLEAR_PLAYLIST = 8192
PLAY = 16384
SHUFFLE_SET = 32768
SELECT_SOUND_MODE = 65536

# capability -> any of these bits (HA registers media_play_pause for PLAY | PAUSE, volume_up/down for STEP | SET)
CAPS = {
    "turn_on": TURN_ON, "turn_off": TURN_OFF, "volume_set": VOLUME_SET, "volume_step": VOLUME_STEP | VOLUME_SET,
    "mute": VOLUME_MUTE, "select_source": SELECT_SOURCE, "play_pause": PLAY | PAUSE, "next": NEXT_TRACK,
    "previous": PREVIOUS_TRACK, "sound_mode": SELECT_SOUND_MODE,
}
OFF_STATES = ("off", "standby", "unavailable", "unknown")
LIST_MAX, TEXT_MAX = 100, 200
SPEAKER = re.compile(r"speaker|soundbar|sound bar|sonos|homepod|echo|nest (audio|mini)|home mini|audio|receiver|\bamp\b|denon|yamaha|bose", re.I)
TV = re.compile(r"\btv\b|television|samsung|\blg\b|webos|bravia|tizen|android ?tv|google ?tv|chromecast|fire ?tv|apple ?tv|roku|shield|"
                r"philips|panasonic|hisense|tcl|\bqe\d|\bue\d|\bqn\d|\bun\d|oled|qled", re.I)


class MediaError(ValueError):
    pass


def features(attrs: dict) -> int:
    sf = attrs.get("supported_features")
    return sf if isinstance(sf, int) and not isinstance(sf, bool) and sf >= 0 else 0


def decode(sf: int) -> dict[str, bool]:
    """supported_features bitmask -> what the TV sheet may offer."""
    return {k: bool(sf & bits) for k, bits in CAPS.items()}


def media_type(attrs: dict, model: str = "", name: str = "", entity_id: str = "") -> str:
    """tv | speaker | other: HA's device_class first, then what the device model / name says."""
    dc = str(attrs.get("device_class") or "").lower()
    if dc == "tv":
        return "tv"
    if dc in ("speaker", "receiver"):
        return "speaker"
    text = f"{model} {name} {entity_id.replace('_', ' ')}"
    if SPEAKER.search(text):
        return "speaker"
    if TV.search(text):
        return "tv"
    return "other"


def _text(v) -> str | None:
    return v[:TEXT_MAX] if isinstance(v, str) and v else None


def _list(v) -> list[str]:
    return [s for s in v if isinstance(s, str) and s][:LIST_MAX] if isinstance(v, list) else []


def picture_version(url) -> str | None:
    return hashlib.sha256(url.encode()).hexdigest()[:12] if isinstance(url, str) and url else None


def is_on(state: str | None) -> bool:
    return state not in OFF_STATES and state is not None


def fields(entity_id: str, model: str, name: str, attrs: dict, state: str | None) -> dict:
    sf = features(attrs)
    vol = attrs.get("volume_level")
    vol = round(min(1.0, max(0.0, float(vol))), 3) if isinstance(vol, (int, float)) and not isinstance(vol, bool) and math.isfinite(vol) else None
    muted = attrs.get("is_volume_muted")
    out = {
        "media_type": media_type(attrs, model, name, entity_id), "supported_features": sf, "supports": decode(sf),
        "volume_level": vol, "is_volume_muted": muted if isinstance(muted, bool) else None,
        "source": _text(attrs.get("source")), "source_list": _list(attrs.get("source_list")),
        "sound_mode": _text(attrs.get("sound_mode")), "sound_mode_list": _list(attrs.get("sound_mode_list")),
        "app_name": _text(attrs.get("app_name")), "media_title": _text(attrs.get("media_title")),
        "media_series_title": _text(attrs.get("media_series_title")), "media_artist": _text(attrs.get("media_artist")),
        "media_content_type": _text(attrs.get("media_content_type")), "picture": None,
    }
    v = picture_version(attrs.get("entity_picture"))
    if v and is_on(state):
        out["picture"] = f"/api/media/{entity_id}/artwork?v={v}"
    return out


def _need(item: dict, cap: str, what: str) -> None:
    if not (item.get("supports") or {}).get(cap):
        raise MediaError(f"{item.get('name') or item.get('entity_id')} can't {what} through Home Assistant")


def _only(body: dict, *keys: str) -> None:
    extra = set(body) - {"action", *keys}
    if extra:
        raise MediaError(f"unexpected field {sorted(extra)[0]!r}")


def _bool(body: dict, key: str) -> bool:
    v = body.get(key)
    if not isinstance(v, bool):
        raise MediaError(f"{key} must be true or false")
    return v


def _choice(body: dict, key: str, options: list[str], what: str) -> str:
    v = body.get(key)
    if not isinstance(v, str) or v not in options:
        raise MediaError(f"{key} must be one of the {what} the TV reports")
    return v


ACTIONS = ("power", "volume", "mute", "source", "play_pause", "next", "previous", "sound_mode")


def command(body, item: dict) -> tuple[str, dict]:
    """A control request {"action", …} for one media player item -> (media_player service, extra service data)."""
    if not isinstance(body, dict):
        raise MediaError("send a JSON object")
    action = body.get("action")
    if action not in ACTIONS:
        raise MediaError(f"action must be one of: {', '.join(ACTIONS)}")
    if item.get("state") in ("unavailable", "unknown") and action != "power":
        raise MediaError(f"{item.get('name')} is {item.get('state')}")
    if action == "power":
        _only(body, "on")
        on = _bool(body, "on")
        if on:
            _need(item, "turn_on", "be turned on")
            return "turn_on", {}
        _need(item, "turn_off", "be turned off")
        return "turn_off", {}
    if action == "volume":
        if "level" in body:
            _only(body, "level")
            lv = body["level"]
            if isinstance(lv, bool) or not isinstance(lv, (int, float)) or not math.isfinite(lv) or not 0 <= lv <= 1:
                raise MediaError("level must be 0–1")
            _need(item, "volume_set", "set its volume")
            return "volume_set", {"volume_level": round(float(lv), 3)}
        _only(body, "step")
        step = body.get("step")
        if step not in ("up", "down"):
            raise MediaError("send level (0–1) or step (up/down)")
        _need(item, "volume_step", "change its volume")
        return f"volume_{step}", {}
    if action == "mute":
        _only(body, "muted")
        muted = _bool(body, "muted")
        _need(item, "mute", "be muted")
        return "volume_mute", {"is_volume_muted": muted}
    if action == "source":
        _only(body, "source")
        _need(item, "select_source", "switch source")
        return "select_source", {"source": _choice(body, "source", item.get("source_list") or [], "sources")}
    if action == "sound_mode":
        _only(body, "sound_mode")
        _need(item, "sound_mode", "change sound mode")
        return "select_sound_mode", {"sound_mode": _choice(body, "sound_mode", item.get("sound_mode_list") or [], "sound modes")}
    _only(body)
    _need(item, {"play_pause": "play_pause", "next": "next", "previous": "previous"}[action],
          {"play_pause": "play or pause", "next": "skip", "previous": "go back"}[action])
    return {"play_pause": "media_play_pause", "next": "media_next_track", "previous": "media_previous_track"}[action], {}


def away_off(devices: dict, states: dict) -> list[str]:
    """Media players Away turns off: on (not off / standby / unavailable) and able to turn off."""
    out = []
    for eid, d in devices.items():
        if d.kind != "media":
            continue
        s = states.get(eid) or {}
        if is_on(s.get("state")) and features(s.get("attributes") or {}) & TURN_OFF:
            out.append(eid)
    return sorted(out)


ARTWORK_MAX = 5 * 1024 * 1024


def _private_host(host: str | None) -> bool:
    """Literal loopback/private/link-local addresses and local names: the server must not be used to reach other
    machines on the home network through a media player's picture URL."""
    import ipaddress
    if not host:
        return True
    h = host.lower().rstrip(".")
    if h == "localhost" or h.endswith((".local", ".lan", ".home", ".internal")) or "." not in h:
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified


async def artwork(ha, url: str) -> tuple[bytes, str]:
    """Fetch a media player's entity_picture. HA's own paths (/api/media_player_proxy/…) go through the HA client, with
    the token; an external URL (a streaming service's CDN) is fetched without any credentials. Images only."""
    import httpx
    from urllib.parse import urlsplit

    from .ha import HAError
    if not isinstance(url, str) or not url:
        raise MediaError("no artwork")
    parts, base = urlsplit(url), urlsplit(ha.base_url)
    if url.startswith("/") and not url.startswith("//"):
        r = await ha.get_image(url)
    elif parts.scheme in ("http", "https") and (parts.scheme, parts.netloc) == (base.scheme, base.netloc):
        r = await ha.get_image(parts.path + (f"?{parts.query}" if parts.query else ""))
    elif parts.scheme in ("http", "https") and parts.netloc:
        if _private_host(parts.hostname):
            raise MediaError("artwork on a local address")  # only HA's own URL may point inside the network
        try:
            async with httpx.AsyncClient(transport=ha.transport, timeout=10.0, follow_redirects=False) as c:
                r = await c.get(url)
        except httpx.HTTPError as e:
            raise HAError(f"artwork unreachable: {type(e).__name__}") from e
        if r.status_code >= 400:
            raise HAError(f"artwork returned {r.status_code}")
    else:
        raise MediaError("unsupported artwork URL")
    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
    if not ctype.startswith("image/") or ctype == "image/svg+xml":  # no SVG: it could carry script
        raise MediaError("artwork isn't an image")
    if len(r.content) > ARTWORK_MAX:
        raise MediaError("artwork too large")
    return r.content, ctype
