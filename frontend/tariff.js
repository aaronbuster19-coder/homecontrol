"use strict";
// Octopus tariff (⋯ → Tariff…) and the appliance run log (backend/tariff.py, backend/runlog.py).
// Tariff sheet (#tariffSheet, its own sheet so device updates never rebuild it under an open <select>): the price now,
// today's / tomorrow's half-hourly prices as bars, the cheapest time to run each linked washer / dishwasher / dryer,
// and (admins) the settings: product, region, use for costs, the opt-in reminder. Suggestions only — nothing here
// switches anything. The last good answer is kept in browser storage so the sheet still shows prices offline.
// Appliance sheet (hook applianceExtras from appliances.js): "Cheapest time to run" for cycle appliances and the
// run history with notes. "/?tariff" (the reminder push tapped) opens the sheet.
// Data: GET /api/tariff, PUT /api/tariff/settings, POST /api/tariff/refresh, GET /api/appliances/{id}/runs,
// PUT /api/appliances/{id}/runs/{run id} {"note"}.
const tf = { data: null, err: null, busy: false, timer: null, day: "Today", pick: null, cached: false, runsOpen: false, more: {} };
const TF_KEY = "hc.tariff";
const TF_SUGGEST = ["washer", "dishwasher", "dryer"];
const TF_NO_RUNS = ["fridge", "freezer", "home_server"];
const TF_LEADS = [5, 10, 15, 30, 45, 60, 90, 120];
const tfe = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const tfHm = (ms) => new Date(ms).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const tfP = (p) => p == null ? "–" : `${Math.abs(p) < 10 ? p.toFixed(1) : Math.round(p)}p`;
const tfAdmin = () => document.documentElement.dataset.role === "admin";
function tfDay(ms, now) {
  const d = new Date(ms), n = new Date(now ?? Date.now()), t = new Date(n); t.setDate(n.getDate() + 1);
  if (d.toDateString() === n.toDateString()) return "today";
  if (d.toDateString() === t.toDateString()) return "tomorrow";
  return d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
}
const tfDur = (m) => m < 60 ? `${m} min` : m % 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${Math.floor(m / 60)} h`;
// Price band of a half-hour against the day's average: what the bar colours mean.
function tfBand(p, avg) {
  if (p == null) return "none";
  if (p < 0) return "neg";
  if (avg == null) return "mid";
  return p <= avg * 0.75 ? "cheap" : p >= avg * 1.25 ? "dear" : "mid";
}

// ---------- sheet ----------
function tfSheet() {
  let s = $("tariffSheet");
  if (s) return s;
  s = tfe("div", "sheet tf-sheet"); s.id = "tariffSheet"; s.hidden = true;
  s.setAttribute("role", "dialog"); s.setAttribute("aria-label", "Tariff");
  const body = tfe("div", "sheet-body");
  const x = tfe("button", "close", "×"); x.id = "tariffClose"; x.setAttribute("aria-label", "Close"); x.onclick = closeTariff;
  const c = tfe("div"); c.id = "tariffContent";
  body.append(x, c); s.append(body); document.body.append(s);
  s.addEventListener("click", (e) => { if (e.target === s) closeTariff(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !s.hidden && !document.querySelector("dialog[open]")) closeTariff(); });
  return s;
}
function openTariff() {
  tfSheet().hidden = false; tf.pick = null; tfRender(); tfLoad();
  clearInterval(tf.timer); tf.timer = setInterval(() => { if (!document.hidden) tfLoad(); }, 60e3);
}
function closeTariff() { const s = $("tariffSheet"); if (s) s.hidden = true; clearInterval(tf.timer); tf.timer = null; }

function tfStore(d) { try { localStorage.setItem(TF_KEY, JSON.stringify(d)); } catch {} }
function tfCached() { try { return JSON.parse(localStorage.getItem(TF_KEY) || "null"); } catch { return null; } }
async function tfLoad() {
  if (tf.busy) return;
  tf.busy = true;
  try { tf.data = await api("/api/tariff"); tf.err = null; tf.cached = false; tfStore(tf.data); }
  catch (e) {
    tf.err = e.message;
    if (!tf.data) { const c = tfCached(); if (c) { tf.data = c; tf.cached = true; } }
  } finally { tf.busy = false; }
  tfShare();
  tfRender();
}
// The appliance sheet reads the same answer (hCache) so it needn't fetch again.
function tfShare() { if (tf.data && !tf.cached && typeof hCache !== "undefined") hCache.set("tariff|status", { at: Date.now(), data: tf.data }); }
async function tfSend(path, body, what, method = "PUT") {
  try {
    tf.data = await api(path, { method, body: body == null ? undefined : JSON.stringify(body) });
    tf.err = null; tf.cached = false; tfStore(tf.data); tfShare();
    for (const k of hCache.keys()) if (k.startsWith("energy|")) hCache.delete(k);
    if (what) setStatus(tf.data.error ? `${what} — ${tf.data.error}` : what, !!tf.data.error);
  } catch (e) { setStatus(`Tariff: ${e.message}`, true); }
  tfRender();
}

function tfRender() {
  const s = $("tariffSheet");
  if (!s || s.hidden) return;
  const c = $("tariffContent"), keep = s.querySelector(".sheet-body").scrollTop;
  c.replaceChildren(tfe("h3", null, "Tariff"));
  const d = tf.data;
  if (!d) {
    c.append(tfe("div", "hist-msg" + (tf.err ? " err" : " loading"), tf.err ? `Couldn't load: ${tf.err}` : "Loading…"));
    return;
  }
  c.append(tfe("div", "sub", d.enabled ? `${d.name} · ${d.region_name} (${d.region})` : "Octopus Energy half-hourly prices"));
  if (tf.cached || d.stale || d.error) {
    const when = d.fetched_at ? ` from ${tfDay(d.fetched_at, d.now)} ${tfHm(d.fetched_at)}` : "";
    const m = tfe("div", "tf-msg" + (d.error && !d.stale && !tf.cached ? " err" : ""));
    m.id = "tariffStatus";
    m.textContent = tf.cached ? `Offline — showing the last known prices${when}.` : d.stale ? `Showing the last known prices${when}. ${d.error}` : d.error;
    c.append(m);
  }
  if (d.enabled) {
    tfNow(c, d);
    tfChart(c, d);
    tfSuggest(c, d);
  } else {
    c.append(tfe("p", "hint", "Shows today's and tomorrow's prices from Octopus Energy (Agile, Go, …), the cheapest time to run the washing machine or dishwasher, and prices energy costs by the half-hour. Nothing is ever switched on automatically."));
    if (!tfAdmin()) c.append(tfe("p", "hint", "An admin can switch it on here."));
  }
  if (tfAdmin()) tfSettings(c, d);
  s.querySelector(".sheet-body").scrollTop = keep;
}

function tfNow(c, d) {
  const box = tfe("div", "tf-now"); box.id = "tariffNow";
  const today = d.today || {};
  box.dataset.band = tfBand(d.current?.p, today.avg);
  box.append(tfe("b", null, d.current ? tfP(d.current.p) : "–"));
  const t = tfe("span", null, d.current ? `per kWh now, until ${tfHm(d.current.end)}` : "No price known for now yet");
  if (d.next) t.append(tfe("span", "tf-next", ` · then ${tfP(d.next.p)}`));
  box.append(t);
  c.append(box);
}

function tfChart(c, d) {
  const sec = tfe("section", "tf-sec");
  const top = tfe("div", "tf-top");
  top.append(segmented(["Today", "Tomorrow"], tf.day, (v) => { tf.day = v; tf.pick = null; tfRender(); }));
  const day = tf.day === "Tomorrow" ? d.tomorrow : d.today;
  const sum = tfe("div", "tf-sum");
  if (day.known) sum.textContent = `Low ${tfP(day.min)} at ${tfHm(day.cheapest.start)} · avg ${tfP(day.avg)} · high ${tfP(day.max)}`;
  top.append(sum); sec.append(top);
  if (!day.known) {
    sec.append(tfe("div", "hist-msg", tf.day === "Tomorrow" ? "Tomorrow's prices usually arrive after 16:00." : "No prices for today yet."));
    c.append(sec); return;
  }
  const lo = Math.min(0, day.min), hi = Math.max(1, day.max), range = hi - lo, zero = (-lo / range) * 100;
  const bars = tfe("div", "tf-bars"); bars.id = "tariffBars"; bars.setAttribute("role", "img");
  bars.setAttribute("aria-label", `${day.label}: ${sum.textContent}`);
  const cap = tfe("div", "tf-cap"); cap.id = "tariffCap";
  const nowMs = d.now;
  const show = (x) => {
    cap.textContent = x ? `${tfHm(x.start)}–${tfHm(x.start + 1800e3)} · ${x.p == null ? "no price yet" : tfP(x.p) + "/kWh"}`
      : d.current && tf.day === "Today" ? `Now ${tfP(d.current.p)}/kWh — tap a bar for its price` : "Tap a bar for its price";
  };
  for (const x of day.slots) {
    const b = tfe("button", `tf-bar b-${tfBand(x.p, day.avg)}`); b.type = "button"; b.dataset.start = x.start;
    if (nowMs >= x.start && nowMs < x.start + 1800e3) b.classList.add("now");
    if (tf.pick === x.start) b.classList.add("pick");
    if (x.start < nowMs - 1800e3 + 1) b.classList.add("past");
    const i = tfe("i");
    if (x.p != null) {
      const h = Math.max(1.5, Math.abs(x.p) / range * 100);
      i.style.height = `${h}%`; i.style.bottom = x.p >= 0 ? `${zero}%` : `${zero - h}%`;
    }
    b.append(i);
    b.setAttribute("aria-label", `${tfHm(x.start)} ${x.p == null ? "no price" : tfP(x.p)}`);
    b.onclick = () => { tf.pick = x.start; for (const o of bars.querySelectorAll(".pick")) o.classList.remove("pick"); b.classList.add("pick"); show(x); };
    b.onpointerenter = (e) => { if (e.pointerType === "mouse") show(x); };
    bars.append(b);
  }
  bars.onpointerleave = () => show(tf.pick != null ? day.slots.find((x) => x.start === tf.pick) : null);
  const axis = tfe("div", "tf-axis");
  for (const h of [0, 6, 12, 18]) {
    const idx = day.slots.findIndex((x) => new Date(x.start).getHours() === h && new Date(x.start).getMinutes() === 0);
    if (idx < 0) continue;
    const l = tfe("span", null, `${String(h).padStart(2, "0")}:00`); l.style.left = `${idx / day.slots.length * 100}%`; axis.append(l);
  }
  show(tf.pick != null ? day.slots.find((x) => x.start === tf.pick) : null);
  sec.append(bars, axis, cap);
  const key = tfe("div", "tf-key");
  for (const [cls, t] of [["cheap", "cheap"], ["mid", "average"], ["dear", "dear"], ...(day.min < 0 ? [["neg", "paid to use"]] : [])]) {
    const k = tfe("span", `k-${cls}`); k.append(tfe("i"), t); key.append(k);
  }
  sec.append(key);
  c.append(sec);
}

function tfWindow(g, now) {
  if (g.running) return { text: "Running now", sub: "" };
  if (g.start == null) return { text: `Not enough prices known yet for a ${tfDur(g.minutes)} run`, sub: "" };
  const when = g.starts_now ? `Now until ${tfHm(g.end)}` : `${tfHm(g.start)}–${tfHm(g.end)} ${tfDay(g.start, now)}`;
  const bits = [`avg ${tfP(g.avg_p)}/kWh`];
  if (g.est_p != null) bits.push(`≈ ${fmtP(g.est_p) || "0p"} a run`);
  if (g.saving_p != null && g.saving_p >= 1) bits.push(`${fmtP(g.saving_p)} less than now`);
  return { text: when, sub: bits.join(" · ") };
}
function tfSuggest(c, d) {
  const sec = tfe("section", "tf-sec"); sec.id = "tariffSuggest";
  sec.append(tfe("h4", null, "Best time to run"));
  if (!d.suggestions.length) {
    sec.append(tfe("p", "hint", "Link a washing machine, dishwasher or tumble dryer to its plug (Edit → select it → Link plug) to see the cheapest time to run it."));
    c.append(sec); return;
  }
  const ul = tfe("ul", "tf-list");
  for (const g of d.suggestions) {
    const li = tfe("li"); li.dataset.appl = g.fid;
    const w = tfWindow(g, d.now);
    const head = tfe("div", "tf-row"); head.append(tfe("span", "name", g.name), tfe("span", "tf-when", w.text));
    li.append(head);
    if (w.sub) li.append(tfe("div", "tf-row-sub", w.sub));
    li.append(tfe("div", "tf-row-src", g.source === "history" ? `Typical run ${tfDur(g.minutes)} (from its recent cycles)`
      : g.source === "runs" ? `Typical run ${tfDur(g.minutes)} (from its run log)` : `Assumes a ${tfDur(g.minutes)} run — no cycles recorded yet`));
    ul.append(li);
  }
  sec.append(ul, tfe("p", "hint", "Suggestions only — nothing is switched on automatically."));
  c.append(sec);
}

function tfSettings(c, d) {
  const s = d.settings, sec = tfe("section", "tf-sec tf-settings"); sec.id = "tariffSettings";
  sec.append(tfe("h4", null, "Settings"));
  const check = (id, on, text, fn) => {
    const l = tfe("label", "check"), cb = tfe("input"); cb.type = "checkbox"; cb.id = id; cb.checked = on;
    cb.onchange = () => fn(cb.checked); l.append(cb, tfe("span", null, text)); return l;
  };
  sec.append(check("tariffEnabled", s.enabled, "Use Octopus prices", (v) => tfSend("/api/tariff/settings", { enabled: v }, v ? "Tariff on" : "Tariff off")));
  const opts = tfe("div", "tf-opts" + (s.enabled ? "" : " off"));
  // product: a preset or any other Octopus product code
  const prow = tfe("label", "tf-field"); prow.append(tfe("span", null, "Tariff"));
  const sel = tfe("select"); sel.id = "tariffProduct";
  for (const p of d.presets) sel.append(new Option(p.name, p.product));
  sel.append(new Option("Other product code…", "other"));
  const known = d.presets.some((p) => p.product === s.product);
  sel.value = known ? s.product : "other";
  const code = tfe("input"); code.id = "tariffCode"; code.type = "text"; code.value = s.product; code.hidden = known;
  code.placeholder = "e.g. AGILE-24-10-01"; code.autocapitalize = "characters"; code.spellcheck = false; code.maxLength = 49;
  code.setAttribute("aria-label", "Octopus product code");
  sel.onchange = () => {
    if (sel.value === "other") { code.hidden = false; code.focus(); return; }
    tfSend("/api/tariff/settings", { product: sel.value }, "Tariff changed");
  };
  code.onchange = () => { if (code.value.trim()) tfSend("/api/tariff/settings", { product: code.value.trim() }, "Product code saved"); };
  prow.append(sel); opts.append(prow, code);
  const rrow = tfe("label", "tf-field"); rrow.append(tfe("span", null, "Region"));
  const reg = tfe("select"); reg.id = "tariffRegion";
  for (const [k, v] of Object.entries(d.regions)) reg.append(new Option(`${k} — ${v}`, k));
  reg.value = s.region; reg.onchange = () => tfSend("/api/tariff/settings", { region: reg.value }, "Region changed");
  rrow.append(reg); opts.append(rrow);
  opts.append(tfe("p", "hint", "The region is the letter at the end of your tariff code on an Octopus bill (e.g. E-1R-AGILE-24-10-01-C → C, London)."));
  opts.append(check("tariffCosts", s.use_for_costs, "Price energy costs with these half-hourly rates", (v) => tfSend("/api/tariff/settings", { use_for_costs: v }, v ? "Costs use Octopus prices" : "Costs use the flat unit rate")));
  const rem = tfe("div", "tf-remind");
  rem.append(check("tariffRemind", s.remind, "Remind me before the cheapest time", (v) => tfSend("/api/tariff/settings", { remind: v }, v ? "Reminder on" : "Reminder off")));
  const lead = tfe("select"); lead.id = "tariffLead"; lead.setAttribute("aria-label", "How long before");
  for (const m of TF_LEADS) lead.append(new Option(`${m} min before`, m));
  lead.value = String(s.remind_lead_min); lead.disabled = !s.remind;
  lead.onchange = () => tfSend("/api/tariff/settings", { remind_lead_min: Number(lead.value) }, "Reminder time saved");
  rem.append(lead); opts.append(rem);
  opts.append(tfe("p", "hint", "Off by default. One push per appliance before its cheapest window; none during quiet hours or while it's running. It never switches anything on."));
  sec.append(opts);
  if (s.enabled) {
    const r = tfe("div", "tf-refresh");
    const b = tfe("button", null, "Refresh prices"); b.type = "button"; b.id = "tariffRefresh";
    b.onclick = () => tfSend("/api/tariff/refresh", null, "Prices refreshed", "POST");
    r.append(b);
    if (d.fetched_at) r.append(tfe("span", "hint", `Updated ${tfDay(d.fetched_at, d.now)} ${tfHm(d.fetched_at)}`));
    sec.append(r);
  }
  c.append(sec);
}

// ---------- appliance sheet: cheapest time + run log ----------
function applianceExtras(f, sec) {
  if (TF_SUGGEST.includes(f.type) && typeof hFetch === "function") {
    const hit = hFetch("tariff|status", "/api/tariff", 60e3);
    const g = hit?.data?.enabled && hit.data.suggestions.find((x) => x.fid === f.id);
    if (g) {
      const w = tfWindow(g, hit.data.now);
      const box = tfe("div", "tf-appl"); box.id = "applCheapest";
      const r = tfe("div", "sum-row"); r.append(tfe("span", null, "Cheapest time to run"), tfe("span", null, w.text));
      box.append(r);
      if (w.sub) box.append(tfe("div", "hint", w.sub));
      const more = tfe("button", "linkish", "Prices →"); more.type = "button"; more.onclick = () => { closeSheet(); openTariff(); };
      box.append(more);
      sec.append(box);
    }
  }
  if (!TF_NO_RUNS.includes(f.type)) runLog(f, sec);
}

function runLog(f, sec) {
  const det = tfe("details", "tf-runs"); det.open = tf.runsOpen; det.id = "applRuns";
  // Rebuilding the sheet with the fold open fires "toggle" too: only a real change re-renders (to load the runs).
  det.addEventListener("toggle", () => { if (det.open !== tf.runsOpen) { tf.runsOpen = det.open; if (det.open) renderSheet(); } });
  det.append(tfe("summary", null, "Run history"));
  sec.append(det);
  if (!det.open) return;
  const key = `runs|${f.id}|${f.plug}`;
  const hit = hFetch(key, `/api/appliances/${encodeURIComponent(f.id)}/runs`, 30e3);
  if (!hit) {
    const err = hCache.get(key)?.error;
    det.append(tfe("div", "hist-msg" + (err && !hPending.has(key) ? " err" : " loading"), err && !hPending.has(key) ? `Couldn't load runs: ${err}` : "Loading…"));
    return;
  }
  const d = hit.data, extra = tf.more[key]?.runs || [];
  if (d.current) {
    const cur = tfe("div", "tf-run cur");
    cur.append(tfe("div", "tf-run-top", `Running since ${tfHm(d.current.start)}` + (d.current.kwh != null ? ` · ${d.current.kwh.toFixed(2)} kWh so far` : "")));
    det.append(cur);
  }
  const runs = [...d.runs, ...extra];
  if (!runs.length && !d.current) { det.append(tfe("div", "hist-msg", "No runs recorded yet. Each run is logged when it ends.")); return; }
  const ul = tfe("ul", "tf-run-list");
  for (const r of runs) {
    const li = tfe("li", "tf-run"); li.dataset.run = r.id;
    const day = new Date(r.start).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
    const top = tfe("div", "tf-run-top");
    top.append(tfe("span", null, `${day} · ${tfHm(r.start)}–${tfHm(r.end)}`), tfe("span", "tf-run-dur", tfDur(Math.max(1, r.minutes))));
    const bits = [];
    if (r.kwh != null) bits.push(`${r.kwh.toFixed(2)} kWh${r.partial ? "+" : ""}`);
    if (r.cost_p != null) bits.push(fmtP(r.cost_p) || "0p");
    if (r.outcome === "stopped") bits.push("switched off before it finished");
    if (r.partial) bits.push("energy since a restart only");
    const sub = tfe("div", "tf-run-sub", bits.join(" · "));
    li.append(top, sub);
    if (r.note) li.append(tfe("div", "tf-note", r.note));
    const nb = tfe("button", "linkish tf-note-btn", r.note ? "✎ Edit note" : "+ Add note"); nb.type = "button";
    nb.onclick = () => noteDialog(f, r, key);
    li.append(nb);
    ul.append(li);
  }
  det.append(ul);
  const next = tf.more[key]?.next !== undefined ? tf.more[key].next : d.next_before;
  if (next) {
    const b = tfe("button", null, "Load older"); b.type = "button"; b.id = "runsMore";
    b.onclick = async () => {
      try {
        const o = await api(`/api/appliances/${encodeURIComponent(f.id)}/runs?before=${next}`);
        tf.more[key] = { runs: [...extra, ...o.runs], next: o.next_before };
        renderSheet();
      } catch (e) { setStatus(`Runs: ${e.message}`, true); }
    };
    det.append(b);
  }
}

document.body.insertAdjacentHTML("beforeend", `
<dialog id="runNoteDialog">
  <form method="dialog" id="runNoteForm">
    <h3>Note</h3>
    <p class="hint" id="runNoteWhat"></p>
    <label>Note on this run <input name="note" maxlength="200" autocomplete="off" placeholder="e.g. 40° cotton, towels"></label>
    <p class="hint warn" id="runNoteMsg"></p>
    <menu><button value="cancel" formnovalidate>Cancel</button><button value="ok" class="primary" id="runNoteOk">Save</button></menu>
  </form>
</dialog>`);
function noteDialog(f, r, key) {
  const dlg = $("runNoteDialog"), form = $("runNoteForm");
  form.note.value = r.note || ""; $("runNoteMsg").textContent = "";
  $("runNoteWhat").textContent = `${applName(f)} · ${new Date(r.start).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" })} ${tfHm(r.start)}–${tfHm(r.end)}`;
  $("runNoteOk").onclick = async (e) => {
    e.preventDefault();
    try {
      const saved = await api(`/api/appliances/${encodeURIComponent(f.id)}/runs/${r.id}`, { method: "PUT", body: JSON.stringify({ note: form.note.value }) });
      r.note = saved.note;
      const hit = hCache.get(key);
      if (hit?.data) for (const x of hit.data.runs) if (x.id === r.id) x.note = saved.note;
      dlg.close(); setStatus(saved.note ? "Note saved" : "Note removed");
      if (st.sheetFor) renderSheet();
    } catch (err) { $("runNoteMsg").textContent = err.message; }
  };
  dlg.returnValue = ""; dlg.showModal(); form.note.focus();
}

// ---------- wiring: ⋯ menu entry (after Energy), push link, energy sheet note ----------
(() => {
  const b = tfe("button", null, "Tariff…"); b.id = "tariffMenuBtn"; b.type = "button"; b.setAttribute("role", "menuitem");
  const anchor = $("energyBtn");
  if (anchor) anchor.after(b); else $("moreMenu").prepend(b);
  b.addEventListener("click", openTariff);
  const fromUrl = (u) => { try { return new URL(u, location.origin).searchParams.has("tariff"); } catch { return false; } };
  document.addEventListener("DOMContentLoaded", () => {
    if (fromUrl(location.href)) { history.replaceState(null, "", location.pathname); openTariff(); }
    navigator.serviceWorker?.addEventListener("message", (e) => { if (e.data?.type === "open" && fromUrl(e.data.url)) openTariff(); });
  });
})();
