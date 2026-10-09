"use strict";
const FT = 3.28084;
const KIND_LABEL = { light: "Lights", plug: "Plugs", valve: "Radiator valves", dehumidifier: "Dehumidifier", media: "TV & media", sensor: "Door / window sensors" };
const KIND_ORDER = ["light", "plug", "valve", "dehumidifier", "media", "sensor"];
const SNAP = 0.05;
const $ = (id) => document.getElementById(id);
const svg = $("plan");
const NS = "http://www.w3.org/2000/svg";

const st = {
  devices: new Map(), layout: { unit: "m", rooms: [], placements: [] }, draft: null,
  editing: false, sel: null, picked: null, sheetFor: null, drag: null, viewBox: null,
};
const cur = () => st.editing ? st.draft : st.layout;
const clone = (o) => JSON.parse(JSON.stringify(o));
const unit = () => cur().unit || "m";
const toDisp = (m) => unit() === "ft" ? m * FT : m;
const fromDisp = (v) => unit() === "ft" ? v / FT : v;
const fmtLen = (m) => `${toDisp(m).toFixed(1)}${unit()}`;
const snap = (v) => Math.round(v / SNAP) * SNAP;

function setStatus(msg, err = false) { const s = $("status"); s.textContent = msg; s.classList.toggle("err", err); }

async function api(path, opts = {}) {
  let r;
  try {
    r = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", ...(opts.headers || {}) } });
  } catch (e) { markOffline(true); throw new Error("offline"); }
  if (r.status === 401) { location.replace("/login.html"); throw new Error("signed out"); }
  markOffline(r.headers.get("X-From-SW-Cache") === "1");
  if (!r.ok) {
    let d = r.statusText;
    try { d = (await r.json()).detail || d; } catch {}
    throw new Error(typeof d === "string" ? d : JSON.stringify(d));
  }
  return r.json();
}

// ---------- session / offline ----------
let offline = false;
function markOffline(on) { offline = on || !navigator.onLine; }
function offlineStatus() { if (offline) { setStatus("Offline — showing last known state"); $("status").classList.add("offline"); } }
window.addEventListener("offline", () => { offline = true; offlineStatus(); });
window.addEventListener("online", () => { offline = false; $("status").classList.remove("offline"); loadDevices(); });
async function signOut() {
  if (!confirm("Sign out?")) return;
  try { await fetch("/api/logout", { method: "POST" }); } catch {}
  location.replace("/login.html");
}
document.getElementById("signOut").addEventListener("click", signOut);

// ---------- device state ----------
function deviceColor(d) {
  if (!d || d.state === "unavailable" || d.state === "unknown") return "var(--unavailable)";
  switch (d.kind) {
    case "light": case "plug": return d.state === "on" ? lightColor(d) || "var(--on)" : "var(--off)";
    case "sensor": return d.state === "on" ? "var(--open)" : "var(--closed)";
    case "dehumidifier": return dehumColor(d);
    case "media": return tvColor(d); // tv.js
    case "valve":
      if (d.state === "off") return "var(--off)";
      return d.current_temperature != null && d.temperature != null && d.current_temperature < d.temperature
        ? "var(--heat)" : "var(--idle)";
  }
  return "var(--off)";
}
function deviceValue(d) {
  if (!d) return "";
  if (d.state === "unavailable" || d.state === "unknown") return d.state;
  const bat = batteryWarn(d) ? (d.battery != null ? ` · 🔋 ${d.battery} %` : " · 🔋 low") : "";
  if (d.kind === "valve") return `${d.current_temperature ?? "–"}° → ${d.temperature ?? "–"}°${bat}`;
  if (d.kind === "sensor") return (d.state === "on" ? "open" : "closed") + bat;
  if (d.kind === "dehumidifier") return dehumValue(d);
  if (d.kind === "media") return tvValue(d);
  if (d.kind === "plug" && d.state === "on" && d.power != null) return `on · ${fmtW(d.power)}`;
  return d.state;
}

// ---------- power / battery ----------
const fmtW = (w) => `${w >= 100 ? Math.round(w) : Math.round(w * 10) / 10} W`;
const batteryWarn = (d) => !!d && (d.battery_low === true || (d.battery != null && d.battery < 20));
function totalPower() {
  let w = 0, any = false;
  for (const d of st.devices.values()) if (d.kind === "plug" && d.power != null) { w += d.power; any = true; }
  return any ? w : null;
}
function batteryText(d) {
  if (d.battery != null) return `Battery ${d.battery} %${batteryWarn(d) ? " — low" : ""}`;
  if (d.battery_low != null) return d.battery_low ? "Battery low" : "Battery OK";
  return "";
}

async function loadDevices() {
  try {
    const list = await api("/api/devices");
    st.devices = new Map(list.map((d) => [d.entity_id, d]));
    $("status").classList.remove("offline");
    st.updatedAt = new Date(); updateStatus();
    offlineStatus();
  } catch (e) { if (offline) offlineStatus(); else setStatus(`Home Assistant: ${e.message}`, true); }
  render();
  if (st.sheetFor) renderSheet();
}

// ---------- geometry ----------
function computeViewBox() {
  const vb = baseViewBox();
  return typeof planZoomBox === "function" ? planZoomBox(vb) : vb; // zoom.js: desktop pinch / Ctrl + wheel
}
function baseViewBox() {
  if (typeof roomViewBox === "function") { const vb = roomViewBox(); if (vb) return vb; } // roomview.js
  const L = cur(); const pts = [];
  for (const r of L.rooms) pts.push([r.x, r.y], [r.x + r.w, r.y + r.h]);
  for (const p of L.placements) pts.push([p.x, p.y]);
  if (!pts.length) return [-0.5, -0.5, 12, 10];
  let x0 = Math.min(...pts.map((p) => p[0])), y0 = Math.min(...pts.map((p) => p[1]));
  let x1 = Math.max(...pts.map((p) => p[0])), y1 = Math.max(...pts.map((p) => p[1]));
  if (!st.editing) return [x0 - 0.5, y0 - 0.5, Math.max(x1 - x0, 2) + 1, Math.max(y1 - y0, 2) + 1];
  // Edit mode: leave room around the plan to draw new rooms (at least the 12 × 10 m default).
  const pad = 3, w = Math.max(x1 - x0 + pad * 2, 12), h = Math.max(y1 - y0 + pad * 2, 10);
  return [(x0 + x1) / 2 - w / 2, (y0 + y1) / 2 - h / 2, w, h];
}
function svgPoint(clientX, clientY) {
  const p = new DOMPoint(clientX, clientY).matrixTransform(svg.getScreenCTM().inverse());
  return { x: p.x, y: p.y };
}
function overSvg(clientX, clientY) {
  const r = svg.getBoundingClientRect();
  return clientX >= r.left && clientX <= r.right && clientY >= r.top && clientY <= r.bottom;
}

// ---------- rendering ----------
function el(tag, attrs = {}, parent) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}
function render() {
  if (!st.drag) st.viewBox = computeViewBox();
  svg.setAttribute("viewBox", st.viewBox.join(" "));
  const L = cur();
  const roomsG = $("rooms"), markersG = $("markers");
  roomsG.replaceChildren(); markersG.replaceChildren();
  const tapsG = $("roomTaps") || roomsG; if (tapsG !== roomsG) tapsG.replaceChildren();

  for (const r of L.rooms) {
    const g = el("g", { class: "room" + (st.sel?.type === "room" && st.sel.id === r.id ? " sel" : ""), "data-room": r.id }, roomsG);
    if (r.cut) el("polygon", { points: roomPoly(r).map((p) => p.join(",")).join(" ") }, g);
    else el("rect", { x: r.x, y: r.y, width: r.w, height: r.h, rx: 0.05 }, g);
    const lp = labelPos(r);
    const t = el("text", { x: lp.x + 0.15, y: lp.y + 0.42 }, g); t.textContent = r.name;
    if (st.editing) { const d = el("text", { x: lp.x + 0.15, y: lp.y + 0.72, class: "dim" }, g); d.textContent = `${fmtLen(r.w)} × ${fmtLen(r.h)}`; }
  }
  if (typeof renderRoomTemps === "function") renderRoomTemps(roomsG);
  if (typeof renderRoomHumidity === "function") renderRoomHumidity(roomsG);
  renderRoomLabels(tapsG); // above the furniture: a linked appliance under a room name never steals its tap
  renderOpenings();
  if (typeof renderSnap === "function") renderSnap();
  const R = 0.26 * (st.markerScale || 1); // wall mode draws bigger markers
  const LO = 0.22 * (st.labelScale || 1); // room view sizes marker labels to the zoom
  for (const p of L.placements) {
    const d = st.devices.get(p.entity_id);
    if (d?.hidden) continue; // placement kept: unhiding brings the marker back
    if (typeof applianceHidesMarker === "function" && applianceHidesMarker(p.entity_id)) continue; // the appliance is the control
    if (typeof tvHidesMarker === "function" && tvHidesMarker(p.entity_id)) continue; // so is a linked TV (tv.js)
    const kind = d?.kind || "light";
    const g = el("g", { class: "marker" + (st.sel?.type === "dev" && st.sel.id === p.entity_id ? " sel" : ""), "data-dev": p.entity_id }, markersG);
    el("circle", { cx: p.x, cy: p.y, r: R, fill: deviceColor(d) }, g);
    const u = el("use", { href: `#ic-${iconKind(d, kind)}`, x: p.x - R * 0.65, y: p.y - R * 0.65, width: R * 1.3, height: R * 1.3 }, g);
    if (iconFill(d)) u.style.fill = iconFill(d);
    if (kind === "valve" && d?.current_temperature != null) {
      const t = el("text", { x: p.x, y: p.y + R + LO }, g); t.textContent = `${d.current_temperature}°`;
    }
    if (kind === "dehumidifier" && d?.current_humidity != null) {
      const t = el("text", { x: p.x, y: p.y + R + LO }, g); t.textContent = `${Math.round(d.current_humidity)} %`;
    }
    if (kind === "plug" && d?.state === "on" && d.power != null) {
      const t = el("text", { x: p.x, y: p.y + R + LO }, g); t.textContent = fmtW(d.power);
    }
    if (batteryWarn(d)) el("circle", { class: "batwarn", cx: p.x + R * 0.75, cy: p.y - R * 0.75, r: R * 0.3 }, g);
    const title = el("title", {}, g); title.textContent = `${d?.name || p.entity_id} — ${deviceValue(d)}`;
  }
  if (st.drawRect) {
    const r = st.drawRect;
    el("rect", { class: "draw-preview", x: r.x, y: r.y, width: r.w, height: r.h }, markersG);
    const t = el("text", { class: "draw-label", x: r.x + r.w / 2, y: r.y + r.h / 2 }, markersG);
    t.textContent = `${fmtLen(r.w)} × ${fmtLen(r.h)}`;
  }
  if (st.editing && st.sel?.type === "room") {
    const r = L.rooms.find((r) => r.id === st.sel.id);
    if (r) {
      const mpp = st.viewBox[2] / (svg.clientWidth || 800); // metres per screen pixel
      const hs = 6 * mpp, hit = 18 * mpp; // 12px handle, 36px touch target
      const hg = el("g", { class: "handles" }, markersG);
      for (const dir of ["nw", "n", "ne", "e", "se", "s", "sw", "w"]) {
        const hx = r.x + (dir.includes("w") ? 0 : dir.includes("e") ? r.w : r.w / 2);
        const hy = r.y + (dir.includes("n") ? 0 : dir.includes("s") ? r.h : r.h / 2);
        const g = el("g", { class: `handle h-${dir}`, "data-h": dir }, hg);
        el("rect", { class: "hit", x: hx - hit, y: hy - hit, width: hit * 2, height: hit * 2 }, g);
        el("rect", { x: hx - hs, y: hy - hs, width: hs * 2, height: hs * 2 }, g);
      }
    }
  }
  if (typeof renderFurniture === "function") renderFurniture(markersG);
  renderFloorplanHandles(markersG);
  if (typeof renderRoomView === "function") renderRoomView();
  renderSide();
  $("deleteSel").disabled = !st.sel;
  $("editRoom").disabled = st.sel?.type !== "room";
}

// TVs and speakers share kind "media" but not an icon (tv.js).
const iconKind = (d, kind) => kind === "media" && typeof mediaIcon === "function" ? mediaIcon(d) : kind;
function icon(kind, color) {
  const s = document.createElementNS(NS, "svg"); s.setAttribute("viewBox", "0 0 24 24");
  el("use", { href: `#ic-${kind}`, fill: color }, s);
  return s;
}
function renderSide() {
  const list = $("list"); list.replaceChildren();
  const placed = new Set(cur().placements.map((p) => p.entity_id));
  for (const f of cur().furniture || []) if (f.media) placed.add(f.media); // shown by its TV furniture (tv.js)
  let devs = [...st.devices.values()].filter((d) => !d.hidden);
  if (st.editing) {
    devs = devs.filter((d) => !placed.has(d.entity_id));
    $("sideTitle").textContent = "Unplaced devices";
    $("sideHint").textContent = devs.length ? "Drag onto the plan, or tap one then tap the plan." : "Everything is placed.";
  } else {
    $("sideTitle").textContent = "Devices";
    $("sideHint").textContent = st.devices.size ? "" : "No devices yet.";
    if (typeof roomSideFilter === "function") devs = roomSideFilter(devs);
  }
  for (const kind of KIND_ORDER) {
    const group = devs.filter((d) => d.kind === kind);
    if (!group.length) continue;
    const h = document.createElement("li"); h.className = "group"; h.textContent = KIND_LABEL[kind]; list.appendChild(h); if (!st.editing) groupActions(kind, h);
    for (const d of group) {
      const li = document.createElement("li");
      li.dataset.dev = d.entity_id;
      if (st.picked === d.entity_id) li.classList.add("picked");
      const af = !st.editing && typeof applianceFor === "function" ? applianceFor(d.entity_id) : null; // linked appliance
      li.appendChild(af ? applianceIcon(af, d) : icon(iconKind(d, kind), deviceColor(d)));
      const n = document.createElement("span"); n.className = "name"; n.textContent = d.name; li.appendChild(n);
      if (af && d.name !== applName(af)) { const a = document.createElement("span"); a.className = "appl-of"; a.textContent = ` · ${applName(af)}`; n.appendChild(a); }
      const v = document.createElement("span"); v.className = "val"; v.textContent = af ? applianceText(af, d) : deviceValue(d); li.appendChild(v);
      list.appendChild(li);
    }
  }
}

// ---------- sheet ----------
let tempTimer = null;
function openSheet(eid) { st.sheetFor = eid; $("sheet").hidden = false; renderSheet(); }
function closeSheet() { st.sheetFor = null; $("sheet").hidden = true; }
function renderSheet() {
  const d = st.devices.get(st.sheetFor); const c = $("sheetContent"); c.replaceChildren();
  if (st.sheetFor === HEATING) return renderHeating(c);
  if (st.sheetFor === DOORS) return renderDoors(c);
  if (st.sheetFor === ENERGY) return renderEnergy(c);
  if (st.sheetFor === HIDDEN) return renderHidden(c);
  if (st.sheetFor === ACTIVITY) return renderActivity(c);
  if (st.sheetFor === WEATHER) return renderWeather(c);
  if (!d) { c.textContent = "Device not found in Home Assistant."; return; }
  const h = document.createElement("h3"); h.textContent = d.name; c.appendChild(h);
  const sub = document.createElement("div"); sub.className = "sub"; sub.textContent = `${d.model || d.kind} · ${d.entity_id}`; c.appendChild(sub);
  const unavailable = d.state === "unavailable" || d.state === "unknown";

  if (d.kind === "light" || d.kind === "plug") {
    const b = document.createElement("button"); b.className = "big" + (d.state === "on" ? " on" : "");
    b.textContent = unavailable ? d.state : d.state === "on" ? "On — tap to turn off" : "Off — tap to turn on";
    b.disabled = unavailable;
    b.onclick = () => { if (typeof confirmOff !== "function" || confirmOff(d)) toggle(d); };
    c.appendChild(b);
    if (typeof applianceSheet === "function") applianceSheet(d, c);
    extraSheet(d, c, unavailable);
    if (d.kind === "plug" && (d.power != null || d.energy_today != null)) {
      const pw = document.createElement("div"); pw.className = "sub"; pw.style.marginTop = "12px";
      pw.textContent = [d.power != null ? `Now ${fmtW(d.power)}` : "", d.energy_today != null ? `Today ${d.energy_today.toFixed(2)} kWh${costText(d.energy_today)}` : ""]
        .filter(Boolean).join(" · ");
      c.appendChild(pw);
    }
    if (d.kind === "plug" && typeof standbyRow === "function") standbyRow(d, c);
  } else if (d.kind === "valve") {
    const cur = document.createElement("div"); cur.className = "sub";
    cur.textContent = `Current ${d.current_temperature ?? "–"}°C · ${d.state}`; c.appendChild(cur);
    const row = document.createElement("div"); row.className = "temp";
    const minus = document.createElement("button"); minus.textContent = "−";
    const plus = document.createElement("button"); plus.textContent = "+";
    const val = document.createElement("div"); val.className = "target";
    const step = d.target_temp_step || 0.5;
    const lo = d.min_temp ?? 5, hi = d.max_temp ?? 30;
    val.textContent = `${d.temperature ?? "–"}°`;
    const bump = (dir) => {
      const t = Math.min(hi, Math.max(lo, Math.round(((d.temperature ?? 20) + dir * step) / step) * step));
      d.temperature = t; val.textContent = `${t}°`; render();
      clearTimeout(tempTimer);
      tempTimer = setTimeout(() => { tempTimer = null; setTemp(d.entity_id, t); }, 700);
    };
    minus.onclick = () => bump(-1); plus.onclick = () => bump(1);
    minus.disabled = plus.disabled = unavailable;
    row.append(minus, val, plus); c.appendChild(row);
    const note = document.createElement("div"); note.className = "sub"; note.style.marginTop = "12px"; note.textContent = "Target temperature"; c.appendChild(note);
  } else if (d.kind === "dehumidifier") {
    dehumSheet(d, c, unavailable);
  } else if (d.kind === "media") {
    tvSheet(d, c, unavailable); // tv.js: a tap opens this, never a blind toggle
  } else if (d.kind === "sensor") {
    const b = document.createElement("span"); b.className = "badge";
    b.style.background = deviceColor(d); b.textContent = unavailable ? d.state : d.state === "on" ? "Open" : "Closed";
    c.appendChild(b);
  }
  if (batteryText(d)) {
    const bt = document.createElement("div"); bt.className = "sub" + (batteryWarn(d) ? " warn" : ""); bt.style.marginTop = "12px";
    bt.textContent = `🔋 ${batteryText(d)}`; c.appendChild(bt);
  }
  historySection(d, c);
  deviceMeta(d, c);
}
// Lights and plugs toggle straight away; valves and sensors open their sheet (and so does a fridge's plug).
function tapDevice(eid) {
  const d = st.devices.get(eid);
  if (typeof applianceTapOpensSheet === "function" && applianceTapOpensSheet(eid)) openSheet(eid);
  else if ((d?.kind === "light" || d?.kind === "plug") && d.state !== "unavailable" && d.state !== "unknown") toggle(d);
  else openSheet(eid);
}
async function toggle(d) {
  const prev = d.state;
  d.state = d.state === "on" ? "off" : "on"; render(); if (st.sheetFor) renderSheet();
  try { await api(`/api/devices/${encodeURIComponent(d.entity_id)}/toggle`, { method: "POST" }); }
  catch (e) { d.state = prev; render(); if (st.sheetFor) renderSheet(); setStatus(`Toggle failed: ${e.message}`, true); return; }
  if (!st.live) setTimeout(loadDevices, 800);
}
async function setTemp(eid, t) {
  try { await api(`/api/devices/${encodeURIComponent(eid)}/temperature`, { method: "POST", body: JSON.stringify({ temperature: t }) }); setStatus(`Target set to ${t}°`); }
  catch (e) { setStatus(`Set temperature failed: ${e.message}`, true); }
  if (!st.live) setTimeout(loadDevices, 1000);
}

// ---------- edit mode ----------
function setEditing(on) {
  if (typeof resetPlanZoom === "function") resetPlanZoom(); // zoom.js: edit mode has its own view
  st.editing = on; st.sel = null; st.picked = null; st.drawing = false; st.drawRect = null; st.adding = null;
  $("addRoom").classList.remove("primary"); $("addRoom").textContent = "+ Room"; document.body.classList.remove("drawing");
  st.draft = on ? clone(st.layout) : null;
  document.body.classList.toggle("editing", on);
  $("editbar").hidden = !on; $("editToggle").hidden = on;
  closeSheet(); render();
}
function placeDevice(eid, pt) {
  st.draft.placements = st.draft.placements.filter((p) => p.entity_id !== eid);
  st.draft.placements.push({ entity_id: eid, x: snap(pt.x), y: snap(pt.y) });
  st.picked = null; st.sel = { type: "dev", id: eid }; render();
}

function roomDialog(room, drawn) {
  const dlg = $("roomDialog"), f = $("roomForm");
  $("roomDialogTitle").textContent = room ? "Edit room" : "Add room";
  dlg.querySelectorAll(".u").forEach((s) => (s.textContent = unit()));
  f.name.value = room?.name || "";
  f.w.value = toDisp(room?.w ?? drawn?.w ?? 4).toFixed(1); f.h.value = toDisp(room?.h ?? drawn?.h ?? 3).toFixed(1);
  dlg.onclose = () => {
    if (dlg.returnValue !== "ok") return;
    const name = f.name.value.trim(), w = fromDisp(+f.w.value), h = fromDisp(+f.h.value);
    if (!name || !(w > 0) || !(h > 0)) return;
    if (room) Object.assign(room, { name, w: snap(w) || SNAP, h: snap(h) || SNAP });
    else {
      const L = st.draft;
      const x = drawn ? drawn.x : L.rooms.length ? snap(Math.max(...L.rooms.map((r) => r.x + r.w))) : 0;
      const y = drawn ? drawn.y : 0;
      const id = "r" + Date.now().toString(36) + Math.random().toString(36).slice(2, 5);
      L.rooms.push({ id, name, x, y, w: snap(w) || SNAP, h: snap(h) || SNAP });
      st.sel = { type: "room", id };
    }
    render();
  };
  dlg.returnValue = ""; dlg.showModal();
  f.name.focus();
}

// pointer handling on the plan
svg.addEventListener("pointerdown", (e) => {
  const mk = e.target.closest(".marker"), rm = e.target.closest(".room");
  if (!st.editing) return;
  const pt = svgPoint(e.clientX, e.clientY);
  st.tidy = null;
  if (floorplanPointerDown(e, pt)) return;
  if (st.drawing) {
    const p0 = snapDrawStart(pt, e.altKey);
    st.drag = { draw: p0, pid: e.pointerId };
    st.drawRect = { x: p0.x, y: p0.y, w: 0, h: 0 };
    svg.setPointerCapture(e.pointerId);
    render();
    return;
  }
  const hd = e.target.closest(".handle");
  if (hd && st.sel?.type === "room") {
    const r = st.draft.rooms.find((r) => r.id === st.sel.id);
    st.drag = { resize: hd.dataset.h, obj: r, start: pt, orig: { ...r }, moved: false, pid: e.pointerId };
    svg.setPointerCapture(e.pointerId);
    return;
  }
  if (st.picked && !mk) { placeDevice(st.picked, pt); return; }
  if (!mk && typeof furniturePointerDown === "function" && furniturePointerDown(e, pt)) return;
  let target = null;
  if (mk) { const p = st.draft.placements.find((p) => p.entity_id === mk.dataset.dev); target = { type: "dev", id: p.entity_id, obj: p }; }
  else if (rm) { const r = st.draft.rooms.find((r) => r.id === rm.dataset.room); target = { type: "room", id: r.id, obj: r }; }
  if (!target) { st.sel = null; render(); return; }
  st.sel = { type: target.type, id: target.id };
  st.drag = { obj: target.obj, start: pt, ox: target.obj.x, oy: target.obj.y, moved: false, pid: e.pointerId };
  // Moving a room moves the devices inside it.
  if (target.type === "room") {
    const r = target.obj;
    st.drag.carried = st.draft.placements.filter((p) => inRoom(r, p))
      .map((p) => ({ p, ox: p.x, oy: p.y }));
    st.drag.carried.push(...openingsOnRoom(r).map((p) => ({ p, ox: p.x, oy: p.y })));
    if (typeof furnitureInRoom === "function") st.drag.carried.push(...furnitureInRoom(r).map((p) => ({ p, ox: p.x, oy: p.y })));
  }
  svg.setPointerCapture(e.pointerId);
  render();
});
svg.addEventListener("pointermove", (e) => {
  const d = st.drag; if (!d || e.pointerId !== d.pid) return;
  const pt = svgPoint(e.clientX, e.clientY);
  d.alt = e.altKey; // Alt: no snapping to other rooms for this move
  if (d.draw) {
    st.drawRect = snapDrawRect(d.draw, pt, e.altKey);
    render();
    return;
  }
  const dx = pt.x - d.start.x, dy = pt.y - d.start.y;
  if (!d.moved && Math.hypot(dx, dy) < 0.08) return;
  d.moved = true;
  if (floorplanPointerMove(d, pt, dx, dy)) { render(); return; }
  if (d.fur) { furniturePointerMove(d, pt, dx, dy); render(); return; }
  if (d.resize) { resizeRoom(d.obj, d.orig, d.resize, dx, dy); snapResize(d, dx, dy); clampCut(d.obj); render(); return; }
  if (!d.carried) { d.obj.x = snap(d.ox + dx); d.obj.y = snap(d.oy + dy); render(); return; }
  snapMove(d, dx, dy); // a room snaps to the others; what it carries moves by the same amount
  const mx = d.obj.x - d.ox, my = d.obj.y - d.oy;
  for (const c of d.carried) { c.p.x = +(c.ox + mx).toFixed(3); c.p.y = +(c.oy + my).toFixed(3); }
  render();
});
const MIN_ROOM = 0.3;
function resizeRoom(r, o, dir, dx, dy) {
  if (dir.includes("e")) r.w = Math.max(MIN_ROOM, snap(o.w + dx));
  if (dir.includes("s")) r.h = Math.max(MIN_ROOM, snap(o.h + dy));
  if (dir.includes("w")) { const x = Math.min(snap(o.x + dx), o.x + o.w - MIN_ROOM); r.w = o.x + o.w - x; r.x = x; }
  if (dir.includes("n")) { const y = Math.min(snap(o.y + dy), o.y + o.h - MIN_ROOM); r.h = o.y + o.h - y; r.y = y; }
  r.w = +r.w.toFixed(3); r.h = +r.h.toFixed(3);
}
const endDrag = (e) => {
  if (!st.drag || e.pointerId !== st.drag.pid) return;
  const wasDraw = st.drag.draw, rect = st.drawRect;
  st.drag = null; st.drawRect = null; st.snapGuides = null;
  if (wasDraw) {
    setDrawing(false);
    if (e.type === "pointerup" && rect.w >= MIN_ROOM && rect.h >= MIN_ROOM) roomDialog(null, rect);
    else setStatus("Room too small — drag out a bigger rectangle");
    return;
  }
  render();
};
function setDrawing(on) {
  st.drawing = on;
  $("addRoom").classList.toggle("primary", on);
  $("addRoom").textContent = on ? "Drag on the plan…" : "+ Room";
  document.body.classList.toggle("drawing", on);
  if (on) { st.sel = null; st.picked = null; setAdding(null); }
  render();
}
svg.addEventListener("pointerup", endDrag);
svg.addEventListener("pointercancel", endDrag);
svg.addEventListener("dblclick", (e) => {
  if (!st.editing) return;
  const rm = e.target.closest(".room");
  if (rm) roomDialog(st.draft.rooms.find((r) => r.id === rm.dataset.room));
});
svg.addEventListener("click", (e) => {
  if (st.editing) return;
  const mk = e.target.closest(".marker");
  if (mk) { tapDevice(mk.dataset.dev); return; }
  const ap = e.target.closest("[data-appl]"); // a linked appliance (appliances.js)
  if (ap) tapAppliance(ap.dataset.appl);
});

// palette / list
let pal = null;
$("list").addEventListener("pointerdown", (e) => {
  const li = e.target.closest("li[data-dev]"); if (!li) return;
  if (!st.editing) return;
  e.preventDefault();
  pal = { eid: li.dataset.dev, x: e.clientX, y: e.clientY, ghost: null };
});
document.addEventListener("pointermove", (e) => {
  if (!pal) return;
  if (!pal.ghost && Math.hypot(e.clientX - pal.x, e.clientY - pal.y) > 6) {
    pal.ghost = document.createElement("div"); pal.ghost.className = "ghost"; document.body.appendChild(pal.ghost);
  }
  if (pal.ghost) { pal.ghost.style.left = e.clientX + "px"; pal.ghost.style.top = e.clientY + "px"; }
});
document.addEventListener("pointerup", (e) => {
  if (!pal) return;
  const p = pal; pal = null;
  if (p.ghost) {
    p.ghost.remove();
    if (overSvg(e.clientX, e.clientY)) placeDevice(p.eid, svgPoint(e.clientX, e.clientY));
  } else {
    st.picked = st.picked === p.eid ? null : p.eid; render();
  }
});
document.addEventListener("pointercancel", () => { if (pal?.ghost) pal.ghost.remove(); pal = null; });
$("list").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-dev]");
  if (li && !st.editing) tapDevice(li.dataset.dev);
});

// toolbar
$("editToggle").onclick = () => setEditing(true);
$("cancelEdit").onclick = () => setEditing(false);
$("addRoom").onclick = () => setDrawing(!st.drawing);
$("editRoom").onclick = () => st.sel?.type === "room" && roomDialog(st.draft.rooms.find((r) => r.id === st.sel.id));
$("deleteSel").onclick = () => {
  if (!st.sel) return;
  if (st.sel.type === "room") {
    const r = st.draft.rooms.find((r) => r.id === st.sel.id);
    if (!confirm(`Delete room "${r.name}"? Devices inside stay where they are.`)) return;
    st.draft.rooms = st.draft.rooms.filter((x) => x.id !== st.sel.id);
  } else if (st.sel.type === "fur") {
    deleteFurniture(st.sel.id);
  } else if (st.sel.type === "open") {
    st.draft.openings = (st.draft.openings || []).filter((o) => o.id !== st.sel.id);
  } else {
    st.draft.placements = st.draft.placements.filter((p) => p.entity_id !== st.sel.id);
  }
  st.sel = null; render();
};
$("save").onclick = async () => {
  $("save").disabled = true;
  // Settings (keep on, names, hidden, tariff) may have changed since the draft was taken: send the current ones.
  const body = st.layout.settings ? { ...st.draft, settings: st.layout.settings } : st.draft;
  try {
    st.layout = await api("/api/layout", { method: "PUT", body: JSON.stringify(body) }); setEditing(false); setStatus("Saved");
    if (typeof loadAppliances === "function") loadAppliances(); // links may have changed
  }
  catch (e) { setStatus(`Save failed: ${e.message}`, true); }
  finally { $("save").disabled = false; }
};
$("refresh").onclick = async () => {
  try { await api("/api/devices/refresh", { method: "POST" }); } catch (e) { setStatus(e.message, true); }
  loadDevices();
};
$("unit").onchange = async (e) => {
  cur().unit = e.target.value;
  if (!st.editing) { // persist the preference straight away
    try { st.layout = await api("/api/layout", { method: "PUT", body: JSON.stringify(st.layout) }); } catch (err) { setStatus(err.message, true); }
  }
  render();
};
$("sheetClose").onclick = closeSheet;
$("sheet").addEventListener("click", (e) => { if (e.target.id === "sheet") closeSheet(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeSheet(); if (st.drawing) setDrawing(false); if (st.adding) setAdding(null); }
  if (st.editing && (e.key === "Delete" || e.key === "Backspace") && st.sel && !document.querySelector("dialog[open]")) $("deleteSel").click();
});

// ---------- live updates (server-sent events) ----------
function updateStatus() {
  const w = totalPower();
  // Most useful first: the status line is truncated on narrow phones.
  const parts = [];
  if (w != null) parts.push(`⚡ ${fmtW(w)}`);
  if (st.live && st.ws !== false) parts.push("● live");
  else if (st.live) parts.push("↻ 10 s");
  else if (st.updatedAt) parts.push(st.updatedAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
  parts.push(`${[...st.devices.values()].filter((d) => d.kind !== "person").length} devices`);  // people (Auto Away) aren't devices
  setStatus(parts.join(" · "));
  $("status").classList.toggle("live", !!st.live && st.ws !== false);
}
function applyDevices() { if (!st.drag) render(); else renderSide(); if (st.sheetFor) renderSheet(); updateStatus(); }
function startLive() {
  if (!window.EventSource) return;
  const es = new EventSource("/api/events");
  es.addEventListener("snapshot", (e) => {
    const list = JSON.parse(e.data);
    st.devices = new Map(list.map((d) => [d.entity_id, d]));
    st.live = true; st.updatedAt = new Date(); applyDevices();
  });
  es.addEventListener("device", (e) => {
    const d = JSON.parse(e.data);
    st.devices.set(d.entity_id, d); st.updatedAt = new Date();
    // Don't redraw the sheet mid-adjustment of a valve target; the pending set will confirm it.
    if ((st.sheetFor === d.entity_id || st.sheetFor === HEATING) && (st.holdSheet || (tempTimer && d.kind === "valve"))) { render(); updateStatus(); return; }
    applyDevices();
  });
  es.addEventListener("status", (e) => { st.ws = !!JSON.parse(e.data).ws; updateStatus(); });
  if (typeof applianceEvents === "function") applianceEvents(es); // washer cycles etc.
  es.onopen = () => { st.live = true; updateStatus(); };
  // EventSource retries by itself; polling covers the gap.
  es.onerror = () => { if (st.live) { st.live = false; updateStatus(); loadDevices(); } };
}

// ---------- boot ----------
// Wait for the later scripts (floorplan, controls, alerts, modes, history): render() calls into them.
document.addEventListener("DOMContentLoaded", async () => {
  try { st.layout = await api("/api/layout"); } catch (e) { setStatus(`Layout: ${e.message}`, true); }
  $("unit").value = st.layout.unit || "m";
  await loadDevices();
  startLive();
  setInterval(() => { if (!st.live && !st.editing && !document.hidden) loadDevices(); }, 5000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !st.editing && !st.live) loadDevices(); });
});
