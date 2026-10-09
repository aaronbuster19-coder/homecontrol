"use strict";
// Climate (⋯ → Climate…): smart preheat per room and damp / mould warnings, run by the server (backend/climate.py).
// Its own sheet (#climateSheet), built here, so device updates never rebuild it under an open <select>.
// Data: GET /api/climate; PUT /api/climate/settings; PUT /api/climate/rooms/{id}; POST /api/climate/rooms/{id}/learn.
// "/?climate" (the damp push tapped) opens it.
const cm = { data: null, err: null, busy: false, timer: null, open: { sensors: false, log: false } };
const cme = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const cmHm = (ms) => new Date(ms).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const cmWhen = (ms) => {
  const d = new Date(ms), now = new Date(cm.data?.now ?? Date.now());
  if (d.toDateString() === now.toDateString()) return cmHm(ms);
  const tmr = new Date(now); tmr.setDate(now.getDate() + 1);
  return `${d.toDateString() === tmr.toDateString() ? "tomorrow" : d.toLocaleDateString("en-GB", { weekday: "short" })} ${cmHm(ms)}`;
};
const cmDeg = (v) => `${Math.round(v * 10) / 10}°`;

function cmSheet() {
  let s = $("climateSheet");
  if (s) return s;
  s = cme("div", "sheet cm-sheet"); s.id = "climateSheet"; s.hidden = true;
  s.setAttribute("role", "dialog"); s.setAttribute("aria-label", "Climate");
  const body = cme("div", "sheet-body");
  const x = cme("button", "close", "×"); x.id = "climateClose"; x.setAttribute("aria-label", "Close"); x.onclick = closeClimate;
  const c = cme("div"); c.id = "climateContent";
  body.append(x, c); s.append(body); document.body.append(s);
  s.addEventListener("click", (e) => { if (e.target === s) closeClimate(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !s.hidden && !document.querySelector("dialog[open]")) closeClimate(); });
  return s;
}
function openClimate() {
  cmSheet().hidden = false; cmRender(); cmLoad();
  clearInterval(cm.timer); cm.timer = setInterval(() => { if (!document.hidden) cmLoad(); }, 60e3);
}
function closeClimate() { const s = $("climateSheet"); if (s) s.hidden = true; clearInterval(cm.timer); cm.timer = null; }

async function cmLoad() {
  if (cm.busy) return;
  cm.busy = true;
  try { cm.data = await api("/api/climate"); cm.err = null; } catch (e) { cm.err = e.message; }
  finally { cm.busy = false; }
  cmRender();
}
async function cmSend(path, body, what, method = "PUT") {
  try {
    cm.data = await api(path, { method, body: body == null ? undefined : JSON.stringify(body) });
    const l = cm.data.learn;
    setStatus(l?.error ? `${what} — couldn't learn from history: ${l.error}` : l ? `${what} — ${l.warmups} warm-up${l.warmups === 1 ? "" : "s"} found in the last 7 days` : what, !!l?.error);
  } catch (e) { setStatus(`Climate: ${e.message}`, true); }
  cmRender();
}

// ---------- render ----------
function cmRender() {
  const s = $("climateSheet");
  if (!s || s.hidden) return;
  const c = $("climateContent"), keep = s.querySelector(".sheet-body").scrollTop;
  c.replaceChildren(cme("h3", null, "Climate"));
  const d = cm.data;
  if (!d) {
    c.append(cme("div", "hist-msg" + (cm.err ? " err" : " loading"), cm.err ? `Couldn't load: ${cm.err}` : "Loading…"));
    return;
  }
  c.append(cme("div", "sub", "Warm rooms on time and catch damp early. Settings are shared by all devices."));
  cmPreheat(c, d);
  cmDamp(c, d);
  cmLog(c, d);
  s.querySelector(".sheet-body").scrollTop = keep;
}

function cmCheck(id, checked, text, onchange) {
  const lab = cme("label", "check"), cb = cme("input"); cb.type = "checkbox"; cb.id = id; cb.checked = checked;
  cb.onchange = () => onchange(cb.checked);
  lab.append(cb, cme("span", null, text)); return lab;
}
function cmNum(id, value, min, max, step, save) {
  const i = cme("input"); i.type = "number"; i.id = id; i.min = min; i.max = max; i.step = step; i.value = value;
  i.inputMode = step < 1 ? "decimal" : "numeric";
  i.onchange = () => {
    const v = Number(i.value);
    if (!(v >= min && v <= max)) { setStatus(`Must be ${min}–${max}`, true); i.value = value; return; }
    save(step < 1 ? v : Math.round(v));
  };
  return i;
}

function cmPreheat(c, d) {
  const s = d.settings, sec = cme("section", "cm-sec"); sec.id = "cmPreheatSec"; c.append(sec);
  sec.append(cme("h4", null, "Smart preheat"));
  sec.append(cmCheck("cmPreheat", s.preheat_enabled, "Start radiators early so a room is warm when its heating schedule begins",
    (on) => cmSend("/api/climate/settings", { preheat_enabled: on }, on ? "Smart preheat on" : "Smart preheat off")));
  const opts = cme("div", "auto-sub" + (s.preheat_enabled ? "" : " off"));
  const lead = cme("label", "cm-row", "Start at most ");
  const sel = cme("select"); sel.id = "cmLead"; sel.setAttribute("aria-label", "Earliest start");
  for (const m of [30, 60, 90, 120, 180, 240]) { const o = cme("option", null, m < 60 ? `${m} min` : `${m / 60} h`.replace(".5 h", "½ h")); o.value = m; sel.append(o); }
  if (![30, 60, 90, 120, 180, 240].includes(s.max_lead_min)) { const o = cme("option", null, `${s.max_lead_min} min`); o.value = s.max_lead_min; sel.append(o); }
  sel.value = String(s.max_lead_min);
  sel.onchange = () => cmSend("/api/climate/settings", { max_lead_min: Number(sel.value) }, "Preheat limit saved");
  lead.append(sel, " early");
  opts.append(lead, cme("div", "hint", "Never while Away or with a window open. The schedule still runs as usual."));
  if (d.mode === "away" && s.preheat_enabled) opts.append(cme("div", "hint cm-note", "Paused — you're Away."));
  sec.append(opts);

  const rooms = d.rooms.filter((r) => r.valves.length), none = d.rooms.filter((r) => !r.valves.length);
  if (!rooms.length) { sec.append(cme("div", "hist-msg", "No room has a radiator valve placed in it yet.")); return; }
  const ul = cme("ul", "cm-rooms"); ul.id = "cmRooms"; sec.append(ul);
  for (const r of rooms) {
    const li = cme("li"); li.dataset.room = r.id;
    const head = cme("div", "cm-room-head");
    const lab = cme("label", "check"), cb = cme("input"); cb.type = "checkbox"; cb.checked = r.preheat; cb.dataset.preheat = r.id;
    cb.setAttribute("aria-label", `Preheat ${r.name}`);
    cb.onchange = () => cmSend(`/api/climate/rooms/${encodeURIComponent(r.id)}`, { preheat: cb.checked }, cb.checked ? `Preheat on for ${r.name}` : `Preheat off for ${r.name}`);
    lab.append(cb, cme("span", "cm-name", r.name)); head.append(lab);
    if (r.preheat) {
      const b = cme("button", "cm-small", "Re-learn"); b.type = "button"; b.dataset.learn = r.id;
      b.title = "Re-learn the warm-up rate from the last 7 days of Home Assistant history";
      b.onclick = () => { b.disabled = true; cmSend(`/api/climate/rooms/${encodeURIComponent(r.id)}/learn`, null, `${r.name}: re-learnt`, "POST"); };
      head.append(b);
    }
    li.append(head);
    const rate = r.rate != null ? `Warms ≈ ${r.rate.toFixed(1)}°/h (learnt from ${r.rate_n} warm-up${r.rate_n === 1 ? "" : "s"})`
      : `Not learnt yet — assumes ${d.default_rate}°/h`;
    li.append(cme("div", "sub cm-rate", rate));
    const n = r.next;
    let nextText = "No heating schedule in the next 24 h";
    if (n) {
      nextText = `“${n.name}” ${cmWhen(n.at)} → ${cmDeg(n.setpoint)}`;
      if (n.started) nextText += ` · preheating since ${cmHm(n.started)}`;
      else if (r.preheat && s.preheat_enabled && n.start != null) {
        nextText += n.lead_min >= 5 ? ` · starts ≈ ${cmHm(n.start)} (${n.lead_min} min early)` : " · already warm enough";
      }
    }
    const nx = cme("div", "sub cm-next", nextText); li.append(nx);
    if (r.preheat && r.blocked) li.append(cme("div", "sub cm-blocked", r.blocked === "away" ? "Paused — Away" : "Paused — window open"));
    ul.append(li);
  }
  if (none.length) sec.append(cme("div", "hint", `No radiator valve in: ${none.map((r) => r.name).join(", ")}.`));
}

function cmDamp(c, d) {
  const s = d.settings, sec = cme("section", "cm-sec"); sec.id = "cmDampSec"; c.append(sec);
  sec.append(cme("h4", null, "Damp & mould"));
  const rooms = d.rooms.filter((r) => r.damp.humidity != null || r.damp.at_risk);
  const ul = cme("ul", "cm-damp"); ul.id = "cmDamp"; sec.append(ul);
  for (const r of rooms) {
    const x = r.damp, li = cme("li", x.sustained ? "risk" : x.at_risk ? "watch" : "ok"); li.dataset.room = r.id;
    const top = cme("div", "cm-damp-top");
    top.append(cme("span", "cm-name", r.name));
    const vals = [x.humidity != null ? `${Math.round(x.humidity)} %` : "– %", x.temperature != null ? cmDeg(x.temperature) : "–°"].join(" · ");
    top.append(cme("span", "cm-vals", vals));
    li.append(top);
    let state = "OK";
    if (x.sustained) state = `Damp risk since ${cmWhen(x.since)} — run a dehumidifier or air the room`;
    else if (x.at_risk) state = `Humid and cool since ${cmWhen(x.since)} — warning after ${s.damp_minutes} min`;
    const st = cme("div", "cm-state", state); li.append(st);
    if (x.humidity_source) li.append(cme("div", "sub cm-src", `Humidity from ${x.humidity_source}`));
    if (x.sustained && x.dehumidifier) {
      const b = cme("button", "cm-small cm-dehum", `${x.dehumidifier.name}${x.dehumidifier.state === "on" ? " (on)" : x.dehumidifier.state === "off" ? " (off)" : ""} →`);
      b.type = "button"; b.dataset.dehum = x.dehumidifier.entity_id;
      b.onclick = () => { closeClimate(); openSheet(x.dehumidifier.entity_id); };
      li.append(b);
    }
    ul.append(li);
  }
  const blind = d.rooms.filter((r) => r.damp.humidity == null && !r.damp.at_risk);
  if (!rooms.length) sec.append(cme("div", "hist-msg", "No humidity readings yet: place a dehumidifier in a room, or choose a humidity sensor below."));
  else if (blind.length) sec.append(cme("div", "hint", `No humidity reading in: ${blind.map((r) => r.name).join(", ")}.`));

  const set = cme("div", "cm-damp-set");
  set.append(cme("div", "cm-label", "Damp risk when, together:"));
  const fields = cme("div", "cm-fields");
  const field = (text, input, unit) => { const l = cme("label"); const u = cme("span", "cm-unit"); u.append(input, ` ${unit}`); l.append(cme("span", null, text), u); return l; };
  const thr = (k) => (v) => cmSend("/api/climate/settings", { [k]: v }, "Damp threshold saved");
  fields.append(field("Humidity at least", cmNum("cmHum", s.damp_humidity, 50, 95, 1, thr("damp_humidity")), "%"),
    field("Temperature at most", cmNum("cmTemp", s.damp_temp, 10, 22, 0.5, thr("damp_temp")), "°"),
    field("For", cmNum("cmMins", s.damp_minutes, 15, 720, 15, thr("damp_minutes")), "min"));
  set.append(fields);
  set.append(cmCheck("cmDampPush", s.damp_push, "Push a damp warning (held in quiet hours)",
    (on) => cmSend("/api/climate/settings", { damp_push: on }, on ? "Damp push on" : "Damp push off")));
  const cool = cme("label", "cm-row auto-sub" + (s.damp_push ? "" : " off"), "At most every ");
  cool.append(cmNum("cmCool", s.damp_cooldown_h, 1, 72, 1, (v) => cmSend("/api/climate/settings", { damp_cooldown_h: v }, "Damp push saved")), " h per room");
  set.append(cool);
  sec.append(set);

  const det = cme("details", "cm-fold"); det.open = cm.open.sensors; det.id = "cmSensors";
  det.addEventListener("toggle", () => { cm.open.sensors = det.open; });
  det.append(cme("summary", null, "Humidity sensors"));
  det.append(cme("div", "hint", "By default a room uses a dehumidifier placed in it. Pick a Home Assistant humidity sensor to use instead."));
  for (const r of d.rooms) {
    const lab = cme("label", "cm-sensor"), sel = cme("select"); sel.dataset.sensor = r.id;
    lab.append(cme("span", null, r.name), sel);
    const auto = cme("option", null, "Auto (devices in the room)"); auto.value = ""; sel.append(auto);
    for (const h of d.humidity_sensors) { const o = cme("option", null, h.name); o.value = h.entity_id; sel.append(o); }
    if (r.humidity_entity && !d.humidity_sensors.some((h) => h.entity_id === r.humidity_entity)) {
      const o = cme("option", null, `${r.humidity_entity} (missing)`); o.value = r.humidity_entity; sel.append(o);
    }
    sel.value = r.humidity_entity || "";
    sel.onchange = () => cmSend(`/api/climate/rooms/${encodeURIComponent(r.id)}`, { humidity_entity: sel.value || null }, `${r.name}: humidity sensor saved`);
    det.append(lab);
  }
  sec.append(det);
}

function cmLog(c, d) {
  if (!d.log.length) return;
  const det = cme("details", "cm-fold cm-log"); det.open = cm.open.log; det.id = "cmLog";
  det.addEventListener("toggle", () => { cm.open.log = det.open; });
  det.append(cme("summary", null, `Recent (${Math.min(d.log.length, 10)})`));
  for (const x of d.log.slice(0, 10)) {
    const r = cme("div", "sum-row");
    r.append(cme("span", null, `${x.room ? x.room + ": " : ""}${x.action}${x.note ? ` — ${x.note}` : ""}`), cme("span", null, cmWhen(x.at)));
    det.append(r);
  }
  c.append(det);
}

// ---------- wiring: ⋯ menu entry (after Schedules…), push link ----------
(() => {
  const b = cme("button", null, "Climate…"); b.id = "climateBtn"; b.setAttribute("role", "menuitem");
  const anchor = $("schedulesBtn");
  if (anchor) anchor.after(b); else $("moreMenu").prepend(b);
  b.addEventListener("click", openClimate);
  const fromUrl = (u) => { try { return new URL(u, location.origin).searchParams.has("climate"); } catch { return false; } };
  document.addEventListener("DOMContentLoaded", () => {
    if (fromUrl(location.href)) { history.replaceState(null, "", location.pathname); openClimate(); }
    navigator.serviceWorker?.addEventListener("message", (e) => { if (e.data?.type === "open" && fromUrl(e.data.url)) openClimate(); });
  });
})();
