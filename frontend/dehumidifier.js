"use strict";
// Dehumidifier: marker colour/value, its sheet (on/off, target humidity, mode, tank banner, "Include in All off"),
// humidity on room labels, indoor humidity for the wall bar, the running bar under its history chart and the
// tank-full setting in the bell sheet. Hooks are called from app.js, controls.js (allOff), history.js and wall.js.
const DH_STEP = 5;
let dhTimer = null;
const dhMk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const dhPct = (v) => `${Math.round(v)} %`;
const dhUsable = (d) => d && d.state !== "unavailable" && d.state !== "unknown";
// Hold live redraws of the sheet while adjusting, then redraw once so updates that arrived meanwhile show.
function dhHold(eid, ms = 2000) {
  holdSheet(ms);
  setTimeout(() => { if (st.sheetFor === eid && !st.holdSheet) renderSheet(); }, ms + 100);
}
const dhRunning = (d) => d.state === "on" && d.action !== "idle" && d.action !== "off";

// ---------- marker / list ----------
// Red when the tank is full, teal while drying, blue when on but idle (target reached), grey when off.
function dehumColor(d) {
  if (d.tank_full) return "var(--open)";
  if (d.state !== "on") return "var(--off)";
  return dhRunning(d) ? "var(--dry)" : "var(--idle)";
}
function dehumValue(d) {
  if (d.tank_full) return "tank full";
  const parts = [d.state === "on" ? (d.action === "idle" ? "idle" : "on") : d.state];
  if (d.current_humidity != null) parts.push(d.target_humidity != null && d.state === "on"
    ? `${dhPct(d.current_humidity)} → ${dhPct(d.target_humidity)}` : dhPct(d.current_humidity));
  return parts.join(" · ");
}

// ---------- "All off" ----------
const allOffInclude = () => st.layout.settings?.all_off_include || [];
// controls.js allOff(): dehumidifiers only when ticked in their sheet (they usually run unattended).
function dehumInAllOff(d) { return d?.kind === "dehumidifier" && allOffInclude().includes(d.entity_id); }

// ---------- sheet ----------
async function dehumPost(d, what, body, ok) {
  try { await api(`/api/devices/${encodeURIComponent(d.entity_id)}/${what}`, { method: "POST", body: JSON.stringify(body) }); setStatus(ok); }
  catch (e) { setStatus(`${d.name}: ${e.message}`, true); }
  if (!st.live) setTimeout(loadDevices, 1000);
}
function dehumSheet(d, c, unavailable) {
  if (d.tank_full) {
    const b = dhMk("div", "tank-banner"); b.setAttribute("role", "alert");
    b.append(dhMk("b", null, "Tank full — empty it"), dhMk("span", null, "It stops drying until the tank is emptied."));
    c.appendChild(b);
  }
  const now = [];
  if (d.current_humidity != null) now.push(`Now ${dhPct(d.current_humidity)}`);
  if (d.current_temperature != null) now.push(`${d.current_temperature}°`);
  if (d.state === "on" && d.action) now.push(d.action);
  if (now.length) c.appendChild(dhMk("div", "sub dh-now", now.join(" · ")));

  const b = dhMk("button", "big" + (d.state === "on" ? " on dh-on" : ""));
  b.textContent = unavailable ? d.state : d.state === "on" ? "On — tap to turn off" : "Off — tap to turn on";
  b.disabled = unavailable; b.onclick = () => toggle(st.devices.get(d.entity_id) || d);
  c.appendChild(b);

  if (d.control === "humidifier") {
    const lo = d.min_humidity ?? 0, hi = d.max_humidity ?? 100;
    const row = dhMk("div", "temp dh-target"); const minus = dhMk("button", null, "−"), plus = dhMk("button", null, "+");
    minus.setAttribute("aria-label", "Lower target humidity"); plus.setAttribute("aria-label", "Raise target humidity");
    const val = dhMk("div", "target", d.target_humidity != null ? dhPct(d.target_humidity) : "–");
    const bump = (dir) => {
      const base = d.target_humidity ?? Math.round((lo + hi) / 2 / DH_STEP) * DH_STEP;
      const t = Math.min(hi, Math.max(lo, Math.round((base + dir * DH_STEP) / DH_STEP) * DH_STEP));
      d.target_humidity = t; val.textContent = dhPct(t); dhHold(d.entity_id); render();
      clearTimeout(dhTimer);
      dhTimer = setTimeout(() => { dhTimer = null; dehumPost(d, "humidity", { humidity: t }, `${d.name}: target ${t} %`); }, 700);
    };
    minus.onclick = () => bump(-1); plus.onclick = () => bump(1);
    minus.disabled = plus.disabled = unavailable;
    row.append(minus, val, plus); c.appendChild(row);
    c.appendChild(dhMk("div", "sub dh-note", `Target humidity (${lo}–${hi} %)`));
  }
  if (d.available_modes?.length) {
    const lab = dhMk("label", "dh-mode"); lab.appendChild(dhMk("span", null, "Mode"));
    const sel = dhMk("select"); sel.disabled = unavailable;
    for (const m of d.available_modes) sel.appendChild(new Option(m.charAt(0).toUpperCase() + m.slice(1).replace(/_/g, " "), m));
    if (d.mode && !d.available_modes.includes(d.mode)) sel.appendChild(new Option(d.mode, d.mode));
    sel.value = d.mode || "";
    sel.onchange = () => { d.mode = sel.value; dehumPost(d, "mode", { mode: sel.value }, `${d.name}: ${sel.value}`); };
    lab.appendChild(sel); c.appendChild(lab);
  }
  const lab = dhMk("label", "keep-on dh-alloff"); const cb = dhMk("input"); cb.type = "checkbox";
  cb.checked = allOffInclude().includes(d.entity_id);
  lab.append(cb, dhMk("span", null, "Include in “All off”"));
  cb.onchange = async () => {
    const prev = st.layout.settings;
    const set = new Set(allOffInclude()); cb.checked ? set.add(d.entity_id) : set.delete(d.entity_id);
    st.layout.settings = { keep_on: [], ...(prev || {}), all_off_include: [...set] };
    try { st.layout = await api("/api/layout", { method: "PUT", body: JSON.stringify(st.layout) }); setStatus(cb.checked ? `“All off” also turns off ${d.name}` : "Saved"); }
    catch (e) { st.layout.settings = prev; cb.checked = !cb.checked; setStatus(`Save failed: ${e.message}`, true); }
  };
  c.appendChild(lab);
}

// ---------- humidity on the plan / wall bar ----------
function roomHumidity(r) {
  const hs = st.layout.placements.filter((p) => inRoom(r, p)).map((p) => st.devices.get(p.entity_id))
    .filter((d) => d && typeof d.current_humidity === "number").map((d) => d.current_humidity);
  return hs.length ? hs.reduce((a, b) => a + b, 0) / hs.length : null;
}
// After modes.js renderRoomTemps: "62 %" right of the room's temperature label, else right of (or below) the name.
function renderRoomHumidity(roomsG) {
  if (st.editing || (typeof showTemps !== "undefined" && !showTemps)) return;
  for (const r of st.layout.rooms) {
    const h = roomHumidity(r); if (h == null) continue;
    const g = roomsG.querySelector(`[data-room="${CSS.escape(r.id)}"]`); if (!g) continue;
    const lp = labelPos(r), anchor = g.querySelector(".temp-label") || g.querySelector("text");
    let x = lp.x + 0.15, y = lp.y + 0.78;
    try {
      const w = anchor.getComputedTextLength(), ax = +anchor.getAttribute("x"), right = r.x + r.w;
      if (w > 0 && ax + w + 0.95 <= right) { x = ax + w + 0.15; y = +anchor.getAttribute("y"); }
      else if (anchor.classList.contains("temp-label")) { x = ax; y = +anchor.getAttribute("y") + 0.34; }
    } catch {}
    const t = el("text", { class: "hum-label", x, y, "data-hum": h.toFixed(1) }, g);
    t.textContent = dhPct(h);
  }
}
// Wall bar: average over every dehumidifier (and anything else) that reports humidity.
function indoorHumidity() {
  const hs = [...st.devices.values()].filter((d) => typeof d.current_humidity === "number").map((d) => d.current_humidity);
  return hs.length ? hs.reduce((a, b) => a + b, 0) / hs.length : null;
}

// ---------- history: running bar under the humidity chart ----------
function dehumRunBar(body, data) {
  const runs = data.timeline || [];
  if (!runs.length) return;
  const W = Math.max(260, Math.round(body.clientWidth || 340)), PL = 40, PR = 4, H = 26;
  const x = (t) => PL + (t - data.start) / (data.end - data.start) * (W - PL - PR);
  const s = sv("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", class: "hist-chart dh-runs", role: "img", "aria-label": "Running" });
  const lb = sv("text", { x: PL - 5, y: H / 2, class: "tick ylab", "text-anchor": "end", "dominant-baseline": "middle" }, s); lb.textContent = "on";
  sv("rect", { x: PL, y: 4, width: W - PL - PR, height: H - 8, rx: 4, class: "bar-bg" }, s);
  for (const r of runs) {
    if (r.state !== "on") continue;
    sv("rect", { x: x(r.start), y: 4, width: Math.max(1.5, x(r.end) - x(r.start)), height: H - 8, class: "bar dehumidifier on" }, s);
  }
  body.appendChild(s);
  const on = runs.filter((r) => r.state === "on"), ms = on.reduce((a, r) => a + r.end - r.start, 0);
  body.appendChild(dhMk("div", "hist-msg dh-runs-sum", `Running ${fmtDur(ms)} in total · ${on.length} time${on.length === 1 ? "" : "s"}`));
}

// ---------- bell sheet: tank-full push ----------
(() => {
  const sec = document.getElementById("dehumSec"), cb = document.getElementById("dehumTank");
  if (!sec || !cb) return;
  document.getElementById("alertsBtn").addEventListener("click", async () => {
    sec.hidden = ![...st.devices.values()].some((d) => d.kind === "dehumidifier");
    try { cb.checked = (await api("/api/alerts/settings")).dehumidifier_tank !== false; } catch {}
  });
  cb.addEventListener("change", async () => {
    try { await api("/api/alerts/settings", { method: "PUT", body: JSON.stringify({ dehumidifier_tank: cb.checked }) }); }
    catch (e) { cb.checked = !cb.checked; setStatus(`Couldn't save: ${e.message}`, true); }
  });
})();
