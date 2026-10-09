"""Rollback guard for dockerbox: Watchtower updates the app, this puts the last healthy version back if the new one
never gets healthy.

Runs as its own small container in the Dockge stack (dockge/compose.yaml), next to Watchtower, with the Docker socket.
Every POLL seconds it inspects the app container (GUARD_TARGET, default "homecontrol"):

- Healthy (Docker health check passing, or running without one): that image becomes the known-good one. Older
  images of the app are removed (Watchtower runs without --cleanup so the previous image stays for a rollback).
- A new image (not the known-good one) that is not healthy for ROLLBACK_AFTER seconds in a row (default 150):
  the container is recreated from the known-good image (tagged <repo>:rollback), with the same settings and its
  Watchtower label set to "false" so Watchtower leaves it alone. The failed container is kept, stopped, as
  "<name>-failed" for its logs. Updates are then paused.
- While paused, every CHECK_EVERY seconds (default 300) it asks the registry for <repo>:<tag>'s digest. When a
  newer image than the failed one is published it pulls it and recreates the app on <repo>:<tag> with the Watchtower
  label back on, and the new image gets the same probation. Recreating the app by hand (Dockge → Update, or
  `docker compose up -d`) also ends the pause.

An image that was healthy once is never rolled back later (an HA outage is not the image's fault), and with no
known-good image there is nothing to roll back to. ROLLBACK_ENABLED=false keeps it watching and logging only.
State survives restarts in STATE_PATH. `python rollback_guard.py status` prints it.

Standard library only; Docker's Engine API over the unix socket.
"""
import base64
import copy
import http.client
import json
import os
import socket
import sys
import time
import urllib.parse

WT_LABEL = "com.centurylinklabs.watchtower.enable"
FROM_LABEL = "homecontrol.rollback.from"
AT_LABEL = "homecontrol.rollback.at"
# Compose compares this with the service's image to decide whether `up` must recreate the container: keep it true to
# the image actually running, so Dockge → Update (the undo) recreates a rolled-back app.
COMPOSE_IMAGE_LABEL = "com.docker.compose.image"


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


# ---------- Docker Engine API ----------
class DockerError(Exception):
    def __init__(self, status, msg):
        super().__init__(f"{status}: {msg}")
        self.status = status


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.unix_path)
        self.sock = s


def split_ref(ref):
    """'ghcr.io/a/b:latest' -> ('ghcr.io/a/b', 'latest'); a digest ref keeps '@sha256:…' as the tag part."""
    if "@" in ref:
        repo, digest = ref.split("@", 1)
        return repo, digest
    slash = ref.rfind("/")
    colon = ref.rfind(":")
    if colon > slash:
        return ref[:colon], ref[colon + 1:]
    return ref, "latest"


def registry_auth(repo, config_path):
    """X-Registry-Auth for repo's registry from a docker config.json (plain "auth" entries), or None."""
    first = repo.split("/", 1)[0]
    host = first if ("." in first or ":" in first or first == "localhost") and "/" in repo else "https://index.docker.io/v1/"
    try:
        with open(config_path) as f:
            auths = json.load(f).get("auths", {})
    except (OSError, ValueError):
        return None
    entry = auths.get(host) or auths.get("https://" + host) or {}
    if not entry.get("auth"):
        return None
    user, _, password = base64.b64decode(entry["auth"]).decode().partition(":")
    return base64.urlsafe_b64encode(json.dumps({"username": user, "password": password, "serveraddress": host}).encode()).decode()


class DockerAPI:
    def __init__(self, socket_path="/var/run/docker.sock", config_path="/config.json", timeout=60):
        self.socket_path, self.config_path, self.timeout = socket_path, config_path, timeout

    def _call(self, method, path, query=None, body=None, headers=None, ok=(200, 201, 204)):
        if query:
            path += "?" + urllib.parse.urlencode(query)
        conn = _UnixConnection(self.socket_path, self.timeout)
        try:
            hdrs = dict(headers or {})
            data = None
            if body is not None:
                data = json.dumps(body).encode()
                hdrs["Content-Type"] = "application/json"
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
        finally:
            conn.close()
        if resp.status not in ok:
            try:
                msg = json.loads(raw).get("message", raw.decode(errors="replace"))
            except ValueError:
                msg = raw.decode(errors="replace")
            raise DockerError(resp.status, msg)
        return raw

    def _json(self, *a, **kw):
        raw = self._call(*a, **kw)
        return json.loads(raw) if raw else None

    def _auth(self, ref):
        a = registry_auth(split_ref(ref)[0], self.config_path)
        return {"X-Registry-Auth": a} if a else {}

    def container(self, name):
        try:
            return self._json("GET", f"/containers/{name}/json")
        except DockerError as e:
            if e.status == 404:
                return None
            raise

    def image(self, ref):
        try:
            return self._json("GET", f"/images/{ref}/json")
        except DockerError as e:
            if e.status == 404:
                return None
            raise

    def images(self):
        return self._json("GET", "/images/json")

    def tag(self, image_id, repo, tag):
        self._call("POST", f"/images/{image_id}/tag", {"repo": repo, "tag": tag})

    def remove_image(self, ref):
        self._call("DELETE", f"/images/{ref}")

    def pull(self, ref):
        repo, tag = split_ref(ref)
        raw = self._call("POST", "/images/create", {"fromImage": repo, "tag": tag}, headers=self._auth(ref))
        for line in raw.decode(errors="replace").splitlines():  # the progress stream reports failures in-band
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("error"):
                raise DockerError(500, msg["error"])

    def remote_digest(self, ref):
        return self._json("GET", f"/distribution/{ref}/json", headers=self._auth(ref))["Descriptor"]["digest"]

    def stop(self, cid, timeout=10):
        self._call("POST", f"/containers/{cid}/stop", {"t": timeout}, ok=(204, 304))

    def start(self, cid):
        self._call("POST", f"/containers/{cid}/start", ok=(204, 304))

    def rename(self, cid, name):
        self._call("POST", f"/containers/{cid}/rename", {"name": name})

    def remove_container(self, cid):
        self._call("DELETE", f"/containers/{cid}", {"force": "true"})

    def create(self, name, body):
        return self._json("POST", "/containers/create", {"name": name}, body=body)["Id"]

    def connect(self, network, cid, endpoint):
        self._call("POST", f"/networks/{network}/connect", body={"Container": cid, "EndpointConfig": endpoint})


# ---------- recreating a container ----------
IMAGE_DEFAULT_KEYS = ("Cmd", "Entrypoint", "WorkingDir", "User", "Healthcheck", "StopSignal", "ExposedPorts", "Volumes",
                      "Shell", "OnBuild")


def create_config(container, image_config):
    """The container's Config minus what came from its image, so another image's defaults apply (as Watchtower
    does): env, command, health check etc. of the failed image must not leak into the rolled-back one."""
    cfg = copy.deepcopy(container["Config"])
    img = image_config or {}
    img_env = set(img.get("Env") or [])
    cfg["Env"] = [e for e in cfg.get("Env") or [] if e not in img_env]
    for k in IMAGE_DEFAULT_KEYS:
        if k in cfg and cfg[k] == img.get(k):
            del cfg[k]
    labels = dict(cfg.get("Labels") or {})
    for k, v in (img.get("Labels") or {}).items():
        if labels.get(k) == v:
            del labels[k]
    cfg["Labels"] = labels
    if cfg.get("Hostname") == container["Id"][:12]:
        del cfg["Hostname"]
    cfg.pop("Image", None)
    return cfg


def endpoints(container):
    """Network name -> EndpointConfig for re-attaching: aliases, static IPs, links (not the old addresses)."""
    out = {}
    for net, ep in ((container.get("NetworkSettings") or {}).get("Networks") or {}).items():
        aliases = [a for a in ep.get("Aliases") or [] if a != container["Id"][:12]]
        out[net] = {k: v for k, v in {"Aliases": aliases or None, "IPAMConfig": ep.get("IPAMConfig"),
                                      "Links": ep.get("Links"), "DriverOpts": ep.get("DriverOpts")}.items() if v}
    return out


def recreate(docker, container, image_ref, labels, keep_old_as=None):
    """Replace `container` with one like it on image_ref, with these labels changed (None: removed). The old one is stopped and
    renamed to keep_old_as (kept for its logs) or removed. If the new one can't start, the old one is put back."""
    name = container["Name"].lstrip("/")
    img = docker.image(container["Image"]) or {}
    body = create_config(container, img.get("Config"))
    body["Image"] = image_ref
    body["Labels"].update({k: v for k, v in labels.items() if v is not None})
    body["HostConfig"] = container["HostConfig"]
    nets = endpoints(container)
    first = next(iter(nets), None)
    if first:
        body["NetworkingConfig"] = {"EndpointsConfig": {first: nets[first]}}
    for k, v in labels.items():
        if v is None:
            body["Labels"].pop(k, None)
    docker.stop(container["Id"])
    old_name = keep_old_as or f"{name}-replaced"
    old = docker.container(old_name)
    if old:
        docker.remove_container(old["Id"])
    docker.rename(container["Id"], old_name)
    new_id = None
    try:
        new_id = docker.create(name, body)
        for net, ep in nets.items():
            if net != first:
                docker.connect(net, new_id, ep)
        docker.start(new_id)
    except Exception:
        if new_id:
            try:
                docker.remove_container(new_id)
            except Exception:
                pass
        docker.rename(container["Id"], name)
        docker.start(container["Id"])
        raise
    if not keep_old_as:
        docker.remove_container(container["Id"])
    return new_id


# ---------- the guard ----------
def health(container):
    """'healthy' / 'starting' / 'unhealthy' / the container state when not running ('restarting', 'exited', …)."""
    st = container.get("State") or {}
    if st.get("Status") != "running" or st.get("Restarting"):
        return st.get("Status") or "unknown"
    return (st.get("Health") or {}).get("Status") or "healthy"  # no health check: running is healthy


class Guard:
    def __init__(self, docker, clock=time.time, state_path="/state/state.json", target="homecontrol",
                 rollback_after=150.0, check_every=300.0, retry_after=300.0, enabled=True, log=log):
        self.docker, self.clock, self.state_path, self.target = docker, clock, state_path, target
        self.rollback_after, self.check_every, self.retry_after = rollback_after, check_every, retry_after
        self.enabled, self.log = enabled, log
        self.state = self._load()
        self._said = None

    # state: good {id, digest, ref}, watch {id, since}, paused {bad_id, bad_digest, ref, label, at, seen}, retry_at
    def _load(self):
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=1, sort_keys=True)
        os.replace(tmp, self.state_path)

    def _say_once(self, msg):
        if msg != self._said:
            self._said = msg
            self.log(msg)

    def _digest(self, image_id, repo):
        img = self.docker.image(image_id) or {}
        return next((d for d in img.get("RepoDigests") or [] if d.startswith(repo + "@")), None)

    def step(self):
        """One look at the app container; returns what it did: idle / good / waiting / rollback / resumed / …"""
        now = self.clock()
        c = self.docker.container(self.target)
        if c is None:
            self._say_once(f"{self.target}: no such container (being recreated?)")
            return "missing"
        s = self.state
        ref = c["Config"]["Image"]
        repo = split_ref(ref)[0]
        img, h = c["Image"], health(c)
        paused = s.get("paused")
        if paused and (c["Config"].get("Labels") or {}).get(WT_LABEL) != "false":
            self.log(f"{self.target} was recreated by hand: updates no longer paused")
            s.pop("paused")
            paused = None
            self._save()
        good = s.get("good")
        result = "idle"
        if h == "healthy":
            s.pop("watch", None)
            if not good or good["id"] != img:
                s["good"] = {"id": img, "digest": self._digest(img, repo), "ref": ref}
                self.log(f"{self.target} healthy on {short(img)}: now the known-good image")
                self._prune(repo, img)
                result = "good"
            self._save()
        elif not good or good["id"] == img:
            self._say_once(f"{self.target} is {h} on {short(img)}" + (", the known-good image: not rolling back" if good else
                                                                      ", and no known-good image to roll back to"))
        else:
            w = s.get("watch")
            if not w or w["id"] != img:
                s["watch"] = w = {"id": img, "since": now}
                self.log(f"{self.target} is {h} on new image {short(img)}: rolling back if not healthy within {self.rollback_after:.0f}s")
                self._save()
            result = "waiting"
            if now - w["since"] >= self.rollback_after and now >= s.get("retry_at", 0):
                if not self.enabled:
                    self._say_once(f"{self.target} unhealthy for {now - w['since']:.0f}s on {short(img)}; ROLLBACK_ENABLED=false, leaving it")
                else:
                    result = self._rollback(c, ref, repo, good, now)
        if s.get("paused") and result not in ("rollback", "failed"):
            result = self._maybe_resume(now) or result
        return result

    def _rollback(self, c, ref, repo, good, now):
        s = self.state
        bad = c["Image"]
        try:
            if not self.docker.image(good["id"]):
                if not good.get("digest"):
                    raise DockerError(404, f"known-good image {short(good['id'])} is gone and has no registry digest")
                self.log(f"pulling the known-good image {good['digest']}")
                self.docker.pull(good["digest"])
            self.docker.tag(good["id"], repo, "rollback")
            label = (c["Config"].get("Labels") or {}).get(WT_LABEL)
            bad_digest = self._digest(bad, repo)
            labels = {WT_LABEL: "false", FROM_LABEL: bad_digest or bad, AT_LABEL: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}
            recreate(self.docker, c, f"{repo}:rollback", {**labels, **compose_image(c, good["id"])}, keep_old_as=f"{self.target}-failed")
        except Exception as e:
            s["retry_at"] = now + self.retry_after
            self._save()
            self.log(f"ROLLBACK FAILED ({e}); trying again in {self.retry_after:.0f}s")
            return "failed"
        s.pop("watch", None)
        s.pop("retry_at", None)
        s["paused"] = {"bad_id": bad, "bad_digest": bad_digest, "ref": ref, "label": label, "at": now, "checked": now}
        self._save()
        self.log(f"ROLLED BACK {self.target}: {short(bad)} was not healthy for {self.rollback_after:.0f}s; now on "
                 f"{short(good['id'])} ({repo}:rollback). Updates paused until a newer {ref} is published. "
                 f"The failed container is kept as {self.target}-failed.")
        return "rollback"

    def _maybe_resume(self, now):
        p = self.state["paused"]
        if now - p.get("checked", 0) < self.check_every:
            return None
        p["checked"] = now
        self._save()
        try:
            digest = self.docker.remote_digest(p["ref"])
            if digest in (p.get("seen"), (p.get("bad_digest") or "").partition("@")[2]):
                return None
            self.docker.pull(p["ref"])
            new = (self.docker.image(p["ref"]) or {}).get("Id")
            if not new or new in (p["bad_id"], self.state.get("good", {}).get("id")):
                p["seen"] = digest  # the same image under another digest: still paused
                self._save()
                return None
            c = self.docker.container(self.target)
            if c is None:
                return None
            recreate(self.docker, c, p["ref"], {WT_LABEL: p.get("label") or "true", FROM_LABEL: None, AT_LABEL: None,
                                                **compose_image(c, new)})
        except Exception as e:
            self.log(f"checking for a newer {p['ref']} failed: {e}")
            return None
        self.state.pop("paused")
        self._save()
        # :rollback stays on the known-good image until the new one is healthy (then _prune removes it).
        self.log(f"a newer {p['ref']} was published ({short(new)}): updating to it, updates resumed")
        return "resumed"

    def _prune(self, repo, keep):
        """Remove the app's other images now that `keep` is healthy. Never the guard's own image; Docker itself
        refuses images a container (e.g. <name>-failed) still uses, which just stay."""
        try:
            images = self.docker.images()
        except Exception as e:
            self.log(f"listing images: {e}")
            return
        for im in images:
            names = (im.get("RepoTags") or []) + (im.get("RepoDigests") or [])
            if im["Id"] == keep or any(n.startswith(repo + ":guard") for n in names) or \
                    not any(n.startswith(repo + ":") or n.startswith(repo + "@") for n in names):
                continue
            try:
                tags = im.get("RepoTags") or []
                for t in tags[1:]:  # untag first: removing a multi-tagged image by id needs force
                    self.docker.remove_image(t)
                self.docker.remove_image(im["Id"])
                self.log(f"removed old image {short(im['Id'])}")
            except Exception as e:
                self.log(f"kept old image {short(im['Id'])}: {e}")


def compose_image(container, image_id):
    return {COMPOSE_IMAGE_LABEL: image_id} if COMPOSE_IMAGE_LABEL in (container["Config"].get("Labels") or {}) else {}


def short(image_id):
    return (image_id or "?").split(":")[-1][:12]


def main(argv):
    env = os.environ.get
    docker = DockerAPI(env("DOCKER_SOCKET", "/var/run/docker.sock"), env("DOCKER_CONFIG_PATH", "/config.json"))
    state_path = env("STATE_PATH", "/state/state.json")
    if argv[1:] == ["status"]:
        try:
            print(open(state_path).read())
        except OSError:
            print("{}")
        return 0
    guard = Guard(docker, state_path=state_path, target=env("GUARD_TARGET", "homecontrol"),
                  rollback_after=float(env("ROLLBACK_AFTER", "150")), check_every=float(env("CHECK_EVERY", "300")),
                  enabled=env("ROLLBACK_ENABLED", "true").lower() not in ("0", "false", "no", "off"))
    poll = float(env("POLL", "10"))
    log(f"watching {guard.target} every {poll:.0f}s; rollback after {guard.rollback_after:.0f}s unhealthy"
        + ("" if guard.enabled else " (ROLLBACK_ENABLED=false: watching only)"))
    while True:
        try:
            guard.step()
        except Exception as e:  # the Docker daemon restarting etc.: keep going
            log(f"error: {e}")
        time.sleep(poll)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
