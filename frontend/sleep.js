"use strict";
// Sleep timers: "off in 15 / 30 / 60 / n minutes" for a light, plug or TV (in its sheet) or a whole room (the room
// view's Sleep chip). The server runs them (backend/sleeptimer.py), so they keep going while this phone sleeps and
// survive an app restart. Running timers show in a strip above the device list (live countdown, × cancels), in the
// device's sheet and on the room chip. State: GET /api/timers, then the "timers" SSE event.
// Hooks: sleepEvents(es) (app.js startLive), sleepSheetRow(d, c) (app.js renderSheet), sleepRoomChip(r) (roomview.js).
// Uses app.js (st, api, $, setStatus, renderSheet, render), controls.js (keepOn, usable), appliances.js (protectedPlugs).
const SLEEP = (() => {
  const sl = { s: null, skew: 0, tick: 0, busy: false, custom: null, room: null, lastSeen: null };
  const sx = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const MOON = `<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M20.5 14.6A8.5 8.5 0 0 1 9.4 3.5a.6.6 0 0 0-.8-.7A9.5 9.5 0 1 0 21.2 15.4a.6.6 0 0 0-.7-.8z"/></svg>`;
  const role = () => document.documentElement.dataset.role || "admin";
  const timers = () => sl.s?.timers || [];
  const presets = () => sl.s?.presets || [15, 30, 60];
  const maxMin = () => sl.s?.max_minutes || 720;
  const now = () => Date.now() + sl.skew;
  const fmtMin = (m) => m >= 60 ? `${Math.floor(m / 60)} h${m % 60 ? ` ${m % 60} min` : ""}` : `${m} min`;
  const left = (t) => { const ms = t.ends_at - now(); return ms < 60000 ? "under a minute" : fmtMin(Math.ceil(ms / 60000)); };
  const clock = (t) => new Date(t.ends_at - sl.skew).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const forEntity = (eid) => timers().find((t) => t.target === "entity" && t.entity_id === eid) || null;
  const forRoom = (rid) => timers().find((t) => t.target === "room" && t.room === rid) || null;
  const room = (id) => st.layout.rooms.find((r) => r.id === id) || null;
  // The SSE event carries no per-role flag: guests may cancel the timers that switch lights only (the server checks).
  const mayCancel = (t) => t.may_cancel ?? (role() !== "guest" || t.entity_ids.every((e) => e.startsWith("light.")));
  function guarded() { return new Set([...(typeof keepOn === "function" ? keepOn() : []), ...(typeof protectedPlugs === "function" ? protectedPlugs() : [])]); }
  // What a timer may switch off (the server checks the same).
  function switchable(d, keep = guarded()) {
    if (!d || d.hidden || !["light", "plug", "media"].includes(d.kind)) return false;
    if (role() === "guest" && d.kind !== "light") return false;
    if (d.kind === "plug" && keep.has(d.entity_id)) return false;
    if (d.kind === "media" && !d.supports?.turn_off) return false;
    return true;
  }
  function roomTargets(r) {
    const keep = guarded();
    const ids = st.layout.placements.filter((p) => inRoom(r, p)).map((p) => p.entity_id);
    for (const f of st.layout.furniture || []) if (f.media && inRoom(r, f)) ids.push(f.media);
    return [...new Set(ids)].map((e) => st.devices.get(e)).filter((d) => switchable(d, keep));
  }

  // ---------- DOM: the strip above the device list, the room sheet ----------
  const strip = sx("ul", "sleep-strip"); strip.id = "sleepStrip"; strip.hidden = true; strip.setAttribute("aria-label", "Sleep timers");
  $("list").before(strip);
  const sheet = sx("div", "sheet sleep-sheet"); sheet.id = "sleepSheet"; sheet.hidden = true;
  sheet.innerHTML = `<div class="sheet-body" role="dialog" aria-modal="true" aria-labelledby="sleepTitle">
    <button class="close" id="sleepClose" type="button" aria-label="Close">×</button><div id="sleepContent"></div></div>`;
  document.body.append(sheet);

  // ---------- set / cancel ----------
  async function set(body, minutes) {
    if (sl.busy) return;
    if (!(minutes >= 1 && minutes <= maxMin())) { setStatus(`Pick 1–${maxMin()} minutes`, true); return; }
    sl.busy = true;
    try {
      const t = await api("/api/timers", { method: "POST", body: JSON.stringify({ ...body, minutes }) });
      await load(); sl.custom = null;
      setStatus(`Sleep timer: ${t.name} off in ${fmtMin(minutes)} (${clock(t)})`);
    } catch (e) { setStatus(`Sleep timer: ${e.message}`, true); }
    finally { sl.busy = false; repaint(); }
  }
  async function cancel(t) {
    try { await api(`/api/timers/${encodeURIComponent(t.id)}`, { method: "DELETE" }); await load(); setStatus(`Sleep timer for ${t.name} cancelled`); }
    catch (e) { setStatus(`Cancel failed: ${e.message}`, true); }
  }

  // The row of choices: 15 min · 30 min · 1 h · Custom (a small number field instead of a prompt()).
  function chooser(body, key, cur) {
    const box = sx("div", "sleep-choose");
    const chips = sx("div", "sleep-chips"); chips.setAttribute("role", "group"); chips.setAttribute("aria-label", "Turn off in");
    for (const m of presets()) {
      const b = sx("button", "sleep-chip" + (cur?.minutes === m ? " cur" : ""), fmtMin(m)); b.type = "button"; b.dataset.min = m;
      b.disabled = sl.busy; b.onclick = () => set(body, m); chips.append(b);
    }
    const open = sl.custom === key;
    const c = sx("button", "sleep-chip" + (open || (cur && !presets().includes(cur.minutes)) ? " cur" : ""), "Custom"); c.type = "button"; c.dataset.min = "custom";
    c.setAttribute("aria-expanded", open ? "true" : "false");
    c.onclick = () => { sl.custom = open ? null : key; repaint(); if (!open) box.closest(".sheet-body")?.querySelector(".sleep-custom input")?.focus(); };
    chips.append(c); box.append(chips);
    if (open) {
      const f = sx("form", "sleep-custom"); const lab = sx("label"); const inp = sx("input");
      Object.assign(inp, { type: "number", min: 1, max: maxMin(), step: 1, inputMode: "numeric", value: cur?.minutes || 45, required: true });
      inp.setAttribute("aria-label", "Minutes");
      lab.append(inp, sx("span", null, "min"));
      const go = sx("button", "primary", "Set"); go.type = "submit"; go.disabled = sl.busy;
      f.append(lab, go); f.onsubmit = (e) => { e.preventDefault(); set(body, Math.round(+inp.value)); };
      box.append(f);
    }
    return box;
  }
  function countdown(t, withCancel = true) {
    const row = sx("div", "sleep-now"); row.dataset.timer = t.id;
    const txt = sx("span", "sleep-left"); txt.innerHTML = MOON; txt.append(sx("span", null, `Off in ${left(t)} · ${clock(t)}`));
    txt.lastChild.dataset.ends = t.ends_at; row.append(txt);
    if (withCancel && mayCancel(t)) { const x = sx("button", "sleep-cancel", "Cancel"); x.type = "button"; x.onclick = () => cancel(t); row.append(x); }
    return row;
  }

  // ---------- device sheet ----------
  function sheetRow(d, c) {
    if (!switchable(d) || (d.kind === "media" && typeof tvIsOn === "function" && !tvIsOn(d) && !forEntity(d.entity_id))) return;
    const t = forEntity(d.entity_id);
    const sec = sx("section", "sleep-row"); sec.id = "sleepRow";
    const h = sx("div", "sleep-head"); h.innerHTML = MOON; h.append(sx("span", null, "Sleep timer"));
    sec.append(h);
    if (t) sec.append(countdown(t));
    sec.append(chooser({ entity_id: d.entity_id }, d.entity_id, t));
    c.append(sec);
  }

  // ---------- room sheet ----------
  function openRoom(r) { if (st.editing) return; sl.room = r.id; sl.custom = null; draw(); sheet.hidden = false; $("sleepClose").focus({ preventScroll: true }); }
  function closeRoom() { sheet.hidden = true; sl.room = null; sl.custom = null; }
  function draw() {
    const c = $("sleepContent"); c.replaceChildren();
    const r = room(sl.room); if (!r) { closeRoom(); return; }
    const h = sx("h3", null, `Sleep timer · ${r.name}`); h.id = "sleepTitle"; c.append(h);
    const t = forRoom(r.id), devs = roomTargets(r);
    const names = (t ? t.entity_ids.map((e) => st.devices.get(e)?.name || e) : devs.map((d) => d.name)).sort((a, b) => a.localeCompare(b));
    c.append(sx("div", "sub", names.length ? `Turns off ${names.join(", ")}` : `Nothing in ${r.name} a timer can switch off.`));
    if (t) c.append(countdown(t));
    if (devs.length) c.append(chooser({ room: r.id }, `room:${r.id}`, t));
    c.append(sx("p", "hint sleep-hint", "Only what is still on gets switched off. Fridges, the home server and keep-on plugs never are."));
  }
  function roomChip(r) {
    if (!roomTargets(r).length && !forRoom(r.id)) return null;
    const t = forRoom(r.id);
    const b = sx("button", "rf sleep-chip-room" + (t ? " on" : "")); b.type = "button"; b.dataset.fact = "sleep";
    b.innerHTML = MOON; const s = sx("span", null, t ? `Off in ${left(t)}` : "Sleep"); if (t) s.dataset.ends = t.ends_at; b.append(s);
    b.title = t ? `Sleep timer: ${r.name} off at ${clock(t)} — tap to change` : `Sleep timer for ${r.name}`;
    b.onclick = () => openRoom(r);
    return b;
  }

  // ---------- the strip ----------
  function paintStrip() {
    const ts = timers();
    strip.replaceChildren(); strip.hidden = !ts.length;
    for (const t of ts) {
      const li = sx("li", "sleep-item" + (t.retrying ? " retry" : "")); li.dataset.timer = t.id;
      const name = t.target === "room" ? `${t.name} (room)` : t.name;
      const txt = sx("span", "sleep-left"); txt.innerHTML = MOON;
      const n = sx("span", "name", name); const v = sx("span", "val", t.retrying ? "retrying…" : left(t));
      if (!t.retrying) v.dataset.ends = t.ends_at;
      txt.append(n, v); li.append(txt); li.title = `Sleep timer: off at ${clock(t)}`;
      if (mayCancel(t)) { const x = sx("button", "sleep-x", "×"); x.type = "button"; x.setAttribute("aria-label", `Cancel the sleep timer for ${t.name}`); x.onclick = () => cancel(t); li.append(x); }
      strip.append(li);
    }
  }
  // Every countdown on screen carries data-ends; one timer keeps them all current.
  function ticker() {
    for (const e of document.querySelectorAll("[data-ends]")) {
      const t = { ends_at: +e.dataset.ends }, l = left(t);
      e.textContent = e.closest(".sleep-strip") ? l : e.closest(".sleep-now") ? `Off in ${l} · ${clock(t)}` : `Off in ${l}`;
    }
  }
  function repaint() {
    paintStrip();
    if (!sheet.hidden) draw();
    if (st.sheetFor && $("sleepRow")) renderSheet();
    if (st.room && typeof render === "function") render(); // the room chip
  }
  function apply(s) {
    const was = new Map(timers().map((t) => [t.id, t]));
    sl.s = s; sl.skew = (s.now || Date.now()) - Date.now();
    const fired = s.last && s.last.at !== sl.lastSeen?.at ? s.last : null;
    if (fired && sl.lastSeen !== null && was.has(fired.id)) setStatus(fired.ok ? `Sleep timer: ${fired.name} off` : `Sleep timer: ${fired.name} — Home Assistant kept failing`, !fired.ok);
    sl.lastSeen = s.last || { at: 0 };
    clearInterval(sl.tick);
    if (timers().length) sl.tick = setInterval(ticker, 15000);
    repaint();
  }
  async function load() { try { apply(await api("/api/timers")); } catch {} }

  $("sleepClose").onclick = closeRoom;
  sheet.addEventListener("click", (e) => { if (e.target === sheet) closeRoom(); });
  window.addEventListener("keydown", (e) => { if (e.key === "Escape" && !sheet.hidden) { e.stopImmediatePropagation(); closeRoom(); } }, true);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
  document.addEventListener("DOMContentLoaded", load);
  return { apply, load, sheetRow, roomChip, openRoom, get state() { return sl.s; } };
})();
function sleepEvents(es) {
  es.addEventListener("timers", (e) => { try { SLEEP.apply(JSON.parse(e.data)); } catch {} });
  es.addEventListener("snapshot", () => SLEEP.load()); // reconnected: catch up
}
function sleepSheetRow(d, c) { SLEEP.sheetRow(d, c); }
function sleepRoomChip(r) { return SLEEP.roomChip(r); }
