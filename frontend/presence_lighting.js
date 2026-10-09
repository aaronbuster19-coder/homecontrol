"use strict";
// Presence lighting (⋯ → Presence lighting…): a door opening after dark turns a room's lights on, and they go off after
// a quiet spell with no door activity. Run by the server (backend/presence_lighting.py); this is its settings sheet.
// Data: GET /api/presence-lighting; PUT /api/presence-lighting/settings; PUT /api/presence-lighting/rooms/{id}.
// Admins change it; members see it read-only; guests don't get the menu entry (and the server answers 403).
const pl = { data: null, err: null, busy: false, timer: null, open: new Set(), log: false };
const ple = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const plHm = (ms) => new Date(ms).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const plWhen = (ms) => {
  const d = new Date(ms), now = new Date(pl.data?.now ?? Date.now());
  if (d.toDateString() === now.toDateString()) return plHm(ms);
  const tmr = new Date(now); tmr.setDate(now.getDate() + 1);
  return `${d.toDateString() === tmr.toDateString() ? "tomorrow" : d.toLocaleDateString("en-GB", { weekday: "short" })} ${plHm(ms)}`;
};
const plAdmin = () => (document.documentElement.dataset.role || "admin") === "admin";

function plSheet() {
  let s = $("plSheet");
  if (s) return s;
  s = ple("div", "sheet pl-sheet"); s.id = "plSheet"; s.hidden = true;
  s.setAttribute("role", "dialog"); s.setAttribute("aria-label", "Presence lighting");
  const body = ple("div", "sheet-body");
  const x = ple("button", "close", "×"); x.type = "button"; x.setAttribute("aria-label", "Close"); x.onclick = closePresenceLighting;
  const c = ple("div"); c.id = "plContent";
  body.append(x, c); s.append(body); document.body.append(s);
  s.addEventListener("click", (e) => { if (e.target === s) closePresenceLighting(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !s.hidden && !document.querySelector("dialog[open]")) closePresenceLighting(); });
  return s;
}
function openPresenceLighting() {
  plSheet().hidden = false; pl.msg = null; plRender(); plLoad();
  clearInterval(pl.timer); pl.timer = setInterval(() => { if (!document.hidden) plLoad(); }, 15e3);
}
function closePresenceLighting() { const s = $("plSheet"); if (s) s.hidden = true; clearInterval(pl.timer); pl.timer = null; }

async function plLoad() {
  if (pl.busy) return;
  pl.busy = true;
  try { pl.data = await api("/api/presence-lighting"); pl.err = null; } catch (e) { pl.err = e.message; }
  finally { pl.busy = false; }
  plRender();
}
async function plSend(path, body, what) {
  try { pl.data = await api(path, { method: "PUT", body: JSON.stringify(body) }); pl.msg = { text: what, warn: false }; }
  catch (e) { pl.msg = { text: `Couldn't save: ${e.message}`, warn: true }; }
  plRender();
}

// ---------- render ----------
function plDarkText(d) {
  const src = d.dark.source === "ha" ? "Home Assistant's sun" : "sunset / sunrise for your location";
  if (d.mode === "away") return "Away — nothing switches on until you're home.";
  if (d.dark.dark) return `Dark now${d.dark.until ? ` until ${plWhen(d.dark.until)}` : ""} (${src}).`;
  return `Daylight — doors switch lights on from ${d.dark.until ? plWhen(d.dark.until) : "sunset"} (${src}).`;
}
function plPhase(r) {
  switch (r.phase) {
    case "on": return ["on", `Lights on — off at ${plHm(r.off_at)} if quiet`];
    case "paused": return ["paused", `Paused (switched by hand) until ${plHm(r.paused_until)} if quiet`];
    case "setup": return ["warn", r.sensors.length ? "Pick the lights" : "Pick a door sensor"];
    case "away": return ["", "Away"];
    case "ready": return ["ready", "Ready"];
    case "daylight": return ["", "Waiting for dark"];
    default: return null;
  }
}

function plRender() {
  const s = $("plSheet");
  if (!s || s.hidden) return;
  const c = $("plContent"), keep = s.querySelector(".sheet-body").scrollTop;
  c.replaceChildren(ple("h3", null, "Presence lighting"));
  const d = pl.data;
  if (!d) {
    c.append(ple("div", "hist-msg" + (pl.err ? " err" : " loading"), pl.err ? `Couldn't load: ${pl.err}` : "Loading…"));
    return;
  }
  c.append(ple("div", "sub", "When a door opens after dark, that room's lights come on. They go off once there's been no door " +
    "activity for a while. Switching a light by hand pauses it for that room. Never while Away."));
  const st = ple("p", "pl-status" + (d.dark.dark ? " dark" : "")); st.id = "plStatus"; st.textContent = plDarkText(d); c.append(st);

  const master = ple("label", "sched-master"), on = ple("input"); on.type = "checkbox"; on.id = "plOn"; on.checked = d.enabled;
  on.onchange = () => plSend("/api/presence-lighting/settings", { enabled: on.checked }, on.checked ? "Presence lighting on" : "Presence lighting off");
  master.append(on, ple("b", null, "Lights on when a door opens after dark")); c.append(master);
  const msg = ple("p", "hint pl-msg" + (pl.msg?.warn ? " warn" : "")); msg.id = "plMsg"; msg.setAttribute("role", "status");
  msg.textContent = pl.msg?.text || ""; c.append(msg);
  if (!plAdmin()) c.append(ple("p", "hint pl-role", "Only an admin can change these settings."));

  const ul = ple("ul", "pl-rooms" + (d.enabled ? "" : " off")); ul.id = "plRooms"; c.append(ul);
  if (!d.rooms.length) ul.append(ple("li", "hist-msg", "No rooms on the plan yet — draw them in Edit mode first."));
  for (const r of d.rooms) ul.append(plRoom(d, r));
  plLog(c, d);
  s.querySelector(".sheet-body").scrollTop = keep;
}

function plRoom(d, r) {
  const li = ple("li"); li.dataset.room = r.id;
  const head = ple("div", "pl-room-head");
  const lab = ple("label", "check"), cb = ple("input"); cb.type = "checkbox"; cb.checked = r.enabled; cb.dataset.enable = r.id;
  cb.setAttribute("aria-label", `Presence lighting in ${r.name}`);
  const path = `/api/presence-lighting/rooms/${encodeURIComponent(r.id)}`;
  cb.onchange = () => plSend(path, { enabled: cb.checked }, `${r.name}: ${cb.checked ? "on" : "off"}`);
  lab.append(cb, ple("span", "pl-name", r.name)); head.append(lab);
  const ph = plPhase(r);
  if (ph) { const p = ple("span", `pl-phase ${ph[0]}`, ph[1]); head.append(p); }
  li.append(head);
  if (!r.enabled) return li;

  const sub = ple("div", "pl-sub");
  const row = ple("label", "pl-row", "Off after ");
  const n = ple("input"); n.type = "number"; n.min = 1; n.max = 120; n.step = 1; n.inputMode = "numeric"; n.value = r.quiet_minutes;
  n.dataset.quiet = r.id; n.setAttribute("aria-label", `Quiet minutes for ${r.name}`);
  n.onchange = () => {
    const v = Math.round(Number(n.value));
    if (!(v >= 1 && v <= 120)) { pl.msg = { text: "Minutes must be 1–120.", warn: true }; n.value = r.quiet_minutes; plRender(); return; }
    plSend(path, { quiet_minutes: v }, `${r.name}: off after ${v} min`);
  };
  row.append(n, " min without door activity"); sub.append(row);

  const det = ple("details", "pl-fold"); det.open = pl.open.has(r.id); det.dataset.pick = r.id;
  det.addEventListener("toggle", () => { if (det.open) pl.open.add(r.id); else pl.open.delete(r.id); });
  const plural = (k, w) => `${k} ${w}${k === 1 ? "" : "s"}`;
  det.append(ple("summary", null, `${plural(r.sensors.length, "door")} · ${plural(r.lights.length, "light")}`));
  const group = (title, items, chosen, mark, key, auto) => {
    const g = ple("fieldset", "pl-pick"); g.append(ple("legend", null, title));
    if (!items.length) g.append(ple("div", "hint", key === "sensors" ? "No door / window sensors in Home Assistant." : "No lights."));
    for (const it of items) {
      const l = ple("label", "check"), x = ple("input"); x.type = "checkbox"; x.checked = chosen.includes(it.entity_id);
      x.dataset[key] = it.entity_id;
      x.onchange = () => {
        const ids = [...g.querySelectorAll(`input[data-${key}]`)].filter((y) => y.checked).map((y) => y.dataset[key]);
        plSend(path, { [key]: ids }, `${r.name}: ${key === "sensors" ? "doors" : "lights"} saved`);
      };
      l.append(x, ple("span", null, it.name));
      if (mark.includes(it.entity_id)) l.append(ple("span", "pl-tag", key === "sensors" ? "door of this room" : "in this room"));
      g.append(l);
    }
    if (!auto) {
      const b = ple("button", "linkish pl-reset", "Use the plan's defaults"); b.type = "button";
      b.onclick = () => plSend(path, { [key]: null }, `${r.name}: ${key === "sensors" ? "doors" : "lights"} from the plan`);
      g.append(b);
    }
    return g;
  };
  // the room's own sensors / lights first
  const order = (all, mine) => [...all.filter((x) => mine.includes(x.entity_id)), ...all.filter((x) => !mine.includes(x.entity_id))];
  det.append(group("Door sensors", order(d.all_sensors, r.suggested_sensors), r.sensors, r.suggested_sensors, "sensors", r.auto_sensors),
    group("Lights", order(d.all_lights, r.room_lights), r.lights, r.room_lights, "lights", r.auto_lights));
  sub.append(det);
  li.append(sub);
  return li;
}

function plLog(c, d) {
  const det = ple("details", "pl-fold pl-log"); det.open = pl.log; det.id = "plLog";
  det.addEventListener("toggle", () => { pl.log = det.open; });
  det.append(ple("summary", null, d.log.length ? `Recent (${Math.min(d.log.length, 12)})` : "Recent"));
  if (!d.log.length) det.append(ple("div", "hist-msg", "Nothing yet."));
  const words = { on: "lights on", off: "lights off", paused: "paused", resumed: "automatic again", failed: "failed",
    enabled: "enabled", disabled: "disabled", "switched on": "Presence lighting switched on", "switched off": "Presence lighting switched off" };
  for (const x of d.log.slice(0, 12)) {
    const r = ple("div", "sum-row");
    r.append(ple("span", null, `${x.room ? x.room + ": " : ""}${words[x.action] || x.action}${x.note ? ` — ${x.note}` : ""}`), ple("span", null, plWhen(x.at)));
    det.append(r);
  }
  c.append(det);
}

// ---------- wiring: ⋯ menu entry after Auto Away… ----------
(() => {
  const b = ple("button", null, "Presence lighting…"); b.id = "plBtn"; b.setAttribute("role", "menuitem");
  const anchor = $("autoAwayBtn") || $("awayBtn");
  if (anchor) anchor.after(b); else $("moreMenu").prepend(b);
  b.addEventListener("click", openPresenceLighting);  // modes.js closes the menu
})();
