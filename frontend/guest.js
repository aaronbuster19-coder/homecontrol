"use strict";
// The guest page behind a guest link / QR code (backend/guest_links.py). /guest.html#<token>: the token is swapped for
// an HttpOnly cookie (POST /api/guest/redeem) and dropped from the address bar at once. The page then shows only the
// lights (and plugs) the link covers, live (GET /api/guest/session/events), and switches / dims them. When the link
// runs out or is revoked the server answers 401 / sends `end`, and the page says so.
(() => {
  const $ = (id) => document.getElementById(id);
  const S = { devices: new Map(), order: [], expires: 0, es: null, gone: false, timer: 0, busy: new Set() };
  const BASE = "/api/guest/session";
  const BULB = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.6 10.8c.7.6 1.1 1.3 1.1 2.2h5c0-.9.4-1.6 1.1-2.2A6 6 0 0 0 12 3z" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  const PLUG = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0zM12 17v4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  const SWATCHES = [["Warm white", { color_temp_kelvin: 2700 }, "#ffc779", "temp"], ["Daylight", { color_temp_kelvin: 5000 }, "#eef3ff", "temp"],
    ["Red", { hs_color: [0, 85] }, "#ff4b4b", "color"], ["Orange", { hs_color: [30, 90] }, "#ff9a2e", "color"],
    ["Green", { hs_color: [125, 70] }, "#44d36a", "color"], ["Blue", { hs_color: [220, 80] }, "#4f7dff", "color"],
    ["Purple", { hs_color: [275, 80] }, "#a35bff", "color"]];
  const ux = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const fmtUntil = (s) => new Date(s * 1000).toLocaleString("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

  function status(msg, err = false) { const s = $("guestStatus"); s.textContent = msg; s.classList.toggle("err", err); s.hidden = !msg; }

  async function call(path, opts = {}) {
    let r;
    try {
      r = await fetch(path, { credentials: "same-origin", cache: "no-store", ...opts, headers: { "Content-Type": "application/json" } });
    } catch { throw Object.assign(new Error("No connection — check your Wi-Fi or data"), { offline: true }); }
    if (r.status === 401) { gone(); throw new Error("gone"); }
    let body = null;
    try { body = await r.json(); } catch {}
    if (!r.ok) throw new Error((body && typeof body.detail === "string" && body.detail) || r.statusText || "Something went wrong");
    return body;
  }

  function gone() {
    if (S.gone) return;
    S.gone = true;
    if (S.es) S.es.close();
    clearTimeout(S.timer);
    $("guestDevices").replaceChildren();
    $("guestDevices").hidden = true;
    $("guestSub").textContent = "";
    status("");
    $("guestGone").hidden = false;
  }

  // ---------- drawing ----------
  const pct = (d) => (d.brightness == null ? null : Math.max(1, Math.round(d.brightness / 2.55)));
  const isOn = (d) => d.state === "on";
  const unavailable = (d) => d.state === "unavailable" || d.state === "unknown";

  function stateText(d) {
    if (unavailable(d)) return "Not responding";
    if (!isOn(d)) return "Off";
    return d.kind === "light" && d.supports_brightness && pct(d) != null ? `On · ${pct(d)}%` : "On";
  }

  function card(d) {
    const li = ux("li", "gd"); li.dataset.dev = d.entity_id;
    li.classList.toggle("on", isOn(d)); li.classList.toggle("unavail", unavailable(d));
    const btn = ux("button", "gd-toggle"); btn.type = "button";
    btn.setAttribute("aria-pressed", isOn(d) ? "true" : "false");
    btn.disabled = unavailable(d) || S.busy.has(d.entity_id);
    const icon = ux("span", "gd-icon"); icon.innerHTML = d.kind === "plug" ? PLUG : BULB;
    if (d.kind === "light" && isOn(d) && d.color_mode !== "color_temp" && Array.isArray(d.rgb_color)) {
      icon.style.setProperty("--gd-colour", `rgb(${d.rgb_color.join(",")})`);
      li.classList.add("coloured");
    }
    const txt = ux("span", "gd-text");
    txt.append(ux("span", "gd-name", d.name), ux("span", "gd-state", stateText(d)));
    btn.append(icon, txt);
    btn.setAttribute("aria-label", `${d.name}: ${stateText(d)}. Tap to switch ${isOn(d) ? "off" : "on"}`);
    btn.onclick = () => toggle(d);
    li.appendChild(btn);
    if (d.kind === "light" && isOn(d) && d.supports_brightness) {
      const row = ux("label", "gd-bright");
      row.append(ux("span", "gd-label", "Brightness"));
      const r = ux("input"); r.type = "range"; r.min = "1"; r.max = "100"; r.step = "1"; r.value = String(pct(d) ?? 100);
      r.setAttribute("aria-label", `Brightness of ${d.name}`);
      r.onchange = () => set(d, { brightness_pct: Number(r.value) });
      row.appendChild(r);
      li.appendChild(row);
    }
    const sw = SWATCHES.filter(([, , , need]) => (need === "temp" ? d.supports_color_temp : d.supports_color));
    if (d.kind === "light" && isOn(d) && sw.length) {
      const row = ux("div", "gd-swatches"); row.setAttribute("role", "group"); row.setAttribute("aria-label", `Colour of ${d.name}`);
      for (const [name, data, css] of sw) {
        const b = ux("button", "gd-swatch"); b.type = "button"; b.title = name; b.setAttribute("aria-label", name);
        b.style.setProperty("--sw", css);
        b.onclick = () => set(d, data);
        row.appendChild(b);
      }
      li.appendChild(row);
    }
    return li;
  }

  function draw() {
    const ul = $("guestDevices"), focus = document.activeElement?.closest?.(".gd")?.dataset.dev;
    ul.replaceChildren(...S.order.map((e) => card(S.devices.get(e))));
    if (focus) ul.querySelector(`.gd[data-dev="${CSS.escape(focus)}"] .gd-toggle`)?.focus({ preventScroll: true });
    if (!S.order.length) status("Nothing to switch here right now.");
  }

  function show(s) {
    S.devices = new Map(s.devices.map((d) => [d.entity_id, d]));
    S.order = s.devices.map((d) => d.entity_id);
    S.expires = s.expires;
    $("guestTitle").textContent = s.room ? `${s.room} lights` : "Your lights";
    $("guestSub").textContent = `For ${s.label} · until ${fmtUntil(s.expires)}`;
    document.title = `${$("guestTitle").textContent} · homecontrol`;
    status("");
    draw();
    clearTimeout(S.timer);  // at the expiry time ask the server, which decides
    S.timer = setTimeout(recheck, Math.min(2 ** 31 - 1, Math.max(1000, s.expires * 1000 - Date.now() + 500)));
  }

  function update(d) {
    if (!S.devices.has(d.entity_id)) return;
    S.devices.set(d.entity_id, d);
    draw();
  }

  // ---------- actions ----------
  async function toggle(d) {
    if (S.busy.has(d.entity_id)) return;
    S.busy.add(d.entity_id);
    const before = S.devices.get(d.entity_id);
    S.devices.set(d.entity_id, { ...before, state: isOn(before) ? "off" : "on" });  // the live stream confirms it
    draw();
    try {
      await call(`${BASE}/devices/${encodeURIComponent(d.entity_id)}/toggle`, { method: "POST" });
    } catch (e) {
      if (S.gone) return;
      S.devices.set(d.entity_id, before);
      status(e.message, true);
    } finally {
      S.busy.delete(d.entity_id);
      if (!S.gone) draw();
    }
  }

  async function set(d, data) {
    try {
      await call(`${BASE}/devices/${encodeURIComponent(d.entity_id)}/light`, { method: "POST", body: JSON.stringify(data) });
      status("");
    } catch (e) { if (!S.gone) status(e.message, true); }
  }

  // ---------- live ----------
  function connect() {
    if (S.gone || !window.EventSource) return;
    const es = new EventSource(`${BASE}/events`);
    S.es = es;
    es.addEventListener("snapshot", (e) => show(JSON.parse(e.data)));
    es.addEventListener("device", (e) => update(JSON.parse(e.data)));
    es.addEventListener("end", () => gone());
    // A refused reconnect (401) closes it for good: ask the server what happened.
    es.onerror = () => { if (es.readyState === EventSource.CLOSED) setTimeout(recheck, 2000); };
  }

  async function recheck() {
    if (S.gone) return;
    try {
      show(await call(BASE));
      if (!S.es || S.es.readyState === EventSource.CLOSED) connect();
    } catch (e) { if (!S.gone) status(e.message, true); }
  }

  async function start() {
    const token = location.hash.slice(1);
    if (token) {
      try {
        await call("/api/guest/redeem", { method: "POST", body: JSON.stringify({ token }) });
      } catch (e) {
        if (!e.offline) history.replaceState(null, "", location.pathname + location.search);  // offline: reload retries
        if (!S.gone) status(e.message, true);
        return;
      }
      history.replaceState(null, "", location.pathname + location.search);  // no token left in the address bar or history
    }
    try {
      show(await call(BASE));
      connect();
    } catch (e) { if (!S.gone) status(e.message, true); }
  }

  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && !S.gone && S.order.length) recheck(); });
  start();
})();
