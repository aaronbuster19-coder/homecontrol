"use strict";
// Morning brief (one card: last night's door / window log, today's weather, yesterday's energy cost, what's left on)
// and the monthly energy report (each appliance's share of the cost, compared with last month).
// The brief opens by itself once per morning (05:00–12:00, first visit of the day, per device; "Show every morning"
// turns that off) and from ⋯ → Morning brief; the report opens from the brief's energy card (›). Read-only: rows open the device sheet, nothing is switched from here.
// Data: GET /api/brief, GET /api/energy/report?month=YYYY-MM. Uses weather.js (wxIcon, WEATHER), energy.js (fmtP).
// All in one closure (no globals to clash with other scripts) except window.openBrief / window.openReport.
(() => {
const BRIEF_SEEN = "hc.brief.seen", BRIEF_AUTO = "hc.brief.auto";  // last date it opened by itself; "0" = don't
const MORNING = [5, 12];
const br = { data: null, err: null, busy: false, allEvents: false };
const rep = { month: null, cache: new Map(), busy: null, hiddenOpen: false };
const be = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const lsGet = (k) => { try { return localStorage.getItem(k); } catch { return null; } };
const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch {} };
const localDay = (t = new Date()) => `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, "0")}-${String(t.getDate()).padStart(2, "0")}`;
const hhmm = (ms) => new Date(ms).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
const money = (p) => typeof fmtP === "function" ? fmtP(p) : `${Math.round(p)}p`;
const kwh = (k) => `${k < 10 ? k.toFixed(2) : k.toFixed(1)} kWh`;
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function dayRange(from, to) {  // "2026-10-01", "2026-10-08" -> "1–8 Oct"
  const [, m0, d0] = from.split("-").map(Number), [, m1, d1] = to.split("-").map(Number);
  if (from === to) return `${d0} ${MON[m0 - 1]}`;
  return m0 === m1 ? `${d0}–${d1} ${MON[m1 - 1]}` : `${d0} ${MON[m0 - 1]} – ${d1} ${MON[m1 - 1]}`;
}

// ---------- sheets + menu entries (built here so index.html stays small) ----------
document.body.insertAdjacentHTML("beforeend", `
<div id="briefSheet" class="sheet brief-sheet" hidden>
  <div class="sheet-body" role="dialog" aria-labelledby="briefTitle">
    <button class="close" id="briefClose" aria-label="Close">×</button>
    <div class="brief-kicker" id="briefHello">Good morning</div>
    <h3 id="briefTitle">Morning brief</h3>
    <div id="briefBody"></div>
    <div class="brief-foot">
      <label class="check"><input type="checkbox" id="briefAuto"> Show every morning</label>
      <button id="briefDone" class="primary" type="button">Got it</button>
    </div>
  </div>
</div>
<div id="reportSheet" class="sheet report-sheet" hidden>
  <div class="sheet-body" role="dialog" aria-labelledby="reportTitle">
    <button class="close" id="reportClose" aria-label="Close">×</button>
    <h3 id="reportTitle">Energy report</h3>
    <div class="report-nav">
      <button id="reportPrev" type="button" aria-label="Previous month">‹</button>
      <b id="reportMonth"></b>
      <button id="reportNext" type="button" aria-label="Next month">›</button>
    </div>
    <div id="reportBody"></div>
  </div>
</div>`);
(() => {
  const anchor = $("energyBtn");
  const mk = (id, text) => { const b = be("button", null, text); b.id = id; b.type = "button"; b.setAttribute("role", "menuitem"); return b; };
  const bb = mk("briefBtn", "Morning brief");  // one item: the report opens from the brief's energy card
  if (anchor) anchor.after(bb); else $("moreMenu").prepend(bb);
  bb.onclick = () => openBrief();
})();

// ---------- brief ----------
async function loadBrief() {
  if (br.busy) return br.busy;
  br.busy = api("/api/brief").then((d) => { br.data = d; br.err = null; }, (e) => { br.err = e.message; })
    .finally(() => { br.busy = false; if (!$("briefSheet").hidden) renderBrief(); });
  return br.busy;
}
function openBrief(reload = true) {
  br.allEvents = false;
  $("briefSheet").hidden = false;
  renderBrief();
  if (reload) loadBrief();
}
function closeBrief() { $("briefSheet").hidden = true; }

function card(cls, title, onOpen) {
  const c = be("section", `brief-card ${cls}`);
  const h = be("h4", null, title);
  c.append(h);
  if (onOpen) {
    const go = be("button", "brief-go", "›"); go.type = "button"; go.setAttribute("aria-label", `Open ${title}`);
    go.onclick = onOpen; h.append(go);
  }
  return c;
}
const partErr = (c, d, part) => { c.append(be("div", "hist-msg err", `Couldn't load: ${d.errors?.[part] || "unknown error"}`)); };

function renderBrief() {
  const body = $("briefBody"), d = br.data, h = new Date().getHours();
  $("briefHello").textContent = h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
  $("briefAuto").checked = lsGet(BRIEF_AUTO) !== "0";
  body.replaceChildren();
  if (!d) {
    $("briefTitle").textContent = "Morning brief";
    body.append(be("div", "hist-msg" + (br.err ? " err" : " loading"), br.err ? `Couldn't load the brief: ${br.err}` : "Loading…"));
    return;
  }
  $("briefTitle").textContent = d.label;
  const grid = be("div", "brief-grid"); body.append(grid);
  grid.append(weatherCard(d), nightCard(d), energyCard(d), leftCard(d));
}

function weatherCard(d) {
  const w = d.weather;
  const c = card("brief-wx", "Weather", w?.available ? () => { closeBrief(); openSheet(WEATHER); } : null);
  if (!w) { partErr(c, d, "weather"); return c; }
  if (!w.available) { c.append(be("div", "sub", w.entity_id ? "The weather entity is unavailable right now." : "No weather entity in Home Assistant.")); return c; }
  const row = be("div", "brief-wx-now");
  const ic = be("span", "brief-wx-ic"); if (typeof wxIcon === "function") ic.innerHTML = wxIcon(w.condition, w.condition === "clear-night");
  const main = be("div");
  main.append(be("div", "brief-wx-t", w.temperature != null ? `${Math.round(w.temperature)}°` : "–"), be("div", "brief-wx-c", w.text));
  row.append(ic, main); c.append(row);
  const facts = [];
  if (w.high != null && w.low != null) facts.push(`High ${Math.round(w.high)}° · low ${Math.round(w.low)}°`);
  if (w.rain_from) facts.push(`Rain likely from ${w.rain_from} (${w.rain_pct} %)`);
  for (const f of facts) c.append(be("div", "brief-line", f));
  if (w.tonight) c.append(be("div", "brief-line brief-cold", w.tonight));
  return c;
}

function nightCard(d) {
  const n = d.night;
  const c = card("brief-night", n ? `Last night · ${n.label}` : "Last night");
  if (!n) { partErr(c, d, "night"); return c; }
  if (!n.sensors) c.append(be("div", "sub", "No door or window sensors."));
  else if (!n.events.length) c.append(be("div", "brief-quiet", "All quiet — no doors or windows opened."));
  else {
    const opens = n.doors.map((x) => `${x.name} ${x.opens}×`).join(" · ");
    c.append(be("div", "brief-line", `Opened: ${opens}`));
    const ul = be("ul", "door-events brief-events"), SHOW = 6;
    const evs = br.allEvents ? n.events : n.events.slice(-SHOW);
    for (const e of evs) {
      const li = be("li", `ev ${e.state}`); li.dataset.dev = e.entity_id;
      li.append(be("span", "t", hhmm(e.t)), be("span", "dot"), be("span", null, `${e.name} ${e.state === "open" ? "opened" : "closed"}`));
      ul.append(li);
    }
    c.append(ul);
    const more = n.events.length - evs.length + n.more;
    if (more > 0 && !br.allEvents) {
      const b = be("button", "brief-more", `Show all (${n.events.length + n.more})`); b.type = "button";
      b.onclick = () => { br.allEvents = true; renderBrief(); };
      c.append(b);
    }
  }
  if (d.open_now?.length) c.append(be("div", "brief-line brief-open", `Open now: ${d.open_now.map((x) => x.name).join(", ")}`));
  return c;
}

function energyCard(d) {
  const e = d.energy;
  const c = card("brief-energy", "Yesterday's energy", () => { closeBrief(); openReport(); });
  if (!e) { partErr(c, d, "energy"); return c; }
  if (!e.plugs) { c.append(be("div", "sub", "No smart plugs with energy monitoring.")); return c; }
  if (!e.available) { c.append(be("div", "sub", "No energy data for yesterday yet.")); return c; }
  const big = be("div", "brief-big"); big.id = "briefCost";
  big.append(be("b", null, e.cost_p != null ? money(e.cost_p) : kwh(e.kwh)), be("span", null, e.cost_p != null ? kwh(e.kwh) : "smart plugs"));
  c.append(big);
  if (e.prev_kwh != null) {
    const dk = e.kwh - e.prev_kwh;
    if (Math.abs(dk) < 0.01) c.append(be("div", "brief-line", "Same as the day before"));
    else {
      const amt = e.cost_p != null ? money(Math.abs(e.cost_p - e.prev_cost_p)) : kwh(Math.abs(dk));
      c.append(be("div", `brief-line ${dk > 0 ? "sum-up" : "sum-down"}`, `${dk > 0 ? "▲" : "▼"} ${amt} ${dk > 0 ? "more" : "less"} than the day before`));
    }
  }
  if (e.top.length) c.append(be("div", "brief-line brief-top", e.top.map((t) => `${t.name} ${t.cost_p != null ? money(t.cost_p) : kwh(t.kwh)}`).join(" · ")));
  if (e.rate_p == null) c.append(be("div", "sub brief-hint", "Set a tariff in ⋯ → Energy to see costs."));
  else if (e.standing_p != null) c.append(be("div", "sub brief-hint", `Plus ${money(e.standing_p)} standing charge`));
  return c;
}

function since(ms) {
  if (!ms) return "";
  const t = new Date(ms), today = new Date();
  const y = new Date(today); y.setDate(y.getDate() - 1);
  if (t.toDateString() === today.toDateString()) return `since ${hhmm(ms)}`;
  if (t.toDateString() === y.toDateString()) return `since yesterday ${hhmm(ms)}`;
  return `since ${t.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" })}`;
}
function leftCard(d) {
  const c = card("brief-left", "Left on");
  if (!d.left_on.length) { c.append(be("div", "brief-quiet", "Nothing left on.")); return c; }
  const ul = be("ul", "brief-rows");
  for (const x of d.left_on) {
    const li = be("li"), b = be("button", "brief-row"); b.type = "button"; b.dataset.dev = x.entity_id;
    const name = be("span", "name", x.name);
    const det = [x.power != null ? fmtW(x.power) : "", since(x.since)].filter(Boolean).join(" · ");
    b.append(name, be("span", "val", det));
    b.onclick = () => { closeBrief(); if (st.devices.has(x.entity_id)) openSheet(x.entity_id); };
    li.append(b); ul.append(li);
  }
  c.append(ul);
  return c;
}

// ---------- monthly report ----------
function openReport(month) {
  rep.month = month || rep.month;
  $("reportSheet").hidden = false;
  renderReport();
}
function closeReport() { $("reportSheet").hidden = true; }
function loadReport(month) {
  const key = month || "";
  if (rep.busy === key) return;
  rep.busy = key;
  api("/api/energy/report" + (month ? `?month=${month}` : "")).then(
    (d) => { rep.cache.set(key, { at: Date.now(), data: d }); rep.cache.set(d.month, { at: Date.now(), data: d }); if (!month) rep.month = d.month; },
    (e) => rep.cache.set(key, { at: Date.now(), error: e.message }))
    .finally(() => { if (rep.busy === key) rep.busy = null; if (!$("reportSheet").hidden) renderReport(); });
}
function renderReport() {
  const body = $("reportBody"), key = rep.month || "";
  const hit = rep.cache.get(key), fresh = hit && Date.now() - hit.at < 300e3;
  if (!fresh) loadReport(rep.month);
  body.replaceChildren();
  const d = hit?.data;
  $("reportMonth").textContent = d ? d.label : "";
  $("reportPrev").disabled = !d?.prev_month; $("reportNext").disabled = !d?.next_month;
  $("reportPrev").onclick = () => d?.prev_month && openReport(d.prev_month);
  $("reportNext").onclick = () => d?.next_month && openReport(d.next_month);
  if (!d) {
    body.append(be("div", "hist-msg" + (hit?.error ? " err" : " loading"), hit?.error ? `Couldn't load the report: ${hit.error}` : "Loading…"));
    return;
  }
  const rate = d.rate_p;
  body.append(be("div", "sub", (d.days ? `${dayRange(d.from, d.to)}${d.current ? " (so far)" : ""}` : "Nothing finished yet this month") +
    " · smart plugs only" + (rate != null ? ` · at ${rate}p/kWh` : "")));
  if (!d.days) { body.append(be("div", "hist-msg", "The first day of the month is counted from tomorrow. Look at last month with ‹.")); return; }
  const big = be("div", "sum-big"); big.id = "reportTotal";
  big.append(be("b", null, d.total_p != null ? money(d.total_p) : kwh(d.total_kwh)), be("span", null, d.total_p != null ? kwh(d.total_kwh) : "smart plugs"));
  body.append(big);
  const p = d.previous;
  if (p.days_with_data) {
    const amt = p.total_p != null ? money(p.total_p) : kwh(p.total_kwh);
    const line = be("div", "report-cmp"); line.id = "reportCompare";
    line.append(`vs ${amt} ${p.full ? `in ${p.label.split(" ")[0]}` : `on ${dayRange(p.from, p.to)}`}`);
    if (d.change_pct === 0) line.append(" — about the same");
    else if (d.change_pct != null) line.append(" ", be("span", d.change_pct > 0 ? "sum-up" : "sum-down", `(${d.change_pct > 0 ? "+" : "−"}${Math.abs(d.change_pct)} %)`));
    body.append(line);
  } else body.append(be("div", "report-cmp", `No data for ${p.label.split(" ")[0]} to compare with.`));
  if (d.standing_total_p != null) body.append(be("div", "sub standing", `Plus standing charge ${money(d.standing_total_p)} (${d.days} day${d.days === 1 ? "" : "s"} × ${d.standing_p}p) — not split across appliances.`));
  if (d.days_with_data < d.days) {
    body.append(be("div", "sub report-cover", `Data for ${d.days_with_data} of ${d.days} days.` + (d.collecting_since ? ` Home Assistant keeps about 10 days of history; homecontrol has kept its own daily totals since ${dayRange(d.collecting_since, d.collecting_since)}.` : "")));
  }
  const shown = d.rows.filter((r) => !r.hidden), hidden = d.rows.filter((r) => r.hidden);
  const sec = be("div", "sum-sec report-rows"); sec.append(be("h4", null, "By appliance")); body.append(sec);
  if (!shown.length) sec.append(be("div", "hist-msg", "No energy used by the smart plugs."));
  const was = !p.days_with_data ? null : p.full ? p.label.split(" ")[0] : dayRange(p.from, p.to);
  for (const r of shown) sec.append(reportRow(r, was));
  if (hidden.length) {
    const fold = be("details", "sum-sec energy-hidden"); fold.open = rep.hiddenOpen;
    fold.addEventListener("toggle", () => { rep.hiddenOpen = fold.open; });
    fold.append(be("summary", null, `Hidden (${hidden.length})`));
    for (const r of hidden) fold.append(reportRow(r, was));
    body.append(fold);
  }
}
function reportRow(r, was) {  // the bar is the appliance's share of the month's total
  const row = be("div", "report-row"); row.dataset.plug = r.entity_id;
  const name = be("span", "name", r.name);
  if (r.appliance && r.plug_name !== r.name) name.append(be("span", "appl-of", ` · ${r.plug_name}`));
  row.append(name, be("span", "val", r.cost_p != null ? money(r.cost_p) : kwh(r.kwh)));
  const bar = be("div", "sum-bar"), i = be("i"); i.style.width = `${Math.min(100, r.share_pct)}%`; bar.append(i); row.append(bar);
  const meta = be("div", "report-meta");
  meta.append(be("span", "share", `${Math.round(r.share_pct)} %`));
  meta.append(be("span", null, `${kwh(r.kwh)}`));
  if (was && r.last_kwh != null) {
    const last = be("span", null, `${was}: ${r.last_cost_p != null ? money(r.last_cost_p) : kwh(r.last_kwh)}`);
    if (r.change_pct != null && r.change_pct !== 0) last.append(" ", be("b", r.change_pct > 0 ? "sum-up" : "sum-down", `${r.change_pct > 0 ? "▲" : "▼"} ${Math.abs(r.change_pct)} %`));
    meta.append(last);
  } else if (was) meta.append(be("span", null, `nothing in ${was}`));
  row.append(meta);
  return row;
}

// ---------- wiring ----------
(() => {
  for (const [id, close] of [["briefSheet", closeBrief], ["reportSheet", closeReport]]) {
    const s = $(id);
    s.addEventListener("click", (e) => { if (e.target === s) close(); });
  }
  $("briefClose").onclick = closeBrief; $("briefDone").onclick = closeBrief;
  $("reportClose").onclick = closeReport;
  $("briefAuto").onchange = (e) => { lsSet(BRIEF_AUTO, e.target.checked ? "1" : "0"); setStatus(e.target.checked ? "The brief opens each morning" : "The brief won't open by itself (⋯ → Morning brief)"); };
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("reportSheet").hidden) closeReport(); else if (!$("briefSheet").hidden) closeBrief();
  });
})();

// Once per morning, on the first visit of the day: not in wall mode, not over a sheet / dialog / edit mode, and not
// when the app was opened from a notification or link (?dev=, ?weather, …). Browser automation (the e2e tests) only
// gets it with hc.brief.auto = "1", so it never covers the plan in other tests.
function briefDue(now = new Date()) {
  const h = now.getHours(), auto = lsGet(BRIEF_AUTO);
  return h >= MORNING[0] && h < MORNING[1] && auto !== "0" && (auto === "1" || !navigator.webdriver) &&
    lsGet(BRIEF_SEEN) !== localDay(now) && !location.search;
}
document.addEventListener("DOMContentLoaded", () => {
  if (!briefDue()) return;
  loadBrief().then(() => {
    const busy = document.body.classList.contains("wall") || st.sheetFor || st.editing || document.querySelector("dialog[open]") ||
      [...document.querySelectorAll(".sheet")].some((s) => !s.hidden);
    if (!br.data || busy || !briefDue()) return;
    lsSet(BRIEF_SEEN, localDay());
    openBrief(false);
  });
});

window.openBrief = openBrief;
window.openReport = openReport;
})();
