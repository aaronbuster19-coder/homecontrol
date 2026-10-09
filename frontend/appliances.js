"use strict";
// Appliances: furniture linked to a smart plug (layout furniture[].plug, hide_marker, thresholds).
// View mode (plan, room view, wall mode): the piece glows while the plug is on and shows its watts and a status read
// from the power ("Boiling…", "Running 47 min"). Tap = toggle the plug, except fridges/freezers and keep-on plugs: those
// open the plug sheet, where switching off asks first. Long-press = plug sheet. The plug's own marker is hidden unless
// the link says otherwise. Edit mode: "Link plug" for a selected appliance, with a one-tap offer to rename the plug.
// Washer / dryer / dishwasher cycles are tracked by the server (GET /api/appliances, SSE "appliances"), which also sends
// the "Washing finished" push. Status rules mirror backend/appliances.py. Nothing here ever switches anything by itself.
// Hooks: renderAppliances, applianceLinked (furniture.js); applianceHidesMarker, applianceFor, applianceIcon,
// applianceText, tapAppliance, confirmOff, applianceSheet, applianceEvents, loadAppliances (app.js); protectedPlugs
// (controls.js All off, modes.js Away).
const CYCLE_TH = { run_w: 10, run_min: 2, idle_w: 5, idle_min: 3 };
const APPLIANCE = { // keep in step with APPLIANCES in backend/appliances.py
  fan: { rule: "on", th: {} },
  floor_lamp: { rule: "on", th: {} },
  tv: { rule: "busy", busy: "On", idle: "Standby", th: { on_w: 15 } },
  heater: { rule: "busy", busy: "Heating", idle: "Idle", th: { on_w: 100 } },
  kettle: { rule: "busy", busy: "Boiling…", idle: "Idle", th: { on_w: 1000 } },
  microwave: { rule: "busy", busy: "Heating…", idle: "Idle", th: { on_w: 300 } },
  coffee_machine: { rule: "busy", busy: "Brewing…", idle: "Idle", th: { on_w: 300 } },
  toaster: { rule: "busy", busy: "Toasting…", idle: "Idle", th: { on_w: 300 } },
  fridge: { rule: "busy", busy: "Cooling", idle: "Idle", th: { on_w: 30 } },
  freezer: { rule: "busy", busy: "Cooling", idle: "Idle", th: { on_w: 30 } },
  washer: { rule: "cycle", th: CYCLE_TH },
  dryer: { rule: "cycle", th: CYCLE_TH },
  dishwasher: { rule: "cycle", th: CYCLE_TH },
};
const PROTECTED_TYPES = ["fridge", "freezer"];
const FINISHED_FOR = 2 * 3600;
const TH_LABEL = { on_w: ["Busy above", "W"], run_w: ["Running above", "W"], run_min: ["for", "min"], idle_w: ["Finished below", "W"], idle_min: ["for", "min"] };
const ap = { cycles: {}, skew: 0, offer: null };
const an = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

// ---------- model ----------
const applianceLinked = (f) => !!(f?.plug && APPLIANCE[f.type]);
function applianceFor(eid, L = cur()) { return eid ? (L.furniture || []).find((f) => f.plug === eid && APPLIANCE[f.type]) || null : null; }
const applName = (f) => f.label || FURNITURE[f.type]?.name || f.type;
const applTh = (f) => ({ ...APPLIANCE[f.type].th, ...(f.thresholds || {}) });
const isProtected = (f) => PROTECTED_TYPES.includes(f.type) || keepOn().includes(f.plug);
const nowS = () => (Date.now() + ap.skew) / 1000;
function protectedPlugs() { return (st.layout.furniture || []).filter((f) => f.plug && PROTECTED_TYPES.includes(f.type)).map((f) => f.plug); }
function fmtMins(s) {
  const m = Math.floor(Math.max(0, s) / 60);
  if (m < 1) return "<1 min";
  if (m < 60) return `${m} min`;
  return m % 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${Math.floor(m / 60)} h`;
}
// Mirrors status() in backend/appliances.py.
function applianceStatus(f, d) {
  if (!d || !usable(d)) return "Offline";
  if (d.state !== "on") return "Off";
  const A = APPLIANCE[f.type], th = applTh(f), p = d.power;
  if (A.rule === "on") return p != null ? `On · ${fmtW(p)}` : "On";
  if (A.rule === "busy") return p == null ? "On" : p > th.on_w ? A.busy : A.idle;
  const c = ap.cycles[f.id], now = nowS();
  if (c?.plug === f.plug && c.phase === "running" && c.run_start != null) return `Running ${fmtMins(now - c.run_start)}`;
  if (p != null && p > th.run_w) return "Starting…";
  if (c?.plug === f.plug && c.phase === "finished" && c.finished_at != null && now - c.finished_at < FINISHED_FOR) return `Finished ${fmtMins(now - c.finished_at)} ago`;
  return "Idle";
}
function applianceBusy(f, d) {
  if (!d || d.state !== "on") return false;
  const A = APPLIANCE[f.type], th = applTh(f);
  if (A.rule === "busy") return d.power != null && d.power > th.on_w;
  if (A.rule === "cycle") return ap.cycles[f.id]?.phase === "running" || (d.power != null && d.power > th.run_w);
  return true;
}
// Status plus the live watts (when the status doesn't already say them).
function applianceText(f, d) {
  const s = applianceStatus(f, d);
  return d?.state === "on" && d.power != null && !/ W$/.test(s) ? `${s} · ${fmtW(d.power)}` : s;
}
function applianceHidesMarker(eid) {
  if (st.editing || !showFurniture) return false;
  const f = applianceFor(eid, st.layout);
  return !!f && f.hide_marker !== false && st.devices.has(eid);
}

// ---------- server cycles ----------
function setCycles(data) {
  if (!data || typeof data !== "object") return;
  if (typeof data.now === "number") ap.skew = data.now * 1000 - Date.now();
  ap.cycles = data.appliances || {};
  if (!st.drag) render();
  if (st.sheetFor && !st.holdSheet) renderSheet();
}
async function loadAppliances() { try { setCycles(await api("/api/appliances")); } catch {} }
function applianceEvents(es) {
  es.addEventListener("appliances", (e) => { try { setCycles(JSON.parse(e.data)); } catch {} });
  es.addEventListener("snapshot", () => loadAppliances());
}
// "Running 47 min" keeps counting without any new reading.
setInterval(() => {
  if (document.hidden || st.editing || st.drag) return;
  const L = st.layout.furniture || [];
  if (L.some((f) => f.plug && APPLIANCE[f.type]?.rule === "cycle" && ["running", "finished"].includes(ap.cycles[f.id]?.phase))) {
    render(); if (st.sheetFor && !st.holdSheet) renderSheet();
  }
}, 20e3);

// ---------- plan ----------
function renderAppliances() {
  const g = $("appliances"); if (!g) return;
  let top = $("applTags"); // status pills and raised room names: above the walls (snap.js), below doors and markers
  if (!top) top = el("g", { id: "applTags" });
  if (top.nextElementSibling !== $("openings")) $("openings").before(top);
  g.replaceChildren(); top.replaceChildren();
  const sel = st.editing && furSel(), lb = $("furLink")?.querySelector("span");
  if (lb) lb.textContent = sel?.plug ? `Plug: ${st.devices.get(sel.plug)?.name || sel.plug}` : "Link plug";
  if (st.editing || !showFurniture) return;
  const k = st.labelScale || st.markerScale || 1, m = typeof mpp === "function" ? mpp() : 0.01, tags = [];
  for (const f of st.layout.furniture || []) {
    const def = FURNITURE[f.type]; if (!def || !applianceLinked(f)) continue;
    const d = st.devices.get(f.plug), on = d?.state === "on", busy = applianceBusy(f, d), off = !d || !usable(d);
    const fg = el("g", { class: `fur appl fu-${f.type}` + (on ? " on" : "") + (busy ? " busy" : "") + (off ? " offline" : ""),
      "data-appl": f.id, "data-dev": f.plug, transform: `translate(${f.x} ${f.y}) rotate(${f.rot || 0})` }, g);
    const gp = Math.min(0.08, f.w * 0.2, f.h * 0.2);
    el("rect", { class: "appl-glow", x: -f.w / 2 - gp, y: -f.h / 2 - gp, width: f.w + 2 * gp, height: f.h + 2 * gp, rx: gp * 2 }, fg);
    def.draw(fg, f.w, f.h);
    // A comfortable touch target even for a kettle; markers above it still win.
    const hw = Math.max(f.w, 40 * m), hh = Math.max(f.h, 40 * m);
    el("rect", { class: "fu-hit appl-hit", x: -hw / 2, y: -hh / 2, width: hw, height: hh }, fg);
    const t = el("title", {}, fg); t.textContent = `${applName(f)} — ${applianceText(f, d)}`;
    if (d?.state !== "off") tags.push([f, d, on, busy, off]);
  }
  if (g.childElementCount && typeof furRaiseNames === "function") furRaiseNames(top); // room names stay readable
  for (const [f, d, on, busy, off] of tags) applianceTag(top, f, d, k, on, busy, off);
}
// The status pill under the piece (above it when that would leave the plan). Text is laid out at 17px in a scaled
// group: tiny SVG font sizes measure unreliably.
const TAG_PX = 17;
function applianceTag(g, f, d, k, on, busy, off) {
  const status = applianceStatus(f, d), extra = applianceText(f, d).slice(status.length);
  const s = (0.17 * k) / TAG_PX, ph = TAG_PX * 1.6;
  const tg = el("g", { class: "appl-tag" + (on ? " on" : "") + (busy ? " busy" : "") + (off ? " offline" : ""), "data-tag": f.id }, g);
  const bg = el("rect", { class: "bg", y: -ph / 2, height: ph, rx: ph / 2 }, tg);
  const t = el("text", { x: 0, y: 0, style: `font-size:${TAG_PX}px` }, tg);
  const s1 = el("tspan", { class: "s" }, t); s1.textContent = status;
  if (extra) { const s2 = el("tspan", { class: "w" }, t); s2.textContent = extra; }
  let w = 0;
  try { w = t.getComputedTextLength(); } catch {}
  if (!w) w = (status.length + extra.length) * TAG_PX * 0.56;
  const pw = w + ph * 0.8;
  bg.setAttribute("x", -pw / 2); bg.setAttribute("width", pw);
  const b = furBox(f), vb = st.viewBox || [-1e3, -1e3, 2e3, 2e3], gap = 0.05 * k, H = ph * s, W = pw * s;
  let y = b.y + b.h + gap + H / 2;
  if (y + H / 2 > vb[1] + vb[3]) y = b.y - gap - H / 2;
  const x = Math.min(vb[0] + vb[2] - W / 2, Math.max(vb[0] + W / 2, f.x));
  tg.setAttribute("transform", `translate(${+x.toFixed(4)} ${+y.toFixed(4)}) scale(${+s.toFixed(6)})`);
}
function applianceIcon(f, d) {
  const def = FURNITURE[f.type], pad = Math.max(def.w, def.h) * 0.12;
  const s = document.createElementNS(NS, "svg");
  const side = Math.max(def.w, def.h) + 2 * pad;
  s.setAttribute("viewBox", `${-side / 2} ${-side / 2} ${side} ${side}`);
  s.setAttribute("class", "appl-ic" + (d?.state === "on" ? " on" : "") + (applianceBusy(f, d) ? " busy" : ""));
  s.setAttribute("aria-hidden", "true");
  def.draw(el("g", { class: `fur fu-${f.type}` }, s), def.w, def.h);
  return s;
}

// ---------- taps ----------
function tapAppliance(fid) {
  const f = (st.layout.furniture || []).find((x) => x.id === fid); if (!f) return;
  const d = st.devices.get(f.plug);
  if (!d) { setStatus(`${applName(f)}: its plug isn't in Home Assistant right now`, true); return; }
  if (!usable(d) || isProtected(f)) openSheet(d.entity_id); else toggle(d);
}
// A marker or list tap on a fridge's plug opens its sheet too.
function applianceTapOpensSheet(eid) { const f = applianceFor(eid, st.layout); return !!f && isProtected(f); }
// The sheet's on/off button: switching a fridge (or a keep-on plug's appliance) off asks first.
function confirmOff(d) {
  const f = applianceFor(d.entity_id, st.layout);
  if (!f || d.state !== "on" || !isProtected(f)) return true;
  const n = FURNITURE[f.type]?.name || f.type;
  return confirm(f.label ? `Turn off ${f.label}?` : `Turn off the ${n === n.toUpperCase() ? n : n.toLowerCase()}?`);
}

// ---------- plug sheet: appliance section ----------
async function saveLink(f, patch) {
  const prev = { ...f };
  Object.assign(f, patch);
  for (const [key, v] of Object.entries(patch)) if (v === undefined) delete f[key];
  try { st.layout = await api("/api/layout", { method: "PUT", body: JSON.stringify(st.layout) }); }
  catch (e) { for (const key of Object.keys(f)) delete f[key]; Object.assign(f, prev); setStatus(`Save failed: ${e.message}`, true); return false; }
  render(); if (st.sheetFor) renderSheet();
  return true;
}
function applianceSheet(d, c) {
  const f = applianceFor(d.entity_id, st.layout); if (!f) return;
  const sec = an("section", "appl-sec"); sec.dataset.appl = f.id; c.appendChild(sec);
  const head = an("div", "appl-head");
  head.append(applianceIcon(f, d));
  const info = an("div", "appl-info");
  info.append(an("div", "name", applName(f)), an("div", "appl-status", applianceText(f, d)));
  head.append(info); sec.append(head);
  if (isProtected(f)) sec.append(an("div", "hint", PROTECTED_TYPES.includes(f.type) ? "Tapping it on the plan opens this sheet; switching it off asks first." : "A keep-on plug: tapping it on the plan opens this sheet."));
  const lab = an("label", "keep-on"); const cb = an("input"); cb.type = "checkbox"; cb.id = "applHide"; cb.checked = f.hide_marker !== false;
  lab.append(cb, an("span", null, "Hide plug marker — the appliance is the control"));
  cb.onchange = async () => { if (!await saveLink(f, { hide_marker: cb.checked })) cb.checked = !cb.checked; else setStatus(cb.checked ? "Plug marker hidden" : "Plug marker shown"); };
  sec.append(lab);
  const keys = Object.keys(APPLIANCE[f.type].th);
  if (!keys.length) return;
  const det = an("details", "appl-th"); det.open = !!ap.thOpen;
  det.addEventListener("toggle", () => { ap.thOpen = det.open; });
  const sum = an("summary", null, "Status thresholds"); det.append(sum);
  const th = applTh(f), form = an("div", "appl-th-body"); det.append(form);
  const inputs = {};
  const row = (ks) => {
    const r = an("div", "appl-th-row");
    for (const key of ks) {
      const [label, unit] = TH_LABEL[key];
      const l = an("label", null); const i = an("input"); Object.assign(i, { type: "number", step: "0.5", min: "0.1", inputMode: "decimal" });
      i.name = key; i.value = th[key]; inputs[key] = i;
      i.addEventListener("input", () => holdSheet(8000));
      i.addEventListener("change", commit);
      l.append(an("span", null, label), i, an("span", "u", unit)); r.append(l);
    }
    form.append(r);
  };
  if (APPLIANCE[f.type].rule === "busy") row(["on_w"]);
  else { row(["run_w", "run_min"]); row(["idle_w", "idle_min"]); }
  const note = an("div", "hint appl-th-msg"); form.append(note);
  const reset = an("button", "linkish", "Reset to defaults"); reset.type = "button";
  reset.onclick = async () => { if (await saveLink(f, { thresholds: undefined })) setStatus("Thresholds reset"); };
  form.append(reset);
  async function commit() {
    const out = {};
    for (const key of keys) {
      const v = Number(inputs[key].value);
      if (!(inputs[key].value.trim() && Number.isFinite(v) && v > 0)) { note.textContent = "Enter a number above 0."; return; }
      if (v !== APPLIANCE[f.type].th[key]) out[key] = Math.round(v * 10) / 10;
    }
    note.textContent = "";
    st.holdSheet = false;
    if (await saveLink(f, { thresholds: Object.keys(out).length ? out : undefined })) setStatus("Thresholds saved");
    else note.textContent = "Not saved — check the numbers.";
  }
  sec.append(det);
}

// ---------- edit mode: Link plug ----------
document.body.insertAdjacentHTML("beforeend", `
<dialog id="plugDialog">
  <form method="dialog" id="plugForm">
    <h3 id="plugDialogTitle">Link plug</h3>
    <p class="hint">The appliance becomes the control: tap it to switch its plug, hold it for the plug's sheet and history.</p>
    <div id="plugList" class="plug-list" role="listbox"></div>
    <menu><button value="cancel" formnovalidate>Cancel</button></menu>
  </form>
</dialog>
<div id="applOffer" class="appl-offer" role="status" hidden><span class="t"></span><button type="button" class="do primary"></button><button type="button" class="x" aria-label="Dismiss">×</button></div>`);
(() => {
  const btn = an("button", null); btn.id = "furLink"; btn.hidden = true; btn.title = "Link this appliance to a smart plug";
  btn.innerHTML = `<svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor" style="vertical-align:-2px" aria-hidden="true"><use href="#ic-plug"/></svg> <span>Link plug</span>`;
  ($("furEdit") || $("deleteSel")).after(btn);
  btn.onclick = linkDialog;
  // Appliance-capable pieces in the furniture catalogue get a small plug badge.
  for (const b of document.querySelectorAll("#furGrid .fur-item")) {
    if (!APPLIANCE[b.dataset.type]) continue;
    b.classList.add("appl"); b.title = `${FURNITURE[b.dataset.type].name} — can be linked to a smart plug`;
    b.insertAdjacentHTML("beforeend", `<svg class="appl-badge" viewBox="0 0 24 24" aria-hidden="true"><use href="#ic-plug"/></svg>`);
  }
  const offer = $("applOffer");
  offer.querySelector(".x").onclick = () => hideOffer();
  offer.querySelector(".do").onclick = () => { const fn = ap.offer?.fn; hideOffer(); fn?.(); };
})();
function showOffer(text, action, fn) {
  const o = $("applOffer"); clearTimeout(ap.offer?.timer);
  o.querySelector(".t").textContent = text; o.querySelector(".do").textContent = action; o.hidden = false;
  ap.offer = { fn, timer: setTimeout(hideOffer, 15e3) };
}
function hideOffer() { clearTimeout(ap.offer?.timer); ap.offer = null; $("applOffer").hidden = true; }
function unlink(f) { f.plug = null; delete f.hide_marker; delete f.thresholds; } // null: the server drops the link
function linkDialog() {
  const f = furSel(); if (!f || !APPLIANCE[f.type]) return;
  const dlg = $("plugDialog"), list = $("plugList");
  $("plugDialogTitle").textContent = `Link ${applName(f)} to a plug`;
  list.replaceChildren();
  const others = new Map((st.draft.furniture || []).filter((o) => o.plug && o !== f).map((o) => [o.plug, o]));
  const plugs = [...st.devices.values()].filter((d) => d.kind === "plug").sort((a, b) => a.name.localeCompare(b.name));
  if (f.plug && !st.devices.has(f.plug)) plugs.push({ entity_id: f.plug, name: f.plug, kind: "plug", state: "unavailable" });
  const opt = (eid, name, sub, cur) => {
    const b = an("button", "plug-opt" + (cur ? " cur" : "")); b.type = "button"; b.dataset.plug = eid; b.setAttribute("role", "option"); b.setAttribute("aria-selected", cur);
    const ic = document.createElementNS(NS, "svg"); ic.setAttribute("viewBox", "0 0 24 24"); el("use", { href: `#ic-plug` }, ic); ic.setAttribute("class", "pi");
    const txt = an("span", "pt"); txt.append(an("span", "name", name)); if (sub) txt.append(an("span", "sub", sub));
    b.append(ic, txt, an("span", "ck", cur ? "✓" : ""));
    list.append(b); return b;
  };
  opt("", "None", "Not linked", !f.plug);
  for (const d of plugs) {
    const o = others.get(d.entity_id);
    const w = !usable(d) ? d.state : d.state === "on" ? (d.power != null ? `on · ${fmtW(d.power)}` : "on") : "off";
    opt(d.entity_id, d.name, [w, o ? `linked to ${applName(o)} — moves here` : "", d.hidden ? "hidden" : ""].filter(Boolean).join(" · "), f.plug === d.entity_id)
      .classList.toggle("taken", !!o);
  }
  list.onclick = (e) => {
    const b = e.target.closest(".plug-opt"); if (!b) return;
    const eid = b.dataset.plug;
    dlg.close("ok");
    if (!eid) { if (f.plug) { unlink(f); setStatus(`${applName(f)} unlinked — Save to keep it`); } render(); return; }
    if (eid === f.plug) return;
    const o = others.get(eid); if (o) unlink(o);
    f.plug = eid; f.hide_marker = true; delete f.thresholds;
    render();
    const d = st.devices.get(eid), want = applName(f).slice(0, 40);
    setStatus(`${applName(f)} linked to ${d?.name || eid} — Save to keep it`);
    if (d && d.name !== want) showOffer(`Linked to “${d.name}”.`, `Rename plug to “${want}”`, async () => {
      try { await saveMeta(d, { name: want }); setStatus(`Plug renamed to “${want}”`); } catch (e) { setStatus(`Rename failed: ${e.message}`, true); }
    });
  };
  dlg.returnValue = ""; dlg.showModal();
}

// ---------- bell sheet: appliance pushes ----------
(() => {
  const sec = an("section", "auto-sec"); sec.id = "applSec";
  sec.innerHTML = `<h4>Appliances</h4>
    <label class="check"><input type="checkbox" id="applDone"> Washing machine, dryer or dishwasher finished</label>
    <p class="hint">Needs the appliance linked to its plug (Edit → select it → Link plug). Held during quiet hours.</p>`;
  const anchor = $("quietSec") || $("alertTest"); anchor.before(sec);
  const msg = (text, warn = false) => { $("alertMsg").textContent = text; $("alertMsg").classList.toggle("warn", warn); };
  $("alertsBtn").addEventListener("click", async () => {
    try { $("applDone").checked = (await api("/api/alerts/settings")).appliance_done !== false; } catch {}
  });
  $("applDone").addEventListener("change", async () => {
    try { await api("/api/alerts/settings", { method: "PUT", body: JSON.stringify({ appliance_done: $("applDone").checked }) }); msg("Saved."); }
    catch (e) { msg(`Couldn't save: ${e.message}`, true); }
  });
})();

document.addEventListener("DOMContentLoaded", loadAppliances);
