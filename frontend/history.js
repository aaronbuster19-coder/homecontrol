"use strict";
// History charts in device sheets (plug W, valve °C, light/door on-off bars) and the door log sheet. Hooks from app.js:
// historySection(d, c) at the end of renderSheet, renderDoors(c) for st.sheetFor === DOORS, groupActions(kind, li) in renderSide.
const DOORS = "@doors";
const H_TTL = { "24h": 60e3, "7d": 600e3, "30d": 600e3 };
const hCache = new Map(); // key -> {at, data} | {at, error} ; pending promises in hPending
const hPending = new Map();
const hState = { open: false, range: {}, doorRange: "24h" };
try { hState.open = localStorage.getItem("hc-hist-open") === "1"; } catch {}
const hx = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const pad2 = (n) => String(n).padStart(2, "0");
const hhmm = (t) => { const d = new Date(t); return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`; };
const dayName = (t) => new Date(t).toLocaleDateString([], { weekday: "short" });
const dayDate = (t) => new Date(t).toLocaleDateString([], { day: "numeric", month: "short" });
const whenLabel = (t, rng) => rng === "24h" ? hhmm(t) : `${dayName(t)} ${dayDate(t)} ${hhmm(t)}`;
function fmtDur(ms) {
  const m = Math.round(ms / 60e3);
  if (m < 1) return "<1 min";
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${h} h ${m % 60} min` : `${h} h`;
  return `${Math.floor(h / 24)} d ${h % 24} h`;
}
const fmtNum = (v, unit) => unit === "W" ? fmtW(v) : `${Math.round(v * 10) / 10}${unit === "°C" ? "°" : " " + unit}`;

// ---------- data ----------
function hFetch(key, url, ttl) {
  const hit = hCache.get(key);
  if (hit && Date.now() - hit.at < (hit.error ? 5e3 : ttl)) return hit;
  if (!hPending.has(key)) {
    hPending.set(key, api(url).then((data) => hCache.set(key, { at: Date.now(), data }), (e) => hCache.set(key, { at: Date.now(), error: e.message }))
      .finally(() => { hPending.delete(key); if (st.sheetFor) renderSheet(); }));
  }
  return hit && !hit.error ? hit : null; // stale data shows while refreshing
}

// ---------- sheet section ----------
function segmented(opts, value, onPick) {
  const g = hx("div", "seg"); g.setAttribute("role", "group");
  for (const o of opts) {
    const b = hx("button", o === value ? "active" : "", o); b.type = "button"; b.setAttribute("aria-pressed", o === value);
    b.onclick = (e) => { e.preventDefault(); e.stopPropagation(); onPick(o); };
    g.appendChild(b);
  }
  return g;
}
function historySection(d, c) {
  if (!d || !["plug", "valve", "light", "sensor", "dehumidifier"].includes(d.kind)) return;
  const box = hx("details", "hist"); box.open = hState.open; c.appendChild(box);
  const sum = hx("summary", null); sum.append(hx("span", null, "History"));
  const rng = hState.range[d.kind] || "24h";
  sum.appendChild(segmented(["24h", "7d", "30d"], rng, (r) => { hState.range[d.kind] = r; hState.open = true; renderSheet(); }));
  box.appendChild(sum);
  box.addEventListener("toggle", () => {
    if (box.open === hState.open) return;
    hState.open = box.open; try { localStorage.setItem("hc-hist-open", box.open ? "1" : "0"); } catch {}
    if (box.open) renderSheet();
  });
  if (d.kind === "sensor") {
    const a = hx("button", "linkish", "Door log →"); a.type = "button"; a.onclick = () => openSheet(DOORS); box.after(a);
  }
  if (!box.open) return;
  const body = hx("div", "hist-body"); box.appendChild(body);
  const key = `${d.entity_id}|${rng}`;
  const hit = hFetch(key, `/api/history/${encodeURIComponent(d.entity_id)}?range=${rng}`, H_TTL[rng]);
  const err = hCache.get(key)?.error;
  if (!hit) {
    if (err && !hPending.has(key)) {
      const e = hx("div", "hist-msg err", `Couldn't load history: ${err}`);
      const r = hx("button", null, "Retry"); r.onclick = () => { hCache.delete(key); renderSheet(); }; e.appendChild(r);
      body.appendChild(e);
    } else body.appendChild(hx("div", "hist-msg loading", "Loading history…"));
    return;
  }
  drawHistory(body, d, hit.data);
}

// ---------- chart ----------
const SVGNS = "http://www.w3.org/2000/svg";
const sv = (tag, attrs, parent) => { const e = document.createElementNS(SVGNS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; };
function timeTicks(start, end, rng) {
  const out = [], t = new Date(start);
  if (rng === "24h") { t.setMinutes(0, 0, 0); t.setHours(Math.ceil((t.getHours() + (t.getTime() < start ? 1 : 0)) / 6) * 6); }
  else { t.setHours(0, 0, 0, 0); t.setDate(t.getDate() + 1); }
  const stepDays = rng === "30d" ? 7 : 1;
  while (t.getTime() < end) {
    out.push(t.getTime());
    if (rng === "24h") t.setHours(t.getHours() + 6); else t.setDate(t.getDate() + stepDays);
  }
  return out;
}
const tickLabel = (t, rng) => rng === "24h" ? hhmm(t) : rng === "7d" ? dayName(t) : dayDate(t);
function nearest(points, t) { // last non-null point at or before t (step semantics), else first after
  let lo = 0, hi = points.length - 1, best = -1;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (points[m][0] <= t) { best = m; lo = m + 1; } else hi = m - 1; }
  if (best >= 0 && points[best][1] == null) return null;
  if (best < 0) return null;
  return points[best];
}
function drawHistory(body, d, data) {
  const series = (data.series || []).filter((s) => s.points.length);
  const timeline = data.kind === "plug" ? [] : data.timeline || [];
  body.classList.add(`${data.kind}-hist`);
  const readout = hx("div", "hist-readout"); body.appendChild(readout);
  const W = Math.max(260, Math.round(body.clientWidth || 340)), PR = 4;
  const isLine = data.kind === "plug" || data.kind === "valve" || data.kind === "dehumidifier";
  const PL = isLine ? 40 : 4;
  const H = isLine ? 150 : 64, PT = isLine ? 14 : 8, PB = 20;
  const x = (t) => PL + (t - data.start) / (data.end - data.start) * (W - PL - PR);
  const tAt = (px) => data.start + (px - PL) / (W - PL - PR) * (data.end - data.start);
  const s = sv("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", class: "hist-chart", role: "img" });
  body.appendChild(s);
  for (const t of timeTicks(data.start, data.end, data.range)) {
    sv("line", { x1: x(t), x2: x(t), y1: PT, y2: H - PB, class: "grid" }, s);
    const lb = sv("text", { x: x(t), y: H - 6, class: "tick", "text-anchor": "middle" }, s); lb.textContent = tickLabel(t, data.range);
  }
  let summary = "", y = null;
  if (isLine) {
    const vals = series.flatMap((se) => se.points.map((p) => p[1]).filter((v) => v != null));
    if (!vals.length) { body.replaceChildren(hx("div", "hist-msg", "No data in this period.")); return; }
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (data.kind === "plug") lo = 0; else { lo = Math.floor(lo - 0.5); hi = Math.ceil(hi + 0.5); }
    if (hi - lo < 1e-9) hi = lo + 1;
    y = (v) => PT + (1 - (v - lo) / (hi - lo)) * (H - PT - PB);
    for (const v of [lo, hi]) sv("line", { x1: PL, x2: W - PR, y1: y(v), y2: y(v), class: "grid base" }, s);
    const unit = series[0].unit;
    const ylab = (v, yy) => { const t = sv("text", { x: PL - 5, y: yy, class: "tick ylab", "text-anchor": "end", "dominant-baseline": "middle" }, s); t.textContent = fmtNum(v, unit); };
    ylab(hi, y(hi)); ylab(lo, y(lo));
    series.forEach((se, i) => {
      let path = "", area = "", runStart = null, prevY = null, prevT = null;
      for (const [t, v] of se.points) {
        if (v == null) { if (runStart != null) area += `L${x(prevT)},${y(lo)}L${runStart},${y(lo)}Z`; runStart = null; prevY = null; continue; }
        const X = x(t), Y = y(v);
        if (prevY == null) { path += `M${X},${Y}`; area += `M${X},${y(lo)}L${X},${Y}`; runStart = X; }
        else if (se.step) { path += `H${X}V${Y}`; area += `H${X}V${Y}`; }
        else { path += `L${X},${Y}`; area += `L${X},${Y}`; }
        prevY = Y; prevT = t;
      }
      if (runStart != null) area += `L${x(prevT)},${y(lo)}L${runStart},${y(lo)}Z`;
      const cls = `s${i}` + (se.step ? " step" : "");
      if (i === 0) sv("path", { d: area, class: `area ${cls}` }, s);
      sv("path", { d: path, class: `line ${cls}` }, s);
    });
    if (data.kind === "plug") {
      summary = [data.energy_kwh != null ? `${data.energy_kwh.toFixed(2)} kWh in ${data.range}` : "",
        data.energy_today_kwh != null ? `today ${data.energy_today_kwh.toFixed(2)} kWh` : "", `peak ${fmtW(Math.max(...vals))}`].filter(Boolean).join(" · ");
    } else {
      const cur = series.find((x) => x.name === "Current")?.points.map((p) => p[1]).filter((v) => v != null) || [];
      const u = unit === "%" ? " %" : "°";
      summary = cur.length ? `Current ${Math.min(...cur)}–${Math.max(...cur)}${u}` : "";
      const legend = hx("div", "hist-legend");
      legend.innerHTML = `<span><i class="sw s0"></i>Current</span>` + (series.length > 1 ? `<span><i class="sw s1"></i>Target</span>` : "");
      body.appendChild(legend);
    }
  } else {
    if (!timeline.length) { body.replaceChildren(hx("div", "hist-msg", "No data in this period.")); return; }
    const on = data.kind === "sensor" ? "open" : "on";
    const by = PT + 6, bh = H - PB - PT - 12;
    sv("rect", { x: PL, y: by, width: W - PL - PR, height: bh, rx: 4, class: "bar-bg" }, s);
    for (const r of timeline) {
      const w = Math.max(r.state === on ? 1.5 : 0, x(r.end) - x(r.start));
      sv("rect", { x: x(r.start), y: by, width: w, height: bh, class: `bar ${data.kind} ${r.state}` }, s);
    }
    const onMs = timeline.filter((r) => r.state === on).reduce((a, r) => a + r.end - r.start, 0);
    const n = timeline.filter((r) => r.state === on).length;
    summary = `${data.kind === "sensor" ? "Open" : "On"} ${fmtDur(onMs)} in total · ${n} time${n === 1 ? "" : "s"}`;
  }
  readout.textContent = summary || " ";
  // tap / hover readout
  const cursor = sv("line", { y1: PT, y2: H - PB, class: "cursor", visibility: "hidden" }, s);
  const dots = series.map((_, i) => sv("circle", { r: 3.5, class: `dot s${i}`, visibility: "hidden" }, s));
  const show = (e) => {
    const r = s.getBoundingClientRect(); const px = (e.clientX - r.left) / r.width * W;
    const t = Math.min(data.end, Math.max(data.start, tAt(px)));
    cursor.setAttribute("x1", x(t)); cursor.setAttribute("x2", x(t)); cursor.setAttribute("visibility", "visible");
    holdSheet(4000);
    const parts = [];
    if (isLine) {
      series.forEach((se, i) => {
      const p = nearest(se.points, t);
      if (!p) return dots[i].setAttribute("visibility", "hidden");
      parts.push(`${series.length > 1 ? se.name + " " : ""}${fmtNum(p[1], se.unit)}`);
      dots[i].setAttribute("cx", x(t)); dots[i].setAttribute("cy", y(p[1])); dots[i].setAttribute("visibility", "visible");
      });
    } else {
      const run = timeline.find((q) => q.start <= t && t < q.end);
      parts.push(run ? `${run.state} ${whenLabel(run.start, data.range)}–${whenLabel(run.end, data.range)} (${fmtDur(run.end - run.start)})` : "no data");
    }
    readout.textContent = `${whenLabel(t, data.range)} · ${parts.join(" · ") || "no data"}`;
    readout.classList.add("active");
  };
  const hide = () => { cursor.setAttribute("visibility", "hidden"); dots.forEach((d) => d.setAttribute("visibility", "hidden")); readout.textContent = summary || " "; readout.classList.remove("active"); };
  s.addEventListener("pointerdown", show);
  s.addEventListener("pointermove", (e) => { if (e.pointerType === "mouse" || e.buttons) show(e); });
  s.addEventListener("pointerleave", (e) => { if (e.pointerType === "mouse") hide(); });
  if (data.kind === "dehumidifier") dehumRunBar(body, data);
}

// ---------- door log ----------
function groupActions(kind, li) {
  if (kind !== "sensor") return;
  const b = hx("button", "group-act", "Log"); b.type = "button"; b.title = "Door log — opens and closes";
  b.onclick = (e) => { e.stopPropagation(); openSheet(DOORS); };
  li.appendChild(b);
}
function renderDoors(c) {
  c.appendChild(hx("h3", null, "Door log"));
  const rng = hState.doorRange;
  const top = hx("div", "doors-top"); top.appendChild(hx("div", "sub", "Opens and closes, newest first."));
  top.appendChild(segmented(["24h", "7d"], rng, (r) => { hState.doorRange = r; renderSheet(); }));
  c.appendChild(top);
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  const key = `doors|${rng}|${tz}`;
  const hit = hFetch(key, `/api/doors/log?range=${rng}&tz=${encodeURIComponent(tz)}`, H_TTL[rng]);
  const box = hx("div", "doors"); c.appendChild(box);
  if (!hit) {
    const err = hCache.get(key)?.error;
    box.appendChild(hx("div", "hist-msg" + (err && !hPending.has(key) ? " err" : " loading"), err && !hPending.has(key) ? `Couldn't load the log: ${err}` : "Loading…"));
    return;
  }
  const data = hit.data;
  if (!data.doors.length) { box.appendChild(hx("div", "hist-msg", "No door sensors found.")); return; }
  const today = data.today_start, yest = today - 864e5;
  const dayHead = (t) => t >= today ? "Today" : t >= yest ? "Yesterday" : `${dayName(t)} ${dayDate(t)}`;
  for (const dr of data.doors) {
    const sec = hx("section", "door"); box.appendChild(sec);
    const head = hx("div", "door-head"); head.appendChild(hx("span", "name", dr.name));
    const live = st.devices.get(dr.entity_id); const open = live ? live.state === "on" : dr.state === "open";
    const badge = hx("span", "badge small", open ? "Open" : "Closed"); badge.style.background = open ? "var(--open)" : "var(--closed)";
    head.appendChild(badge); sec.appendChild(head);
    const sm = dr.summary, bits = [`${sm.opens_today} open${sm.opens_today === 1 ? "" : "s"} today`];
    if (sm.longest_open_ms) bits.push(`longest ${fmtDur(sm.longest_open_ms)}`);
    if (sm.open_since != null) bits.push(`open since ${sm.open_since_before_range ? "before " : ""}${whenLabel(sm.open_since, sm.open_since >= today ? "24h" : "7d")}`);
    sec.appendChild(hx("div", "sub", bits.join(" · ")));
    if (!dr.events.length) { sec.appendChild(hx("div", "hist-msg", `No opens or closes in the last ${rng === "24h" ? "24 hours" : "7 days"}.`)); continue; }
    const ul = hx("ul", "door-events"); sec.appendChild(ul);
    let lastDay = null;
    dr.events.forEach((e, i) => {
      const dh = dayHead(e.t);
      if (dh !== lastDay) { ul.appendChild(hx("li", "day", dh)); lastDay = dh; }
      const li = hx("li", `ev ${e.state}`);
      li.append(hx("span", "t", hhmm(e.t)), hx("i", "dot"));
      let txt = e.state === "open" ? "Opened" : "Closed";
      if (e.state === "closed" && e.open_ms != null) txt += ` · open for ${fmtDur(e.open_ms)}`;
      if (e.state === "open" && i === 0 && dr.state === "open") txt += ` · still open (${fmtDur(data.end - e.t)})`;
      li.appendChild(hx("span", "what", txt));
      ul.appendChild(li);
    });
  }
}
