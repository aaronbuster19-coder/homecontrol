"use strict";
// Standby saver: per plug, off at night when idle, back on in the morning (run by the server, backend/standby.py).
// Hooks: standbyRow(d, c) in the plug sheet (app.js), standbySection(c) in the Energy sheet (energy.js).
const sbSt = { data: null, at: 0, pending: null, err: null };
const sbEl = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const sbHm = (ms) => new Date(ms).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const sbWhen = (ms) => {
  const d = new Date(ms);
  return new Date(sbSt.data?.now ?? Date.now()).toDateString() === d.toDateString() ? sbHm(ms) : `${d.toLocaleDateString("en-GB", { weekday: "short" })} ${sbHm(ms)}`;
};

function sbLoad(force = false) {
  if (!force && sbSt.data && Date.now() - sbSt.at < 30e3) return sbSt.data;
  if (!sbSt.pending) {
    sbSt.pending = api("/api/standby").then((d) => { sbSt.data = d; sbSt.at = Date.now(); sbSt.err = null; },
      (e) => { sbSt.err = e.message; sbSt.at = Date.now(); })
      .finally(() => { sbSt.pending = null; if (st.sheetFor) renderSheet(); });
  }
  return sbSt.data;
}
async function sbSave(eid, body) {
  try {
    sbSt.data = await api(`/api/standby/${encodeURIComponent(eid)}`, { method: "PUT", body: JSON.stringify(body) });
    sbSt.at = Date.now();
    const p = sbSt.data.plugs.find((x) => x.entity_id === eid);
    if ("enabled" in body) setStatus(body.enabled ? `Standby saver on for ${p.name}: off at ${p.off_at} if below ${p.threshold_w} W` : `Standby saver off for ${p.name}`);
    else setStatus("Standby saver saved");
  } catch (e) { setStatus(`Standby saver: ${e.message}`, true); }
  if (st.sheetFor) renderSheet();
}
const sbBlockedText = (p) => p.blocked === "keep on" ? "Keep-on plugs always stay on." : p.blocked === "fridge" ? "A fridge or freezer is linked to this plug — it always stays on."
  : !p.has_power ? "This plug doesn't measure power, so it can't tell whether something is in use." : "";
const sbSaves = (p) => !p.year_kwh ? "" : p.year_p != null ? `saves ≈ ${fmtP(p.year_p)}/year` : `saves ≈ ${p.year_kwh} kWh/year`;
function sbSummary(p) {
  if (!p.enabled) return p.standby_w != null ? `Standby ${fmtW(p.standby_w)} overnight` + (sbSaves(p) ? ` — would ${sbSaves(p).replace("saves", "save")}` : "") : "";
  const parts = [`Off at ${p.off_at} if below ${p.threshold_w} W for 15 min, back on at ${p.on_at}`];
  if (sbSaves(p)) parts.push(sbSaves(p)[0].toUpperCase() + sbSaves(p).slice(1));
  return parts.join(" · ");
}
function sbOwnedText(p) {
  if (!p.owned) return "";
  return p.owed ? `Off by the saver since ${sbWhen(p.owned_since)} — back on when you're home.`
    : `Off by the saver since ${sbWhen(p.owned_since)} — back on at ${p.on_at}.`;
}
function sbToggle(p, label) {
  const lab = sbEl("label", "keep-on sb-toggle"); const cb = sbEl("input"); cb.type = "checkbox";
  cb.checked = p.enabled; cb.dataset.standby = p.entity_id; cb.disabled = !p.enabled && (!!p.blocked || !p.has_power);
  cb.onchange = () => sbSave(p.entity_id, { enabled: cb.checked });
  lab.append(cb, sbEl("span", null, label)); return lab;
}

// ---------- plug sheet ----------
function standbyRow(d, c) {
  const data = sbLoad(), p = data?.plugs.find((x) => x.entity_id === d.entity_id);
  const box = sbEl("div", "sb-row"); box.id = "standbyRow"; c.appendChild(box);
  if (!p) { box.appendChild(sbEl("div", "sub", sbSt.err ? `Standby saver: ${sbSt.err}` : "Standby saver…")); return; }
  box.appendChild(sbToggle(p, "Standby saver (off at night when idle)"));
  const why = !p.enabled ? sbBlockedText(p) : "";
  const info = sbEl("div", "sub sb-info", why || sbSummary(p) || "Turns this plug off at night only when nothing is using it, and back on in the morning.");
  box.appendChild(info);
  if (p.owned) box.appendChild(sbEl("div", "sub sb-owned", sbOwnedText(p)));
  if (!p.enabled) return;
  const f = sbEl("div", "sb-opts");
  const num = (v) => { const i = sbEl("input"); i.type = "number"; i.min = "2"; i.max = "500"; i.step = "0.5"; i.inputMode = "decimal"; i.value = v; return i; };
  const tm = (v) => { const i = sbEl("input"); i.type = "time"; i.step = "60"; i.value = v; return i; };
  const thr = num(p.threshold_w), off = tm(p.off_at), on = tm(p.on_at);
  thr.id = "sbThreshold"; off.id = "sbOff"; on.id = "sbOn";
  const l1 = sbEl("label", null, "Off at "); l1.append(off, " if below ", thr, " W");
  const l2 = sbEl("label", null, "Back on at "); l2.append(on);
  f.append(l1, l2);
  f.appendChild(sbEl("div", "hint", `Suggested threshold ${p.suggested_w} W` + (p.standby_w != null ? ` (standby ${fmtW(p.standby_w)} + 5 W).` : " (no overnight data yet).")));
  thr.onchange = () => { const t = Number(thr.value); if (!(t >= 2 && t <= 500)) { setStatus("Threshold must be 2–500 W", true); return; } sbSave(p.entity_id, { threshold_w: t }); };
  const times = () => { if (/^\d\d:\d\d$/.test(off.value) && /^\d\d:\d\d$/.test(on.value)) sbSave(p.entity_id, { off_at: off.value, on_at: on.value }); };
  off.onchange = times; on.onchange = times;
  box.appendChild(f);
}

// ---------- Energy sheet ----------
const SB_ACTION = { off: "Turned off", on: "Turned back on", skipped: "Skipped", released: "Left alone", waiting: "Waiting",
  failed: "Failed", enabled: "Saver on", disabled: "Saver off" };
function standbySection(c) {
  const sec = sbEl("div", "sum-sec sb-sec"); sec.id = "standbySaver"; c.appendChild(sec);
  sec.appendChild(sbEl("h4", null, "Standby saver"));
  const data = sbLoad();
  if (!data) { sec.appendChild(sbEl("div", "hist-msg" + (sbSt.err ? " err" : " loading"), sbSt.err ? `Couldn't load: ${sbSt.err}` : "Loading…")); return; }
  sec.appendChild(sbEl("div", "sub", "Off at night only when the plug has stayed below its standby threshold for 15 min — never something in use — and back on in the morning if nobody touched it."));
  const plugs = data.plugs.filter((p) => p.has_power && (!p.hidden || p.enabled));
  if (!plugs.length) sec.appendChild(sbEl("div", "hist-msg", "No plugs that measure power."));
  const ul = sbEl("ul", "sb-list"); sec.appendChild(ul);
  for (const p of plugs) {
    const li = sbEl("li"); li.dataset.plug = p.entity_id;
    li.appendChild(sbToggle(p, p.name));
    const sub = (!p.enabled && sbBlockedText(p)) || sbOwnedText(p) || sbSummary(p);
    if (sub) li.appendChild(sbEl("div", "sub", sub));
    ul.appendChild(li);
  }
  if (data.log.length) {
    const fold = sbEl("details", "sb-log"); fold.open = !!sbSt.logOpen; fold.id = "standbyLog";
    fold.addEventListener("toggle", () => { sbSt.logOpen = fold.open; });
    fold.appendChild(sbEl("summary", null, `Recent (${Math.min(data.log.length, 10)})`));
    for (const x of data.log.slice(0, 10)) {
      const r = sbEl("div", "sum-row");
      r.append(sbEl("span", null, `${SB_ACTION[x.action] || x.action}: ${x.name}${x.note ? ` — ${x.note}` : ""}`), sbEl("span", null, sbWhen(x.at)));
      fold.appendChild(r);
    }
    sec.appendChild(fold);
  }
}
// The Energy sheet opens: always fresh numbers / log.
$("energyBtn").addEventListener("click", () => sbLoad(true));
