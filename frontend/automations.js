"use strict";
// Automation settings in the bell sheet (window heating, device health, weekly summary) and the "This week" sheet.
(() => {
  const el = (id) => document.getElementById(id);
  const msg = (text, warn = false) => { el("alertMsg").textContent = text; el("alertMsg").classList.toggle("warn", warn); };
  const put = (body) => api("/api/alerts/settings", { method: "PUT", body: JSON.stringify(body) });

  // ---------- bell sheet ----------
  function syncOpts() { el("winHeatOpts").classList.toggle("off", !el("winHeat").checked); }
  async function refresh() {
    try {
      const [s, status] = await Promise.all([api("/api/alerts/settings"), api("/api/automations/status")]);
      el("winHeat").checked = s.window_heating_enabled; el("winMins").value = s.window_open_minutes;
      el("winTemp").value = s.window_off_temp; el("winNotify").checked = s.window_notify;
      el("healthBattery").checked = s.health_battery; el("healthUnavail").checked = s.health_unavailable;
      el("healthMins").value = s.health_unavailable_minutes; el("weeklySummary").checked = s.weekly_summary;
      syncOpts();
      const n = status.windows_linked, info = el("winInfo");
      const held = status.held.map((h) => `${h.name} held low (back to ${h.restore}° when the window closes)`);
      info.textContent = n ? [`${n} window${n === 1 ? "" : "s"} linked to sensors.`, ...held].join(" ")
        : "Link window sensors in Edit mode: select a window → Link sensor.";
    } catch (e) { msg(`Automations: ${e.message}`, true); }
  }
  async function save() {
    syncOpts();
    const mins = Math.round(Number(el("winMins").value)), temp = Math.round(Number(el("winTemp").value) * 2) / 2;
    const hmins = Math.round(Number(el("healthMins").value));
    if (!(mins >= 1 && mins <= 30)) return msg("Window minutes must be 1–30.", true);
    if (!(temp >= 5 && temp <= 15)) return msg("Window temperature must be 5–15°.", true);
    if (!(hmins >= 10 && hmins <= 240)) return msg("Offline minutes must be 10–240.", true);
    try {
      await put({ window_heating_enabled: el("winHeat").checked, window_open_minutes: mins, window_off_temp: temp,
        window_notify: el("winNotify").checked, health_battery: el("healthBattery").checked,
        health_unavailable: el("healthUnavail").checked, health_unavailable_minutes: hmins,
        weekly_summary: el("weeklySummary").checked });
      msg("Saved.");
    } catch (e) { msg(`Couldn't save: ${e.message}`, true); }
  }
  ["winHeat", "winMins", "winTemp", "winNotify", "healthBattery", "healthUnavail", "healthMins", "weeklySummary"]
    .forEach((id) => el(id).addEventListener("change", save));
  el("alertsBtn").addEventListener("click", refresh);

  // ---------- "This week" sheet ----------
  const sheet = el("summarySheet");
  const node = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const day = (ms) => new Date(ms).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
  function section(title, rows) {
    const s = node("div", "sum-sec"); s.appendChild(node("h4", null, title));
    const max = Math.max(...rows.map((r) => r.bar || 0), 0);
    for (const r of rows) {
      const row = node("div", "sum-row");
      row.append(node("span", null, r.name), node("span", null, r.value));
      if (r.bar != null) { const b = node("div", "sum-bar"), i = node("i"); i.style.width = `${max ? (r.bar / max) * 100 : 0}%`; b.appendChild(i); row.appendChild(b); }
      s.appendChild(row);
    }
    return s;
  }
  function show(s) {
    const body = el("summaryBody"); body.replaceChildren();
    el("summarySub").textContent = `${s.preview ? "Preview · " : ""}${day(s.start)} – ${day(s.end)}`;
    const e = s.energy || {};
    if (e.this_kwh != null) {
      const big = node("div", "sum-big"); big.append(node("b", null, `${e.this_kwh.toFixed(1)} kWh`));
      if (e.change_pct != null) big.append(node("span", e.change_pct > 0 ? "sum-up" : "sum-down",
        `${e.change_pct > 0 ? "▲" : e.change_pct < 0 ? "▼" : ""} ${Math.abs(e.change_pct)}% vs last week (${e.last_kwh.toFixed(1)} kWh)`));
      body.appendChild(big);
    }
    if (s.plugs?.length) body.appendChild(section("Energy by plug", s.plugs.slice(0, 6).map((p) => ({ name: p.name, value: `${p.kwh.toFixed(2)} kWh`, bar: p.kwh }))));
    if (s.doors?.length) body.appendChild(section("Doors & windows opened", s.doors.slice(0, 5).map((d) => ({ name: d.name, value: `${d.opens}×`, bar: d.opens }))));
    if (s.rooms?.length) body.appendChild(section("Average temperature", s.rooms.map((r) => ({ name: r.name, value: `${r.avg_temp.toFixed(1)}°` }))));
    if (!body.children.length) body.appendChild(node("p", "hint", s.text || "Nothing to show yet."));
    el("alertSheet").hidden = true; sheet.hidden = false;
  }
  async function open(kind) {
    try { show(await api(kind === "preview" ? "/api/summary/preview" : "/api/summary/latest", { method: kind === "preview" ? "POST" : "GET" })); }
    catch (e) { msg(/no weekly summary/.test(e.message) ? "No weekly summary yet — the first arrives Sunday 19:00. Try Preview." : `Summary: ${e.message}`, true); return false; }
    return true;
  }
  el("summaryPreview").onclick = () => { msg("Building preview…"); open("preview").then((ok) => ok && msg("")); };
  el("summaryLast").onclick = () => open("latest");
  el("summaryClose").onclick = () => { sheet.hidden = true; };
  sheet.addEventListener("click", (e) => { if (e.target === sheet) sheet.hidden = true; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") sheet.hidden = true; });

  // Notification click: "/?summary" (new window) or a message from the service worker (app already open).
  const fromUrl = (u) => { try { return new URL(u, location.origin).searchParams.has("summary"); } catch { return false; } };
  if (fromUrl(location.href)) {
    history.replaceState(null, "", location.pathname);
    open("latest").then((ok) => { if (!ok) { el("alertSheet").hidden = false; refresh(); } });
  }
  navigator.serviceWorker?.addEventListener("message", (e) => { if (e.data?.type === "open" && fromUrl(e.data.url)) open("latest"); });
})();
