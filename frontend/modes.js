"use strict";
// "⋯ More" header menu, room temperatures on the plan, Away/Home mode, layout export/import.

// ---------- room temperatures ----------
const TEMP_KEY = "hc.showTemps";
let showTemps = true;
try { showTemps = localStorage.getItem(TEMP_KEY) !== "0"; } catch {}
const TEMP_COLD = [89, 168, 255], TEMP_MID = [150, 156, 168], TEMP_WARM = [255, 138, 61];
// Blue at <=16°, neutral around 19.5°, orange at >=23°.
function tempColor(t) {
  const mix = (a, b, f) => a.map((v, i) => Math.round(v + (b[i] - v) * f));
  const c = t <= 19.5 ? mix(TEMP_COLD, TEMP_MID, Math.min(1, Math.max(0, (t - 16) / 3.5)))
    : mix(TEMP_MID, TEMP_WARM, Math.min(1, (t - 19.5) / 3.5));
  return `rgb(${c.join(",")})`;
}
function roomTemp(r) {
  const ts = st.layout.placements.filter((p) => inRoom(r, p)).map((p) => st.devices.get(p.entity_id))
    .filter((d) => d?.kind === "valve" && typeof d.current_temperature === "number").map((d) => d.current_temperature);
  return ts.length ? ts.reduce((a, b) => a + b, 0) / ts.length : null;
}
function renderRoomTemps(roomsG) {
  if (st.editing || !showTemps) return;
  for (const r of st.layout.rooms) {
    const t = roomTemp(r); if (t == null) continue;
    const g = roomsG.querySelector(`[data-room="${CSS.escape(r.id)}"]`); if (!g) continue;
    const col = tempColor(t);
    const attrs = { class: "temp-tint", style: `fill:${col}`, "data-temp": t.toFixed(1) };
    const tint = r.cut ? el("polygon", { ...attrs, points: roomPoly(r).map((p) => p.join(",")).join(" ") })
      : el("rect", { ...attrs, x: r.x, y: r.y, width: r.w, height: r.h, rx: 0.05 });
    g.insertBefore(tint, g.children[1] || null);
    // Beside the room name when it fits, else just below it.
    const lp = labelPos(r), name = g.querySelector("text");
    let nw = 0; try { nw = name ? name.getComputedTextLength() : 0; } catch {}
    const right = r.cut && labelPos(r).y === r.y && r.cut.corner === "ne" ? r.x + r.w - r.cut.w : r.x + r.w;
    const beside = nw > 0 && lp.x + 0.15 + nw + 0.95 <= right;
    const tx = el("text", { class: "temp-label", x: beside ? lp.x + 0.3 + nw : lp.x + 0.15, y: beside ? lp.y + 0.42 : lp.y + 0.78, style: `fill:${col}` }, g);
    tx.textContent = `${Math.round(t * 10) / 10}°`;
  }
}

// ---------- More menu ----------
(() => {
  const menu = $("moreMenu"), btn = $("moreBtn");
  const open = (on) => {
    menu.hidden = !on; btn.setAttribute("aria-expanded", on ? "true" : "false"); btn.classList.toggle("on-menu", on);
    if (on) { const b = btn.getBoundingClientRect(); menu.style.top = `${b.bottom + 6}px`; menu.style.right = `${Math.max(8, innerWidth - b.right)}px`; }
  };
  btn.onclick = (e) => { e.stopPropagation(); open(menu.hidden); };
  document.addEventListener("pointerdown", (e) => { if (!menu.hidden && !menu.contains(e.target) && !btn.contains(e.target)) open(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !menu.hidden) { open(false); btn.focus(); } });
  window.addEventListener("resize", () => open(false));
  // Buttons close the menu after acting (their own handlers still run: app.js binds #refresh/#signOut).
  menu.addEventListener("click", (e) => { if (e.target.closest("button")) open(false); });

  const tt = $("tempToggle"); tt.checked = showTemps;
  tt.onchange = () => { showTemps = tt.checked; try { localStorage.setItem(TEMP_KEY, showTemps ? "1" : "0"); } catch {} render(); };
})();

// ---------- Away / Home ----------
const modeSt = { mode: "home", away_temp: 16 };
const li = (text) => { const e = document.createElement("li"); e.textContent = text; return e; };
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
function applyMode(m) {
  Object.assign(modeSt, m);
  const away = modeSt.mode === "away";
  $("awayBadge").hidden = !away;
  if (away && modeSt.since) $("awayBadge").title = `Away since ${new Date(modeSt.since).toLocaleString()}`;
  $("awayBtn").textContent = away ? "I'm home" : "Away…";
}
async function loadMode() { try { applyMode(await api("/api/mode")); } catch {} }
function modeDialog() {
  const away = modeSt.mode !== "away", f = $("modeForm"), list = $("modeList"); list.replaceChildren();
  const devs = [...st.devices.values()], keep = new Set(st.layout.settings?.keep_on || []);
  const onOff = devs.filter((d) => (d.kind === "light" || d.kind === "plug") && !keep.has(d.entity_id));
  const valves = devs.filter((d) => d.kind === "valve");
  $("modeTitle").textContent = away ? "Leaving home?" : "Back home?";
  $("awayTempRow").hidden = !away;
  f.away_temp.value = modeSt.away_temp;
  const items = away ? [
    `Turn off all lights and plugs (${onOff.length})` + (keep.size ? ` (${keep.size} “keep on” stay on)` : ""),
    `Set ${plural(valves.length, "radiator")} to the away temperature (current targets are remembered)`,
    "Turn door alerts on",
  ] : [
    `Put ${plural(valves.length, "radiator")} back to their previous targets`,
    "Restore the door alerts setting",
    "Lights stay off",
  ];
  items.forEach((t) => list.appendChild(li(t)));
  $("modeOk").textContent = away ? "Go away" : "I'm home";
  const dlg = $("modeDialog");
  dlg.onclose = async () => {
    if (dlg.returnValue !== "ok") return;
    try {
      if (away) {
        const t = +f.away_temp.value;
        if (!(t >= 5 && t <= 25)) { setStatus("Away temperature must be 5–25°", true); return; }
        if (t !== modeSt.away_temp) applyMode(await api("/api/mode/settings", { method: "PUT", body: JSON.stringify({ away_temp: t }) }));
      }
      const r = await api("/api/mode", { method: "POST", body: JSON.stringify({ mode: away ? "away" : "home" }) });
      applyMode(r);
      setStatus(away ? `Away: ${plural(r.turned_off.length, "device")} off, radiators ${r.away_temp}°`
        : `Welcome home: ${plural(Object.keys(r.valves).length, "radiator")} restored`);
      if (!st.live) setTimeout(loadDevices, 800);
    } catch (e) { setStatus(`${away ? "Away" : "Home"} failed: ${e.message}`, true); }
  };
  dlg.returnValue = ""; dlg.showModal();
}
$("awayBtn").onclick = modeDialog;
loadMode();
document.addEventListener("visibilitychange", () => { if (!document.hidden) loadMode(); });

// ---------- layout export / import ----------
$("exportLayout").onclick = async () => {
  try {
    const L = await api("/api/layout");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([JSON.stringify(L, null, 2)], { type: "application/json" }));
    a.download = `homecontrol-layout-${new Date().toISOString().slice(0, 10)}.json`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    setStatus("Layout exported");
  } catch (e) { setStatus(`Export failed: ${e.message}`, true); }
};
$("importLayout").onclick = () => {
  if (st.editing) { setStatus("Finish editing first", true); return; }
  $("importFile").value = ""; $("importFile").click();
};
const refsOf = (L) => [...(L.placements || []).map((p) => p.entity_id), ...(L.openings || []).map((o) => o.entity_id).filter(Boolean),
  ...(L.settings?.keep_on || []), ...Object.keys(L.settings?.names || {}), ...(L.settings?.hidden || [])];
function stripUnknown(L) {
  const ok = (e) => st.devices.has(e);
  const out = { ...L, placements: (L.placements || []).filter((p) => ok(p.entity_id)),
    openings: (L.openings || []).map((o) => { if (!o.entity_id || ok(o.entity_id)) return o; const c = { ...o }; delete c.entity_id; return c; }) };
  if (L.settings) {
    out.settings = { ...L.settings, keep_on: (L.settings.keep_on || []).filter(ok) };
    if (L.settings.names) out.settings.names = Object.fromEntries(Object.entries(L.settings.names).filter(([e]) => ok(e)));
    if (Array.isArray(L.settings.hidden)) out.settings.hidden = L.settings.hidden.filter(ok);
  }
  return out;
}
$("importFile").onchange = async () => {
  const file = $("importFile").files[0]; if (!file) return;
  let L;
  try { L = JSON.parse(await file.text()); if (!L || typeof L !== "object" || Array.isArray(L)) throw new Error("not a layout object"); }
  catch (e) { setStatus(`Import: ${file.name} isn't a valid layout (${e.message})`, true); return; }
  const unknown = [...new Set(refsOf(L))].filter((e) => typeof e !== "string" || !st.devices.has(e));
  const list = $("importList"); list.replaceChildren();
  [plural(Array.isArray(L.rooms) ? L.rooms.length : 0, "room"), `${plural(Array.isArray(L.placements) ? L.placements.length : 0, "placed device")}`,
    `${Array.isArray(L.openings) ? L.openings.length : 0} doors/windows`,
    unknown.length ? `${unknown.length} not in Home Assistant now: ${unknown.join(", ")}` : "All devices are known to Home Assistant"]
    .forEach((t) => list.appendChild(li(t)));
  const msg = $("importMsg"), strip = $("importStrip"), okBtn = $("importOk"), dlg = $("importDialog");
  msg.textContent = "This replaces the current plan."; msg.classList.remove("warn"); strip.hidden = true; okBtn.hidden = false;
  const send = async (data) => {
    okBtn.disabled = strip.disabled = true;
    try {
      st.layout = await api("/api/layout", { method: "PUT", body: JSON.stringify(data) });
      $("unit").value = st.layout.unit || "m"; dlg.close(); render(); setStatus(`Imported ${file.name}`);
    } catch (e) {
      msg.textContent = `Rejected: ${e.message}`; msg.classList.add("warn");
      if (/unknown/i.test(e.message)) { strip.hidden = false; okBtn.hidden = true; }
    } finally { okBtn.disabled = strip.disabled = false; }
  };
  okBtn.onclick = () => send(L);
  strip.onclick = () => send(stripUnknown(L));
  dlg.showModal();
};
