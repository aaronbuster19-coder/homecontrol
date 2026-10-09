"use strict";
// Energy costs (tariff dialog, "Energy" sheet, pence next to kWh), and per-device names / hidden flags
// (rename dialog + Hide toggle in every device sheet, "Hidden devices" sheet). Hooks from app.js:
// renderSheet -> renderEnergy(c) / renderHidden(c) / deviceMeta(d, c); costText(kwh) wherever kWh is shown.
const ENERGY = "@energy", HIDDEN = "@hidden";
const KIND_ONE = { light: "Light", plug: "Plug", valve: "Radiator valve", sensor: "Door / window sensor" };
const E_TTL = { today: 60e3, week: 300e3, month: 300e3, standby: 600e3 };
const eState = { range: "today" };
const en = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

// ---------- money ----------
const tariff = () => st.layout.settings?.energy || {};
const unitRate = () => tariff().rate_p ?? null;
function fmtP(p) {
  if (p == null) return "";
  if (p > 0 && p < 0.5) return "<1p";
  return p < 99.5 ? `${Math.round(p)}p` : `£${(p / 100).toFixed(2)}`;
}
// " · 10p" after a kWh figure when a unit rate is set.
function costText(kwh) { const r = unitRate(); return r == null || kwh == null ? "" : ` · ${fmtP(kwh * r)}`; }

// ---------- dialogs (built here so index.html stays small) ----------
document.body.insertAdjacentHTML("beforeend", `
<dialog id="tariffDialog">
  <form method="dialog" id="tariffForm">
    <h3>Energy tariff</h3>
    <label>Unit rate (pence per kWh) <input name="rate" type="number" min="0" max="200" step="0.01" inputmode="decimal" placeholder="e.g. 24.5"></label>
    <label>Standing charge (pence per day, optional) <input name="standing" type="number" min="0" max="500" step="0.01" inputmode="decimal" placeholder="e.g. 60.1"></label>
    <p class="hint">Costs are worked out from the smart plugs only. Leave the rate empty to hide costs.</p>
    <p class="hint warn" id="tariffMsg"></p>
    <menu><button value="cancel" formnovalidate>Cancel</button><button value="ok" class="primary" id="tariffOk">Save</button></menu>
  </form>
</dialog>
<dialog id="renameDialog">
  <form method="dialog" id="renameForm">
    <h3>Rename</h3>
    <label>Name shown in this app <input name="name" maxlength="40" autocomplete="off"></label>
    <p class="hint" id="renameHint"></p>
    <menu><button value="cancel" formnovalidate>Cancel</button><button value="ok" class="primary">Save</button></menu>
  </form>
</dialog>`);

function tariffDialog() {
  const dlg = $("tariffDialog"), f = $("tariffForm"), t = tariff();
  f.rate.value = t.rate_p ?? ""; f.standing.value = t.standing_p ?? ""; $("tariffMsg").textContent = "";
  $("tariffOk").onclick = async (e) => {
    e.preventDefault();
    const num = (v) => v.trim() === "" ? null : Number(v);
    const body = { rate_p: num(f.rate.value), standing_p: num(f.standing.value) };
    if (body.rate_p != null && !(body.rate_p >= 0 && body.rate_p <= 200)) { $("tariffMsg").textContent = "Unit rate must be 0–200 p/kWh."; return; }
    if (body.standing_p != null && !(body.standing_p >= 0 && body.standing_p <= 500)) { $("tariffMsg").textContent = "Standing charge must be 0–500 p/day."; return; }
    try {
      const saved = await api("/api/energy/settings", { method: "PUT", body: JSON.stringify(body) });
      st.layout.settings = { keep_on: [], ...(st.layout.settings || {}), energy: saved };
      for (const k of hCache.keys()) if (k.startsWith("energy|")) hCache.delete(k);
      dlg.close(); setStatus(saved.rate_p == null ? "Unit rate cleared" : `Unit rate ${saved.rate_p}p/kWh saved`);
      render(); if (st.sheetFor) renderSheet();
    } catch (err) { $("tariffMsg").textContent = err.message; }
  };
  dlg.returnValue = ""; dlg.showModal();
}

// ---------- device names / hidden ----------
async function saveMeta(d, body) {
  const L = await api(`/api/devices/${encodeURIComponent(d.entity_id)}/meta`, { method: "PUT", body: JSON.stringify(body) });
  st.layout = L;
  const s = L.settings || {};
  d.name = s.names?.[d.entity_id] || d.ha_name || d.name; d.hidden = (s.hidden || []).includes(d.entity_id);
  for (const k of hCache.keys()) if (k.startsWith("energy|")) hCache.delete(k);
  render(); if (st.sheetFor) renderSheet();
}
function renameDialog(d) {
  const dlg = $("renameDialog"), f = $("renameForm");
  f.name.value = d.name === d.ha_name ? "" : d.name; f.name.placeholder = d.ha_name || d.name;
  $("renameHint").textContent = `Leave empty to use the Home Assistant name (“${d.ha_name || d.name}”). Home Assistant isn't changed.`;
  dlg.onclose = async () => {
    if (dlg.returnValue !== "ok") return;
    try { await saveMeta(d, { name: f.name.value.trim() }); setStatus(`Renamed to “${d.name}”`); }
    catch (e) { setStatus(`Rename failed: ${e.message}`, true); }
  };
  dlg.returnValue = ""; dlg.showModal(); f.name.focus();
}
// Bottom of every device sheet: rename + hide.
function deviceMeta(d, c) {
  const row = en("div", "dev-meta");
  const b = en("button", null, "✎ Rename"); b.type = "button"; b.onclick = () => renameDialog(d);
  const lab = en("label", "keep-on hide-toggle"); const cb = en("input"); cb.type = "checkbox"; cb.checked = !!d.hidden;
  lab.append(cb, en("span", null, "Hide"));
  lab.title = "Hide from the plan and device lists (“All off” still includes it)";
  cb.onchange = async () => {
    try { await saveMeta(d, { hidden: cb.checked }); setStatus(cb.checked ? `${d.name} hidden — ⋯ → Hidden devices to bring it back` : `${d.name} shown again`); }
    catch (e) { cb.checked = !cb.checked; setStatus(`Save failed: ${e.message}`, true); }
  };
  row.append(b, lab); c.appendChild(row);
  if (d.name !== d.ha_name && d.ha_name) c.appendChild(en("div", "sub ha-name", `Home Assistant: ${d.ha_name}`));
}
function renderHidden(c) {
  c.appendChild(en("h3", null, "Hidden devices"));
  const hid = [...st.devices.values()].filter((d) => d.hidden).sort((a, b) => a.name.localeCompare(b.name));
  c.appendChild(en("div", "sub", hid.length ? "Hidden from the plan and lists. “All off” and Away still switch them off." : "Nothing is hidden. Use “Hide” in a device's sheet."));
  const ul = en("ul", "hidden-list"); c.appendChild(ul);
  for (const d of hid) {
    const li = en("li"); li.dataset.hidden = d.entity_id;
    const info = en("div", "valve-info"); info.append(en("div", "name", d.name), en("div", "sub", `${KIND_ONE[d.kind] || d.kind} · ${deviceValue(d)}`));
    const b = en("button", null, "Unhide"); b.type = "button";
    b.onclick = async () => { try { await saveMeta(d, { hidden: false }); setStatus(`${d.name} is back`); } catch (e) { setStatus(`Save failed: ${e.message}`, true); } };
    li.append(info, b); ul.appendChild(li);
  }
}

// ---------- Energy sheet ----------
function rows(sec, items, bar) {
  const max = Math.max(0, ...items.map(bar));
  for (const it of items) {
    const r = en("div", "sum-row"); r.dataset.plug = it.entity_id;
    r.append(en("span", null, it.label), en("span", null, it.value));
    const b = en("div", "sum-bar"), i = en("i"); i.style.width = `${max ? bar(it) / max * 100 : 0}%`; b.appendChild(i); r.appendChild(b);
    sec.appendChild(r);
  }
}
function renderEnergy(c) {
  c.appendChild(en("h3", null, "Energy"));
  c.appendChild(en("div", "sub", "Smart plugs only — the rest of the flat isn't measured."));
  const top = en("div", "doors-top");
  const cap = (r) => r[0].toUpperCase() + r.slice(1);
  top.appendChild(segmented(["Today", "Week", "Month"], cap(eState.range), (r) => { eState.range = r.toLowerCase(); renderSheet(); }));
  c.appendChild(top);
  const t = tariff(), rate = t.rate_p ?? null;
  const tr = en("div", "energy-tariff");
  tr.appendChild(en("span", null, rate == null ? "No unit rate set — costs are hidden." : `${rate}p/kWh` + (t.standing_p != null ? ` · standing charge ${t.standing_p}p/day` : "")));
  const tb = en("button", rate == null ? "primary" : null, rate == null ? "Set tariff…" : "Change…"); tb.type = "button"; tb.id = "tariffBtn"; tb.onclick = tariffDialog;
  tr.appendChild(tb); c.appendChild(tr);

  const rng = eState.range, key = `energy|${rng}`;
  const hit = hFetch(key, `/api/energy?range=${rng}`, E_TTL[rng]);
  const box = en("div", "energy-body"); c.appendChild(box);
  const hidden = [];
  if (!hit) {
    const err = hCache.get(key)?.error;
    box.appendChild(en("div", "hist-msg" + (err && !hPending.has(key) ? " err" : " loading"), err && !hPending.has(key) ? `Couldn't load energy: ${err}` : "Loading…"));
  } else {
    const e = hit.data, label = { today: "Today", week: "This week", month: "This month" }[rng];
    const big = en("div", "sum-big"); big.id = "energyTotal";
    big.append(en("b", null, e.total_p != null ? fmtP(e.total_p) : `${e.total_kwh.toFixed(2)} kWh`),
      en("span", null, e.total_p != null ? `${label}: ${e.total_kwh.toFixed(2)} kWh` : label));
    box.appendChild(big);
    if (e.standing_total_p != null) box.appendChild(en("div", "sub standing", `Plus standing charge ${fmtP(e.standing_total_p)} (${e.days} day${e.days === 1 ? "" : "s"} × ${e.standing_p}p) — not split across plugs.`));
    const shown = e.plugs.filter((p) => !p.hidden);
    hidden.push(...e.plugs.filter((p) => p.hidden).map((p) => ({ label: p.name, entity_id: p.entity_id, value: `${p.kwh.toFixed(2)} kWh${p.cost_p != null ? " · " + fmtP(p.cost_p) : ""}` })));
    const sec = en("div", "sum-sec"); sec.appendChild(en("h4", null, "By plug")); box.appendChild(sec);
    if (!shown.length) sec.appendChild(en("div", "hist-msg", "No plugs with energy monitoring."));
    rows(sec, shown.map((p) => ({ entity_id: p.entity_id, label: p.name, kwh: p.kwh,
      value: `${p.kwh.toFixed(2)} kWh${p.cost_p != null ? " · " + fmtP(p.cost_p) : ""}` })), (it) => it.kwh);
  }

  const sb = hFetch("energy|standby", "/api/energy/standby", E_TTL.standby);
  const ssec = en("div", "sum-sec standby"); c.appendChild(ssec);
  ssec.appendChild(en("h4", null, "Overnight standby"));
  if (!sb) ssec.appendChild(en("div", "hist-msg loading", "Loading…"));
  else {
    const s = sb.data;
    ssec.appendChild(en("div", "sub", `Average power ${s.from}–${s.to} over the last ${s.nights} nights — what's always on.` + (s.rate_p != null ? " Yearly cost if it stays like this." : "")));
    const yr = (p) => p.avg_w == null ? "no data" : `${fmtW(p.avg_w)}` + (p.year_p != null ? ` · ≈ ${fmtP(p.year_p)}/yr` : ` · ${p.year_kwh} kWh/yr`);
    const shown = s.plugs.filter((p) => !p.hidden);
    rows(ssec, shown.map((p) => ({ entity_id: p.entity_id, label: p.name, w: p.avg_w || 0, value: yr(p) })), (it) => it.w);
    for (const p of s.plugs.filter((p) => p.hidden)) {
      const h = hidden.find((x) => x.entity_id === p.entity_id);
      if (h) h.value += ` · standby ${yr(p)}`; else hidden.push({ label: p.name, entity_id: p.entity_id, value: `standby ${yr(p)}` });
    }
  }
  if (hidden.length) {
    const fold = en("details", "sum-sec energy-hidden"); fold.open = !!eState.hiddenOpen;
    fold.addEventListener("toggle", () => { eState.hiddenOpen = fold.open; });
    fold.appendChild(en("summary", null, `Hidden (${hidden.length})`));
    for (const h of hidden) { const r = en("div", "sum-row"); r.append(en("span", null, h.label), en("span", null, h.value)); fold.appendChild(r); }
    c.appendChild(fold);
  }
}

$("energyBtn").onclick = () => openSheet(ENERGY);
$("hiddenBtn").onclick = () => openSheet(HIDDEN);
