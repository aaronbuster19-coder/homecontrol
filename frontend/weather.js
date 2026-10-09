"use strict";
// Outdoor weather: a compact chip in the status line (tap -> weather sheet: now, today's high/low, the next 12 h, the
// next 5 days, a cold-night hint, settings), a weather panel in the wall bar and the temperature on the dim screen.
// Data: GET /api/weather (HA's weather.* entity; forecasts cached 15 min on the server). Hooks: app.js renderSheet ->
// renderWeather(c) for st.sheetFor === WEATHER; wall.js -> weatherDim() for the dim screen.
const WEATHER = "@weather";
const WX_EVERY = 10 * 60e3;
const wx = { data: null, at: 0, err: null, busy: false, settingsOpen: false };
const wxe = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

// ---------- icons (inline SVG, colours from CSS variables via classes) ----------
const WX_CLOUD = "M7 17h10a3.5 3.5 0 0 0 .4-6.98A5.5 5.5 0 0 0 6.6 11.2 2.9 2.9 0 0 0 7 17z";
const WX_CLOUD_UP = "M7 14h10a3.5 3.5 0 0 0 .4-6.98A5.5 5.5 0 0 0 6.6 8.2 2.9 2.9 0 0 0 7 14z";
const wxSvg = (inner) => `<svg class="wx-ic" viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${inner}</svg>`;
const wxSun = (cx = 12, cy = 12, r = 4, ray = 3) => {
  let rays = "";
  for (let i = 0; i < 8; i++) {
    const a = i * Math.PI / 4, c = Math.cos(a), s = Math.sin(a), r0 = r + 2, r1 = r + 2 + ray;
    rays += `M${(cx + c * r0).toFixed(1)} ${(cy + s * r0).toFixed(1)}L${(cx + c * r1).toFixed(1)} ${(cy + s * r1).toFixed(1)}`;
  }
  return `<g class="wx-sun"><circle cx="${cx}" cy="${cy}" r="${r}"/><path d="${rays}"/></g>`;
};
const wxMoon = (sx = 0, sy = 0, k = 1) => `<path class="wx-moon" transform="translate(${sx} ${sy}) scale(${k})" d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>`;
const wxDrops = (n, cls = "wx-rain") => `<path class="${cls}" d="${["M8 17l-1 3", "M12 17l-1 3", "M16 17l-1 3"].slice(0, n).join("")}"/>`;
const wxFlakes = `<g class="wx-snow"><circle cx="8" cy="19" r=".9"/><circle cx="12" cy="20.5" r=".9"/><circle cx="16" cy="19" r=".9"/></g>`;
function wxIcon(cond, night = false) {
  const cloud = (up) => `<path class="wx-cloud" d="${up ? WX_CLOUD_UP : WX_CLOUD}"/>`;
  switch (cond) {
    case "sunny": return wxSvg(night ? wxMoon() : wxSun());
    case "clear-night": return wxSvg(wxMoon());
    case "partlycloudy": return wxSvg((night ? wxMoon(-3, -3, 0.75) : wxSun(8, 8, 3, 2)) + `<path class="wx-cloud" d="M9 20h8a3 3 0 0 0 .3-5.98A4.6 4.6 0 0 0 8.6 15.2 2.4 2.4 0 0 0 9 20z"/>`);
    case "cloudy": case "exceptional": return wxSvg(cloud(false));
    case "fog": return wxSvg(`${cloud(true)}<path class="wx-cloud" d="M5 17.5h14M7 20.5h10"/>`);
    case "rainy": return wxSvg(cloud(true) + wxDrops(2));
    case "pouring": return wxSvg(cloud(true) + wxDrops(3));
    case "snowy": return wxSvg(cloud(true) + wxFlakes);
    case "snowy-rainy": case "hail": return wxSvg(cloud(true) + wxDrops(1) + `<g class="wx-snow"><circle cx="12" cy="20" r=".9"/><circle cx="16" cy="19" r=".9"/></g>`);
    case "lightning": return wxSvg(`${cloud(true)}<path class="wx-bolt" d="M12.5 14l-2 3.5h3l-2 3.5"/>`);
    case "lightning-rainy": return wxSvg(`${cloud(true)}<path class="wx-bolt" d="M12.5 14l-2 3.5h3l-2 3.5"/>` + `<path class="wx-rain" d="M8 17l-1 3M17 17l-1 3"/>`);
    case "windy": case "windy-variant": return wxSvg(`<path class="wx-cloud" d="M3 9h11a3 3 0 1 0-3-3M3 13h15a3 3 0 1 1-3 3M3 17h7"/>`);
    default: return wxSvg(cloud(false));
  }
}
const WX_SNOWFLAKE = `<svg class="wx-ic" viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path class="wx-snow" d="M12 3v18M4.2 7.5l15.6 9M4.2 16.5l15.6-9M9.5 4.5 12 7l2.5-2.5M9.5 19.5 12 17l2.5 2.5"/></svg>`;
const isNightAt = (t) => { const h = new Date(t).getHours(); return h < 7 || h >= 19; };
const deg = (v) => v == null ? "–" : `${Math.round(v)}°`;

// ---------- data ----------
async function loadWeather(force = false) {
  if (wx.busy) return;
  wx.busy = true;
  try {
    wx.data = await api("/api/weather" + (force ? "/refresh" : ""), force ? { method: "POST" } : {});
    wx.err = null;
  } catch (e) { wx.err = e.message; }
  finally { wx.busy = false; wx.at = Date.now(); }
  paintWeather();
}
function paintWeather() {
  const d = wx.data, chip = $("wxChip"), ok = !!(d?.available && d.current);
  chip.hidden = !ok;
  if (ok) {
    const c = d.current;
    chip.innerHTML = wxIcon(c.condition, c.condition === "clear-night") + `<span>${deg(c.temperature)}</span>`;
    chip.title = `${c.text}, ${deg(c.temperature)} outside — weather`;
    chip.setAttribute("aria-label", chip.title);
  }
  wallWeather();
  if (st.sheetFor === WEATHER) renderSheet();
}
function wallWeather() {
  const stats = document.querySelector("#wallBar .wall-stats");
  if (!stats) return;
  let p = $("wallWx");
  if (!p) {
    p = wxe("button", "wall-stat wx"); p.id = "wallWx"; p.type = "button";
    p.onclick = () => openSheet(WEATHER);
    stats.prepend(p);
  }
  const d = wx.data, ok = !!(d?.available && d.current);
  p.hidden = !ok;
  if (!ok) return;
  const c = d.current;
  p.innerHTML = wxIcon(c.condition, c.condition === "clear-night");
  p.append(wxe("b", null, deg(c.temperature)));
  const t = d.today || {};
  p.append(wxe("span", "wx-more", [c.text, t.high != null && t.low != null ? `${deg(t.high)} / ${deg(t.low)}` : ""].filter(Boolean).join(" · ")));
  if (d.tonight?.cold) { const cold = wxe("span", "wx-cold-tag"); cold.innerHTML = WX_SNOWFLAKE; cold.append(d.tonight.text); p.append(cold); }
  p.setAttribute("aria-label", `Weather: ${c.text}, ${deg(c.temperature)}`);
}
// wall.js: what the dim screen shows (icon + temperature), or null
function weatherDim() {
  const c = wx.data?.available && wx.data.current;
  return c && c.temperature != null ? { icon: wxIcon(c.condition, c.condition === "clear-night"), text: deg(c.temperature) } : null;
}

// ---------- sheet ----------
// Built once per data change: device updates re-render the sheet and must not close an open <select>.
function renderWeather(c) {
  const d = wx.data;
  if (!d) {
    c.appendChild(wxe("h3", null, "Weather"));
    c.appendChild(wxe("div", "hist-msg" + (wx.err ? " err" : " loading"), wx.err ? `Couldn't load the weather: ${wx.err}` : "Loading…"));
    if (!wx.busy && !wx.err) loadWeather();
    return;
  }
  if (wx.node && wx.nodeFor === d) { c.appendChild(wx.node); return; }
  const box = wxe("div", "wx"); box.id = "weather";
  wx.node = box; wx.nodeFor = d; c.appendChild(box);
  box.appendChild(wxe("h3", null, "Weather"));
  if (!d.entity_id) {
    box.appendChild(wxe("div", "sub", "No weather entity in Home Assistant. Add the Met.no integration (Settings → Devices & services) — it creates weather.forecast_home."));
    return;
  }
  const cur = d.current;
  const ent = d.entities.find((e) => e.entity_id === d.entity_id);
  box.appendChild(wxe("div", "sub", `${ent?.name || d.entity_id}${cur ? "" : " — unavailable right now"}`));
  if (cur) {
    const now = wxe("div", "wx-now");
    const ic = wxe("span", "wx-big"); ic.innerHTML = wxIcon(cur.condition, cur.condition === "clear-night");
    const main = wxe("div", "wx-main");
    main.append(wxe("div", "wx-temp", deg(cur.temperature)), wxe("div", "wx-cond", cur.text));
    const facts = [];
    if (cur.apparent_temperature != null) facts.push(`Feels like ${deg(cur.apparent_temperature)}`);
    if (d.today?.high != null && d.today?.low != null) facts.push(`Today ${deg(d.today.high)} / ${deg(d.today.low)}`);
    if (cur.humidity != null) facts.push(`Humidity ${Math.round(cur.humidity)} %`);
    if (cur.wind_mph != null) facts.push(`Wind ${Math.round(cur.wind_mph)} mph${cur.wind_dir ? " " + cur.wind_dir : ""}`);
    const fx = wxe("div", "wx-facts"); facts.forEach((f) => fx.append(wxe("span", null, f)));
    now.append(ic, main); box.append(now, fx);
  }
  if (d.tonight?.cold) {
    const cold = wxe("div", "wx-cold"); cold.innerHTML = WX_SNOWFLAKE; cold.append(wxe("span", null, d.tonight.text)); box.append(cold);
  }
  if (d.hourly?.length) {
    box.append(wxe("h4", "wx-h", "Next 12 hours"));
    const strip = wxe("div", "wx-hours"); strip.setAttribute("role", "list");
    for (const h of d.hourly) {
      const cell = wxe("div", "wx-hour"); cell.setAttribute("role", "listitem");
      const t = new Date(h.t);
      cell.append(wxe("span", "wx-h-t", `${String(t.getHours()).padStart(2, "0")}:00`));
      const i = wxe("span", "wx-h-i"); i.innerHTML = wxIcon(h.condition, isNightAt(h.t)); cell.append(i);
      cell.append(wxe("span", "wx-h-v", deg(h.temperature)));
      if (h.precipitation_probability >= 30) cell.append(wxe("span", "wx-h-p", `${Math.round(h.precipitation_probability)} %`));
      strip.append(cell);
    }
    box.append(strip);
  }
  if (d.daily?.length) {
    box.append(wxe("h4", "wx-h", "Next 5 days"));
    const ul = wxe("ul", "wx-days");
    const lo = Math.min(...d.daily.map((x) => x.templow ?? x.temperature)), hi = Math.max(...d.daily.map((x) => x.temperature));
    const today = new Date().toDateString();
    for (const x of d.daily) {
      const li = wxe("li", "wx-day"), t = new Date(x.t);
      li.append(wxe("span", "wx-d-n", t.toDateString() === today ? "Today" : t.toLocaleDateString("en-GB", { weekday: "short" })));
      const i = wxe("span", "wx-d-i"); i.innerHTML = wxIcon(x.condition); li.append(i);
      li.append(wxe("span", "wx-d-lo", deg(x.templow)));
      // a bar from the day's low to its high on the 5-day scale
      const bar = wxe("span", "wx-d-bar"), fill = wxe("span", "wx-d-fill"), span = Math.max(1, hi - lo);
      fill.style.left = `${((x.templow ?? x.temperature) - lo) / span * 100}%`;
      fill.style.right = `${(hi - x.temperature) / span * 100}%`;
      bar.append(fill); li.append(bar, wxe("span", "wx-d-hi", deg(x.temperature)));
      ul.append(li);
    }
    box.append(ul);
  }
  if (d.forecast_error && !d.hourly?.length && !d.daily?.length) box.append(wxe("div", "hist-msg", "Forecast not available from Home Assistant (weather.get_forecasts needs HA 2023.9 or newer)."));
  weatherSettings(box, d);
}
function weatherSettings(box, d) {
  const det = wxe("details", "wx-set"); det.open = wx.settingsOpen; det.id = "weatherSettings";
  det.addEventListener("toggle", () => { wx.settingsOpen = det.open; });
  det.append(wxe("summary", null, "Weather settings"));
  const save = async (body, what) => {
    try { wx.data = await api("/api/weather/settings", { method: "PUT", body: JSON.stringify(body) }); setStatus(what); }
    catch (e) { setStatus(`Weather settings: ${e.message}`, true); }
    paintWeather();
  };
  if (d.entities.length) {
    const lab = wxe("label", null, "Weather entity ");
    const sel = wxe("select"); sel.id = "wxEntity";
    for (const e of d.entities) { const o = wxe("option", null, `${e.name} (${e.entity_id})`); o.value = e.entity_id; sel.append(o); }
    sel.value = d.entity_id;
    sel.onchange = () => save({ entity_id: sel.value }, "Weather source changed");
    lab.append(sel); det.append(lab);
  }
  const fl = wxe("label", "check"), cb = wxe("input"); cb.type = "checkbox"; cb.id = "wxFrost"; cb.checked = !!d.settings.frost_push;
  cb.onchange = () => save({ frost_push: cb.checked }, cb.checked ? "Frost push on" : "Frost push off");
  fl.append(cb, " Push “Frost tonight” when tonight's low is under 3° (sent from 17:00; held in quiet hours)");
  det.append(fl);
  box.append(det);
}

// ---------- wiring ----------
$("wxChip").onclick = () => openSheet(WEATHER);
$("weatherBtn").onclick = () => { wx.settingsOpen = true; openSheet(WEATHER); };
document.addEventListener("DOMContentLoaded", () => {
  loadWeather();
  setInterval(() => { if (!document.hidden) loadWeather(); }, WX_EVERY);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && Date.now() - wx.at > WX_EVERY) loadWeather(); });
  // "/?weather": the frost push tapped
  const fromUrl = (u) => { try { return new URL(u, location.origin).searchParams.has("weather"); } catch { return false; } };
  if (fromUrl(location.href)) { history.replaceState(null, "", location.pathname); openSheet(WEATHER); }
  navigator.serviceWorker?.addEventListener("message", (e) => { if (e.data?.type === "open" && fromUrl(e.data.url)) openSheet(WEATHER); });
});
