"use strict";
// Light brightness/colour sheet, long-press, room-name toggles, "All off" and the heating sheet. Hooks are called from app.js.
const LP_MS = 450, LP_MOVE = 8, SEND_EVERY = 300;
const SWATCHES = [[0, 100], [28, 100], [52, 100], [120, 90], [180, 90], [225, 100], [275, 90], [320, 75]];
const HEATING = "@heating";
const isOnOff = (d) => d && (d.kind === "light" || d.kind === "plug");
const usable = (d) => d && d.state !== "unavailable" && d.state !== "unknown";
const mk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

// ---------- colour helpers ----------
function hsToRgb(h, s) {
  s /= 100; const f = (n) => { const k = (n + h / 60) % 6; return Math.round(255 * (1 - s * Math.max(0, Math.min(k, 4 - k, 1)))); };
  return [f(5), f(3), f(1)];
}
const hex = (rgb) => "#" + rgb.map((v) => v.toString(16).padStart(2, "0")).join("");
const fromHex = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
function lightRgb(d) {
  if (!d || d.kind !== "light" || d.state !== "on") return null;
  if (Array.isArray(d.rgb_color)) return d.rgb_color.map(Math.round);
  if (Array.isArray(d.hs_color)) return hsToRgb(...d.hs_color);
  return null;
}
// Marker fill for a lit bulb; null keeps the default amber.
function lightColor(d) { const c = lightRgb(d); return c ? `rgb(${c.join(",")})` : null; }
// Dark fills get a light icon so it stays readable.
function iconFill(d) {
  const c = lightRgb(d); if (!c) return null;
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2] < 110 ? "#f4f6fa" : null;
}

// ---------- throttled sending ----------
function throttle(send) {
  let last = 0, timer = null, pending = null, sent = "";
  const run = () => { timer = null; last = Date.now(); const k = JSON.stringify(pending); if (k !== sent) { sent = k; send(pending); } };
  return {
    push(v) { pending = v; if (!timer) timer = setTimeout(run, Math.max(0, last + SEND_EVERY - Date.now())); },
    flush(v) { pending = v; clearTimeout(timer); run(); },
  };
}
let holdTimer = null;
function holdSheet(ms = 1500) { st.holdSheet = true; clearTimeout(holdTimer); holdTimer = setTimeout(() => { st.holdSheet = false; }, ms); }
async function sendLight(d, body) {
  try { await api(`/api/devices/${encodeURIComponent(d.entity_id)}/light`, { method: "POST", body: JSON.stringify(body) }); }
  catch (e) { setStatus(`Light update failed: ${e.message}`, true); }
}

// ---------- long-press on markers and list rows ----------
let lp = null, lpFired = 0;
function lpCancel() { if (lp) clearTimeout(lp.timer); lp = null; }
document.addEventListener("pointerdown", (e) => {
  lpCancel();
  if (st.editing || e.button > 0) return;
  const t = e.target.closest?.("#plan .marker, #list li[data-dev]"); if (!t) return;
  const eid = t.dataset.dev; if (!isOnOff(st.devices.get(eid))) return;
  lp = { eid, x: e.clientX, y: e.clientY, id: e.pointerId };
  lp.timer = setTimeout(() => { lp = null; lpFired = Date.now(); navigator.vibrate?.(12); openSheet(eid); }, LP_MS);
}, true);
document.addEventListener("pointermove", (e) => {
  if (lp && e.pointerId === lp.id && Math.hypot(e.clientX - lp.x, e.clientY - lp.y) > LP_MOVE) lpCancel();
}, true);
document.addEventListener("pointerup", () => { if (lp) clearTimeout(lp.timer); lp = null; }, true);
document.addEventListener("pointercancel", lpCancel, true);
// The click that ends a long-press must not toggle (or close the sheet it just opened).
document.addEventListener("click", (e) => {
  if (lpFired && Date.now() - lpFired < 1500) { lpFired = 0; e.stopPropagation(); e.preventDefault(); }
}, true);
document.addEventListener("contextmenu", (e) => {
  if (!st.editing && e.target.closest?.("#plan .marker, #list li[data-dev]")) e.preventDefault();
});

// ---------- light / plug sheet extras ----------
function slider(label, min, max, step, value, fmt, onInput, onDone) {
  const wrap = mk("label", "ctl"); const top = mk("span", "ctl-top"); const name = mk("span", null, label); const out = mk("span", "ctl-val", fmt(value));
  top.append(name, out);
  const r = mk("input"); Object.assign(r, { type: "range", min, max, step, value });
  r.oninput = () => { holdSheet(); out.textContent = fmt(+r.value); onInput(+r.value); };
  r.onchange = () => { holdSheet(800); onDone(+r.value); };
  wrap.append(top, r); return wrap;
}
function extraSheet(d, c, unavailable) {
  if (d.kind === "plug") return plugExtras(d, c);
  if (d.kind !== "light" || unavailable) return;
  const box = mk("div", "light-ctl"); c.appendChild(box);
  const optimistic = (patch) => { Object.assign(d, { state: "on" }, patch); render(); };
  if (d.supports_brightness) {
    const pct = d.state === "on" && d.brightness != null ? Math.max(1, Math.round(d.brightness / 2.55)) : 100;
    const t = throttle((v) => sendLight(d, { brightness_pct: v }));
    const go = (v) => { optimistic({ brightness: Math.round(v * 2.55) }); };
    box.appendChild(slider("Brightness", 1, 100, 1, pct, (v) => `${v} %`, (v) => { go(v); t.push(v); }, (v) => { go(v); t.flush(v); }));
  }
  if (d.supports_color_temp) {
    const lo = d.min_color_temp_kelvin, hi = d.max_color_temp_kelvin;
    const k = Math.min(hi, Math.max(lo, d.color_temp_kelvin || Math.round((lo + hi) / 2)));
    const t = throttle((v) => sendLight(d, { color_temp_kelvin: v }));
    const go = (v) => optimistic({ color_temp_kelvin: v, color_mode: "color_temp", rgb_color: null, hs_color: null });
    const s = slider("White", lo, hi, 50, k, (v) => `${v} K`, (v) => { go(v); t.push(v); }, (v) => { go(v); t.flush(v); });
    s.classList.add("kelvin"); box.appendChild(s);
  }
  if (d.supports_color) {
    const row = mk("div", "swatches"); box.appendChild(mk("div", "ctl-top", "Colour")); box.appendChild(row);
    for (const hs of SWATCHES) {
      const b = mk("button", "swatch"); b.style.background = hex(hsToRgb(...hs)); b.setAttribute("aria-label", `Colour hue ${hs[0]}`);
      b.onclick = () => { optimistic({ hs_color: hs, rgb_color: hsToRgb(...hs), color_mode: "hs" }); renderSheet(); sendLight(d, { hs_color: hs }); };
      row.appendChild(b);
    }
    const pick = mk("input", "picker"); pick.type = "color"; pick.value = hex(lightRgb(d) || [255, 196, 65]); pick.title = "Pick any colour";
    const t = throttle((rgb) => sendLight(d, { rgb_color: rgb }));
    pick.oninput = () => { holdSheet(); const rgb = fromHex(pick.value); optimistic({ rgb_color: rgb, hs_color: null, color_mode: "rgb" }); t.push(rgb); };
    pick.onchange = () => { holdSheet(800); t.flush(fromHex(pick.value)); };
    row.appendChild(pick);
  }
}
function keepOn() { return st.layout.settings?.keep_on || []; }
function plugExtras(d, c) {
  const lab = mk("label", "keep-on"); const cb = mk("input"); cb.type = "checkbox"; cb.checked = keepOn().includes(d.entity_id);
  lab.append(cb, mk("span", null, "Keep on — skip with “All off”"));
  cb.onchange = async () => {
    const prev = st.layout.settings;
    const set = new Set(keepOn()); cb.checked ? set.add(d.entity_id) : set.delete(d.entity_id);
    st.layout.settings = { ...(prev || {}), keep_on: [...set] };
    try { st.layout = await api("/api/layout", { method: "PUT", body: JSON.stringify(st.layout) }); setStatus(cb.checked ? `${d.name} stays on with “All off”` : "Saved"); }
    catch (e) { st.layout.settings = prev; cb.checked = !cb.checked; setStatus(`Save failed: ${e.message}`, true); }
  };
  c.appendChild(lab);
}

// ---------- bulk on/off ----------
async function bulk(action, devs) {
  const prev = devs.map((d) => [d, d.state]);
  for (const d of devs) d.state = action === "turn_on" ? "on" : "off";
  render(); if (st.sheetFor) renderSheet();
  try { await api("/api/bulk", { method: "POST", body: JSON.stringify({ action, entity_ids: devs.map((d) => d.entity_id) }) }); }
  catch (e) { for (const [d, s] of prev) d.state = s; render(); setStatus(`Failed: ${e.message}`, true); return false; }
  if (!st.live) setTimeout(loadDevices, 800);
  return true;
}
async function allOff() {
  const keep = new Set(keepOn());
  const devs = [...st.devices.values()].filter((d) => isOnOff(d) && usable(d) && !keep.has(d.entity_id));
  const on = devs.filter((d) => d.state === "on");
  if (!on.length) { setStatus("Everything is already off" + (keep.size ? ` (${keep.size} kept on)` : "")); return; }
  const kept = [...keep].filter((e) => st.devices.get(e)?.state === "on").length;
  if (!confirm(`Turn off ${on.length} device${on.length > 1 ? "s" : ""}?` + (kept ? `\n${kept} “keep on” plug${kept > 1 ? "s" : ""} stay on.` : ""))) return;
  if (await bulk("turn_off", devs)) setStatus(`Turned off ${on.length}`);
}

// ---------- room name toggles ----------
function roomLights(r) {
  return st.layout.placements.filter((p) => inRoom(r, p)).map((p) => st.devices.get(p.entity_id))
    .filter((d) => d?.kind === "light" && usable(d));
}
function renderRoomLabels(parent) {
  if (st.editing) return;
  const mpp = st.viewBox[2] / (svg.clientWidth || 800);
  for (const r of st.layout.rooms) {
    const k = st.markerScale || 1; // wall mode: bigger names, bigger targets
    const lp = labelPos(r), h = Math.max(0.6 * k, 40 * mpp * k);
    const w = Math.min(r.w, Math.max(r.name.length * 0.2 * k + 0.3, 44 * mpp * k));
    const lights = roomLights(r), on = lights.some((d) => d.state === "on");
    const cls = "room-tap" + (lights.length ? "" : " empty") + (on ? " lit" : "") + (st.flashRoom === r.id ? " flash" : "");
    const g = el("g", { class: cls, "data-roomtap": r.id }, parent);
    el("rect", { x: lp.x + 0.05, y: lp.y + 0.42 - h / 2 - 0.1, width: w, height: h, rx: 0.12 }, g);
    const t = el("title", {}, g); t.textContent = lights.length ? `${r.name}: tap to turn ${on ? "off" : "on"} ${lights.length} light${lights.length > 1 ? "s" : ""}` : `${r.name}: no lights`;
  }
}
svg.addEventListener("click", async (e) => {
  const t = e.target.closest?.("[data-roomtap]"); if (!t || st.editing) return;
  const r = st.layout.rooms.find((x) => x.id === t.dataset.roomtap); if (!r) return;
  const lights = roomLights(r);
  if (!lights.length) { setStatus(`No lights in ${r.name}`); return; }
  const action = lights.some((d) => d.state === "on") ? "turn_off" : "turn_on";
  st.flashRoom = r.id; setTimeout(() => { if (st.flashRoom === r.id) { st.flashRoom = null; render(); } }, 600);
  if (await bulk(action, lights)) setStatus(`${r.name}: ${lights.length} light${lights.length > 1 ? "s" : ""} ${action === "turn_on" ? "on" : "off"}`);
});

// ---------- heating sheet ----------
const valves = () => [...st.devices.values()].filter((d) => d.kind === "valve").sort((a, b) => a.name.localeCompare(b.name));
let allTarget = null;
const valveTimers = new Map();
function stepper(value, onStep, disabled) {
  const row = mk("div", "temp small"); const minus = mk("button", null, "−"), plus = mk("button", null, "+");
  const val = mk("div", "target", value); minus.onclick = () => onStep(-1, val); plus.onclick = () => onStep(1, val);
  minus.disabled = plus.disabled = !!disabled; row.append(minus, val, plus); return row;
}
const stepT = (t, dir, d) => { const s = d?.target_temp_step || 0.5; return Math.min(d?.max_temp ?? 30, Math.max(d?.min_temp ?? 5, Math.round((t + dir * s) / s) * s)); };
function openHeating() { allTarget = null; openSheet(HEATING); }
function renderHeating(c) {
  c.appendChild(mk("h3", null, "Heating"));
  const vs = valves();
  if (!vs.length) { c.appendChild(mk("div", "sub", "No radiator valves found.")); return; }
  c.appendChild(mk("div", "sub", "Tap +/− per radiator, or set them all at once."));
  const list = mk("div", "valves"); c.appendChild(list);
  for (const d of vs) {
    const row = mk("div", "valve-row"); const info = mk("div", "valve-info");
    info.append(mk("div", "name", d.name), mk("div", "sub", usable(d) ? `Now ${d.current_temperature ?? "–"}° · ${d.state}` : d.state));
    row.append(info, stepper(`${d.temperature ?? "–"}°`, (dir, val) => {
      const t = stepT(d.temperature ?? 20, dir, d); d.temperature = t; val.textContent = `${t}°`; render(); holdSheet(2000);
      clearTimeout(valveTimers.get(d.entity_id));
      valveTimers.set(d.entity_id, setTimeout(() => { valveTimers.delete(d.entity_id); setTemp(d.entity_id, t); }, 700));
    }, !usable(d)));
    list.appendChild(row);
  }
  const ok = vs.filter(usable);
  if (allTarget == null) allTarget = ok.length ? Math.max(...ok.map((d) => d.temperature ?? 20)) : 20;
  const all = mk("div", "valve-row all"); const info = mk("div", "valve-info"); info.append(mk("div", "name", "All radiators"));
  all.append(info, stepper(`${allTarget}°`, (dir, val) => { allTarget = stepT(allTarget, dir, ok[0]); val.textContent = `${allTarget}°`; }, !ok.length));
  c.appendChild(all);
  const apply = mk("button", "big primary", "Apply to all"); apply.disabled = !ok.length;
  apply.onclick = async () => {
    const t = allTarget; const prev = ok.map((d) => [d, d.temperature]);
    ok.forEach((d) => (d.temperature = t)); render(); renderSheet(); holdSheet(2000);
    try { await api("/api/valves/temperature", { method: "POST", body: JSON.stringify({ temperature: t, entity_ids: ok.map((d) => d.entity_id) }) }); setStatus(`All radiators set to ${t}°`); }
    catch (e) { prev.forEach(([d, v]) => (d.temperature = v)); render(); renderSheet(); setStatus(`Set temperature failed: ${e.message}`, true); }
  };
  c.appendChild(apply);
}

$("allOff").onclick = allOff;
$("heating").onclick = openHeating;
