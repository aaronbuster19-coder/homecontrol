"""Rollback guard (ops/rollback_guard.py): a fake Docker and an injected clock, no real Docker socket anywhere.
The last tests run the real Engine API client against a fake Docker API served on a unix socket in a temp dir."""
import base64
import http.server
import json
import socketserver
import threading

import pytest

from ops import rollback_guard as rg
from ops.rollback_guard import AT_LABEL, FROM_LABEL, WT_LABEL, DockerError, Guard

REPO = "ghcr.io/aaronbuster19-coder/homecontrol"
LATEST = REPO + ":latest"
OLD, NEW, NEWER = "sha256:" + "a" * 64, "sha256:" + "b" * 64, "sha256:" + "c" * 64
IMAGE_ENV = {OLD: ["PATH=/usr/bin", "PY=3.12"], NEW: ["PATH=/usr/bin", "PY=3.13"], NEWER: ["PATH=/usr/bin", "PY=3.13"]}


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class FakeDocker:
    """Just enough of the Engine API's behaviour: containers by name/id, images by id/tag, digests in a registry."""

    def __init__(self):
        self.images_ = {}      # id -> {"RepoTags", "RepoDigests", "Config"}
        self.containers = {}   # id -> inspect dict
        self.registry = {}     # ref -> (digest, image id)
        self.pushed = {}       # digest -> image id: everything ever published stays pullable by digest
        self.ops, self.n = [], 0
        self.fail_create = self.fail_pull = False

    # helpers for the tests
    def add_image(self, iid, tags=(), digest=None):
        if digest:
            self.pushed[digest] = iid
        self.images_[iid] = {"Id": iid, "RepoTags": list(tags), "RepoDigests": [f"{REPO}@{digest}"] if digest else [],
                             "Config": {"Env": IMAGE_ENV[iid], "Cmd": ["uvicorn"], "Labels": {"org.image": "x"},
                                        "Healthcheck": {"Test": ["CMD", iid[7:9]]}}}
        for t in tags:
            for other in self.images_.values():
                if other["Id"] != iid and t in other["RepoTags"]:
                    other["RepoTags"].remove(t)

    def run(self, name, iid, ref=LATEST, health="starting", status="running", label="true"):
        self.n += 1
        cid = f"{self.n:064x}"
        img = self.images_[iid]["Config"]
        self.containers[cid] = {
            "Id": cid, "Name": "/" + name, "Image": iid,
            "State": {"Status": status, "Restarting": False, "Health": {"Status": health}},
            "Config": {"Image": ref, "Hostname": cid[:12], "Env": img["Env"] + ["HA_URL=http://ha:8123", "DB_PATH=/data/layout.db"],
                       "Cmd": img["Cmd"], "Healthcheck": img["Healthcheck"],
                       "Labels": {**img["Labels"], WT_LABEL: label, "com.docker.compose.project": "homecontrol",
                                  "com.docker.compose.image": iid}},
            "HostConfig": {"Binds": ["homecontrol-data:/data"], "PortBindings": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8078"}]},
                           "RestartPolicy": {"Name": "unless-stopped"}},
            "NetworkSettings": {"Networks": {"homecontrol_default": {"Aliases": ["homecontrol", cid[:12]], "IPAddress": "172.18.0.2"}}},
        }
        return cid

    def by_name(self, name):
        return next((c for c in self.containers.values() if c["Name"] == "/" + name), None)

    def set_health(self, name, health, status="running"):
        c = self.by_name(name)
        c["State"].update(Status=status, Health={"Status": health})

    def _img(self, ref):
        if ref in self.images_:
            return self.images_[ref]
        return next((i for i in self.images_.values() if ref in i["RepoTags"] or ref in i["RepoDigests"]), None)

    # the DockerAPI surface
    def container(self, name):
        c = self.containers.get(name) or self.by_name(name)
        return json.loads(json.dumps(c)) if c else None

    def image(self, ref):
        i = self._img(ref)
        return json.loads(json.dumps(i)) if i else None

    def images(self):
        return [{k: v for k, v in i.items() if k != "Config"} for i in self.images_.values()]

    def tag(self, iid, repo, tag):
        self.ops.append(("tag", iid, f"{repo}:{tag}"))
        self.add_image(iid, self.images_[iid]["RepoTags"] + [f"{repo}:{tag}"],
                       (self.images_[iid]["RepoDigests"] or ["@"])[0].partition("@")[2] or None)

    def remove_image(self, ref):
        i = self._img(ref)
        if i is None:
            raise DockerError(404, "no such image")
        if ref in i["RepoTags"] and len(i["RepoTags"]) > 1:
            i["RepoTags"].remove(ref)
            self.ops.append(("untag", ref))
            return
        if any(c["Image"] == i["Id"] for c in self.containers.values()):
            raise DockerError(409, "image is being used by a container")
        if len(i["RepoTags"]) > 1:
            raise DockerError(409, "image is referenced in multiple repositories")
        del self.images_[i["Id"]]
        self.ops.append(("rmi", i["Id"]))

    def pull(self, ref):
        self.ops.append(("pull", ref))
        if self.fail_pull:
            raise DockerError(500, "registry down")
        repo, tag = rg.split_ref(ref)
        if tag.startswith("sha256:"):
            iid = self.pushed[tag]
            self.add_image(iid, [], tag)
        else:
            digest, iid = self.registry[ref]
            self.add_image(iid, [ref], digest)

    def remote_digest(self, ref):
        self.ops.append(("digest", ref))
        return self.registry[ref][0]

    def stop(self, cid, timeout=10):
        self.ops.append(("stop", cid))
        self.containers[cid]["State"].update(Status="exited", Health={"Status": "unhealthy"})

    def start(self, cid):
        self.ops.append(("start", cid))
        self.containers[cid]["State"].update(Status="running", Health={"Status": "starting"})

    def rename(self, cid, name):
        if self.by_name(name):
            raise DockerError(409, "name in use")
        self.ops.append(("rename", cid, name))
        self.containers[cid]["Name"] = "/" + name

    def remove_container(self, cid):
        self.ops.append(("rm", cid))
        del self.containers[cid]

    def create(self, name, body):
        if self.fail_create:
            raise DockerError(500, "create failed")
        if self.by_name(name):
            raise DockerError(409, "name in use")
        img = self._img(body["Image"])
        self.n += 1
        cid = f"{self.n:064x}"
        cfg = img["Config"]
        self.created = body
        self.containers[cid] = {
            "Id": cid, "Name": "/" + name, "Image": img["Id"], "State": {"Status": "created"},
            "Config": {**body, "Env": cfg["Env"] + body["Env"], "Cmd": body.get("Cmd", cfg["Cmd"]),
                       "Healthcheck": body.get("Healthcheck", cfg["Healthcheck"]), "Labels": {**cfg["Labels"], **body["Labels"]}},
            "HostConfig": body["HostConfig"],
            "NetworkSettings": {"Networks": {n: {**ep, "Aliases": (ep.get("Aliases") or []) + [cid[:12]]}
                                             for n, ep in body["NetworkingConfig"]["EndpointsConfig"].items()}},
        }
        self.ops.append(("create", name, body["Image"]))
        return cid

    def connect(self, network, cid, endpoint):
        self.ops.append(("connect", network, cid))
        self.containers[cid]["NetworkSettings"]["Networks"][network] = endpoint


@pytest.fixture
def env(tmp_path):
    d, clock, logs = FakeDocker(), Clock(), []
    d.add_image(OLD, [LATEST], "sha256:" + "1" * 64)
    d.registry[LATEST] = ("sha256:" + "1" * 64, OLD)
    d.run("homecontrol", OLD, health="healthy")

    def guard(**kw):
        return Guard(d, clock=clock, state_path=str(tmp_path / "state.json"), log=logs.append, **kw)
    return d, clock, guard, logs


def publish(d, iid, digest_char):
    """CI pushed a new :latest; Watchtower pulled it and recreated the app (the old image loses its tag)."""
    digest = "sha256:" + digest_char * 64
    d.registry[LATEST] = (digest, iid)
    d.add_image(iid, [LATEST], digest)
    old = d.by_name("homecontrol")
    del d.containers[old["Id"]]
    d.run("homecontrol", iid)


def run_for(guard, clock, seconds, step=10):
    """A step now, then every `step` s, for `seconds`: run_for(160) looks at 0, 10, … 150 s; the clock ends at 160 s."""
    results = []
    for _ in range(int(seconds / step)):
        results.append(guard.step())
        clock.t += step
    return results


# ---------------------------------------------------------------------------------------------------------------------

def test_healthy_app_becomes_known_good(env):
    d, clock, guard, logs = env
    g = guard()
    assert g.step() == "good"
    assert g.state["good"] == {"id": OLD, "digest": f"{REPO}@sha256:" + "1" * 64, "ref": LATEST}
    assert g.step() == "idle"
    # state survives a restart of the guard
    assert guard().state["good"]["id"] == OLD


def test_good_update_is_kept_and_old_image_pruned(env):
    d, clock, guard, logs = env
    g = guard()
    g.step()
    publish(d, NEW, "2")
    assert run_for(g, clock, 60) == ["waiting"] * 6
    d.set_health("homecontrol", "healthy")
    clock.t += 10
    assert g.step() == "good"
    assert g.state["good"]["id"] == NEW and "watch" not in g.state
    assert ("rmi", OLD) in d.ops and OLD not in d.images_
    assert not [o for o in d.ops if o[0] in ("stop", "create")]


def test_unhealthy_update_rolls_back_after_150s(env):
    d, clock, guard, logs = env
    g = guard()
    g.step()
    publish(d, NEW, "2")
    bad = d.by_name("homecontrol")
    d.set_health("homecontrol", "unhealthy")
    assert run_for(g, clock, 150) == ["waiting"] * 15   # 0 … 140 s after first seen unhealthy: not yet
    assert g.step() == "rollback"                        # 150 s
    new = d.by_name("homecontrol")
    assert new["Image"] == OLD and new["Config"]["Image"] == f"{REPO}:rollback"
    labels = new["Config"]["Labels"]
    assert labels[WT_LABEL] == "false" and labels[FROM_LABEL] == f"{REPO}@sha256:" + "2" * 64 and labels[AT_LABEL]
    assert labels["com.docker.compose.project"] == "homecontrol"
    assert labels["com.docker.compose.image"] == OLD   # so Dockge → Update sees it differs from :latest and recreates
    # The failed image's own defaults don't leak into the rolled-back container; the stack's settings do carry over.
    assert new["Config"]["Env"] == IMAGE_ENV[OLD] + ["HA_URL=http://ha:8123", "DB_PATH=/data/layout.db"]
    assert new["Config"]["Healthcheck"] == {"Test": ["CMD", "aa"]}
    assert "Hostname" not in d.created and d.created["HostConfig"]["Binds"] == ["homecontrol-data:/data"]
    assert d.created["NetworkingConfig"] == {"EndpointsConfig": {"homecontrol_default": {"Aliases": ["homecontrol"]}}}
    # The failed one is kept, stopped, for its logs.
    failed = d.by_name("homecontrol-failed")
    assert failed["Id"] == bad["Id"] and failed["State"]["Status"] == "exited"
    assert g.state["paused"]["bad_id"] == NEW and any("ROLLED BACK" in m for m in logs)
    # Healthy again on the old image: no second rollback, nothing pruned that the failed container still uses.
    d.set_health("homecontrol", "healthy")
    assert set(run_for(g, clock, 60)) == {"idle"}
    assert NEW in d.images_


def test_crash_loop_counts_as_unhealthy_and_starting_never_resets_the_timer(env):
    d, clock, guard, logs = env
    g = guard()
    g.step()
    publish(d, NEW, "2")
    states = [("restarting", "starting"), ("running", "starting"), ("exited", "unhealthy")] * 6
    results = []
    for status, h in states:
        d.set_health("homecontrol", h, status)
        results.append(g.step())
        clock.t += 10
    assert results[:15] == ["waiting"] * 15 and results[15] == "rollback"


def test_healthy_in_time_is_not_rolled_back(env):
    d, clock, guard, logs = env
    g = guard()
    g.step()
    publish(d, NEW, "2")
    run_for(g, clock, 140)
    d.set_health("homecontrol", "healthy")
    clock.t += 10
    assert g.step() == "good"
    # Later trouble on an image that was healthy is not the image's fault: no rollback.
    d.set_health("homecontrol", "unhealthy")
    assert set(run_for(g, clock, 600)) == {"idle"}
    assert d.by_name("homecontrol")["Image"] == NEW


def test_nothing_to_roll_back_to(env):
    d, clock, guard, logs = env
    d.set_health("homecontrol", "unhealthy")
    g = guard()
    assert set(run_for(g, clock, 600)) == {"idle"}
    assert not [o for o in d.ops if o[0] == "stop"]
    assert logs == ["homecontrol is unhealthy on aaaaaaaaaaaa, and no known-good image to roll back to"]  # said once


def test_new_image_watch_restarts_when_watchtower_brings_another(env):
    d, clock, guard, logs = env
    g = guard()
    g.step()
    publish(d, NEW, "2")
    d.set_health("homecontrol", "unhealthy")
    run_for(g, clock, 100)
    d.add_image(NEWER, [], None)
    publish(d, NEWER, "3")
    d.set_health("homecontrol", "unhealthy")
    assert run_for(g, clock, 150) == ["waiting"] * 15
    assert g.step() == "rollback"


def test_disabled_only_logs(env):
    d, clock, guard, logs = env
    g = guard(enabled=False)
    g.step()
    publish(d, NEW, "2")
    d.set_health("homecontrol", "unhealthy")
    assert set(run_for(g, clock, 600)) == {"waiting"}
    assert d.by_name("homecontrol")["Image"] == NEW and not [o for o in d.ops if o[0] == "stop"]
    assert any("ROLLBACK_ENABLED=false" in m for m in logs)


def test_paused_until_a_newer_image_then_resumes(env):
    d, clock, guard, logs = env
    g = guard(check_every=300)
    g.step()
    publish(d, NEW, "2")
    d.set_health("homecontrol", "unhealthy")
    run_for(g, clock, 160)
    assert g.state.get("paused")
    d.set_health("homecontrol", "healthy")
    # Nothing new published: checks the registry every 300 s, pulls nothing.
    run_for(g, clock, 900)
    assert [o for o in d.ops if o[0] == "digest"] == [("digest", LATEST)] * 3
    assert not [o for o in d.ops if o[0] == "pull"]
    # CI publishes a fix.
    d.registry[LATEST] = ("sha256:" + "3" * 64, NEWER)
    results = run_for(g, clock, 300)
    assert results[-1] == "resumed" and "paused" not in g.state
    app = d.by_name("homecontrol")
    assert app["Image"] == NEWER and app["Config"]["Image"] == LATEST
    labels = app["Config"]["Labels"]
    assert labels[WT_LABEL] == "true" and FROM_LABEL not in labels and AT_LABEL not in labels
    assert labels["com.docker.compose.image"] == NEWER
    # The rolled-back container is gone; :rollback still marks the known-good image until the fix proves healthy.
    assert d.by_name("homecontrol-replaced") is None and f"{REPO}:rollback" in d.images_[OLD]["RepoTags"]
    d.set_health("homecontrol", "healthy")
    clock.t += 10
    assert g.step() == "good" and OLD not in d.images_ and g.state["good"]["id"] == NEWER


def test_fix_that_fails_too_rolls_back_again(env):
    d, clock, guard, logs = env
    g = guard(check_every=300)
    g.step()
    publish(d, NEW, "2")
    d.set_health("homecontrol", "unhealthy")
    run_for(g, clock, 160)
    d.set_health("homecontrol", "healthy")
    d.registry[LATEST] = ("sha256:" + "3" * 64, NEWER)
    assert run_for(g, clock, 300)[-1] == "resumed"
    d.set_health("homecontrol", "unhealthy")
    assert run_for(g, clock, 160)[-1] == "rollback"
    assert d.by_name("homecontrol")["Image"] == OLD and g.state["paused"]["bad_id"] == NEWER


def test_same_image_under_new_digest_stays_paused(env):
    d, clock, guard, logs = env
    g = guard(check_every=300)
    g.step()
    publish(d, NEW, "2")
    d.set_health("homecontrol", "unhealthy")
    run_for(g, clock, 160)
    d.set_health("homecontrol", "healthy")
    d.registry[LATEST] = ("sha256:" + "9" * 64, NEW)   # re-pushed, same image
    run_for(g, clock, 900)
    assert d.by_name("homecontrol")["Image"] == OLD and g.state["paused"]["seen"] == "sha256:" + "9" * 64
    assert len([o for o in d.ops if o[0] == "pull"]) == 1   # pulled once, then remembered


def test_recreated_by_hand_ends_the_pause(env):
    d, clock, guard, logs = env
    g = guard()
    g.step()
    publish(d, NEW, "2")
    d.set_health("homecontrol", "unhealthy")
    run_for(g, clock, 160)
    # Dockge → Update: the compose file's container again (label true, :latest).
    c = d.by_name("homecontrol")
    del d.containers[c["Id"]]
    d.run("homecontrol", NEW, health="starting")
    clock.t += 10
    assert g.step() == "waiting" and "paused" not in g.state
    assert any("recreated by hand" in m for m in logs)


def test_known_good_image_removed_is_pulled_by_digest(env):
    d, clock, guard, logs = env
    g = guard()
    g.step()
    publish(d, NEW, "2")
    d.images_.pop(OLD)   # someone ran docker image prune -a
    d.set_health("homecontrol", "unhealthy")
    assert run_for(g, clock, 160)[-1] == "rollback"
    assert ("pull", f"{REPO}@sha256:" + "1" * 64) in d.ops and d.by_name("homecontrol")["Image"] == OLD


def test_failed_rollback_puts_the_app_back_and_retries_later(env):
    d, clock, guard, logs = env
    g = guard(retry_after=300)
    g.step()
    publish(d, NEW, "2")
    bad = d.by_name("homecontrol")["Id"]
    d.set_health("homecontrol", "unhealthy")
    d.fail_create = True
    assert run_for(g, clock, 160)[-1] == "failed"
    app = d.by_name("homecontrol")
    assert app["Id"] == bad and app["State"]["Status"] == "running"   # renamed back and started
    assert any("ROLLBACK FAILED" in m for m in logs)
    d.fail_create = False
    assert run_for(g, clock, 290) == ["waiting"] * 29   # retried 300 s after the failed attempt
    assert g.step() == "rollback"


def test_known_good_gone_and_registry_down(env):
    d, clock, guard, logs = env
    g = guard()
    g.step()
    publish(d, NEW, "2")
    d.images_.pop(OLD)
    d.fail_pull = True
    d.set_health("homecontrol", "unhealthy")
    assert run_for(g, clock, 160)[-1] == "failed"
    assert d.by_name("homecontrol")["Image"] == NEW and not [o for o in d.ops if o[0] == "stop"]


def test_missing_container_waits(env):
    d, clock, guard, logs = env
    g = guard()
    d.containers.clear()
    assert g.step() == "missing" and g.step() == "missing" and len(logs) == 1


def test_guard_image_never_pruned(env):
    d, clock, guard, logs = env
    d.images_["sha256:" + "f" * 64] = {"Id": "sha256:" + "f" * 64, "RepoTags": [REPO + ":guard"], "RepoDigests": [], "Config": {}}
    d.images_["sha256:" + "e" * 64] = {"Id": "sha256:" + "e" * 64, "RepoTags": ["python:3.13-slim"], "RepoDigests": [], "Config": {}}
    g = guard()
    g.step()
    publish(d, NEW, "2")
    d.set_health("homecontrol", "healthy")
    clock.t += 10
    g.step()
    assert set(d.images_) == {NEW, "sha256:" + "f" * 64, "sha256:" + "e" * 64}


def test_split_ref_and_auth(tmp_path):
    assert rg.split_ref(LATEST) == (REPO, "latest")
    assert rg.split_ref("localhost:5000/app") == ("localhost:5000/app", "latest")
    assert rg.split_ref(f"{REPO}@sha256:abc") == (REPO, "sha256:abc")
    cfg = tmp_path / "config.json"
    assert rg.registry_auth(REPO, str(cfg)) is None
    cfg.write_text(json.dumps({"auths": {"ghcr.io": {"auth": base64.b64encode(b"aaron:tok").decode()}}}))
    got = json.loads(base64.urlsafe_b64decode(rg.registry_auth(REPO, str(cfg))))
    assert got == {"username": "aaron", "password": "tok", "serveraddress": "ghcr.io"}
    assert rg.registry_auth("library/python", str(cfg)) is None


# ---------- the real Engine API client against a fake Docker on a unix socket ----------
class FakeEngine(http.server.BaseHTTPRequestHandler):
    calls = []

    def _reply(self, status, body=b""):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        FakeEngine.calls.append((self.command, self.path, body, self.headers.get("X-Registry-Auth")))
        p = self.path
        if p == "/containers/homecontrol/json":
            return self._reply(200, {"Id": "c1", "Name": "/homecontrol"})
        if p.startswith("/containers/") and p.endswith("/json"):
            return self._reply(404, {"message": "No such container"})
        if p.startswith("/images/create"):
            if "fromImage=bad" in p:
                return self._reply(200, b'{"status":"Pulling"}\n{"error":"manifest unknown"}\n')
            return self._reply(200, b'{"status":"Pulling"}\n{"status":"Done"}\n')
        if p.startswith("/distribution/"):
            return self._reply(200, {"Descriptor": {"digest": "sha256:123"}})
        if p == "/containers/create?name=homecontrol":
            return self._reply(201, {"Id": "c2"})
        if p.startswith("/containers/c1/stop"):
            return self._reply(304)
        if p.startswith("/images/gone"):
            return self._reply(409, {"message": "conflict: image is being used"})
        return self._reply(204)

    do_GET = do_POST = do_DELETE = _handle

    def log_message(self, *a):
        pass


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        req, _ = super().get_request()
        return req, ("local", 0)


@pytest.fixture
def engine(tmp_path):
    path = str(tmp_path / "docker.sock")
    srv = UnixServer(path, FakeEngine)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    FakeEngine.calls = []
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"auths": {"ghcr.io": {"auth": base64.b64encode(b"u:p").decode()}}}))
    yield rg.DockerAPI(path, str(cfg), timeout=5)
    srv.shutdown()
    srv.server_close()


def test_engine_client_over_a_unix_socket(engine):
    assert engine.container("homecontrol")["Id"] == "c1"
    assert engine.container("nope") is None
    assert engine.create("homecontrol", {"Image": "x"}) == "c2"
    engine.stop("c1")   # 304 (already stopped) is fine
    engine.pull(LATEST)
    with pytest.raises(DockerError, match="manifest unknown"):
        engine.pull("bad:1")
    assert engine.remote_digest(LATEST) == "sha256:123"
    with pytest.raises(DockerError) as e:
        engine.remove_image("gone")
    assert e.value.status == 409
    pull = next(c for c in FakeEngine.calls if c[1].startswith("/images/create?fromImage=ghcr.io"))
    assert pull[1] == "/images/create?fromImage=ghcr.io%2Faaronbuster19-coder%2Fhomecontrol&tag=latest"
    assert json.loads(base64.urlsafe_b64decode(pull[3]))["username"] == "u"
    assert ("POST", "/containers/create?name=homecontrol", {"Image": "x"}, None) in FakeEngine.calls
