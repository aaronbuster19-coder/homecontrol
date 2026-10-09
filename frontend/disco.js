"use strict";
// Disco mode (backend/disco.py runs it, so it keeps going while this phone sleeps). Ways in: ⋯ → Disco… (whole home)
// and the Disco chip in a room view (that room). While one runs, a bar at the bottom shows it with a big Stop.
// State: GET /api/disco, then the "disco" SSE event (discoEvents, called from app.js startLive).
// Hooks: discoEvents(es) (app.js), discoRoomChip(room) (roomview.js facts). Uses controls.js roomLights / usable.
const DISCO = (() => {
  const ds = { s: null, skew: 0, scope: null, preset: "rainbow", speed: "normal", minutes: 30, picked: null, warn: false, busy: false, tick: 0 };
  const mk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const PRESETS = [ // the server's list replaces this once loaded
    { id: "rainbow", name: "Rainbow fade", about: "All lights glide through the rainbow together.", warning: false },
    { id: "flash", name: "Party flash", about: "Sudden jumps between bright party colours.", warning: true },
    { id: "chill", name: "Slow chill", about: "Soft colours drifting slowly, each light its own.", warning: false },
  ];
  const SPEEDS = [["slow", "Slow"], ["normal", "Normal"], ["fast", "Fast"]];
  const MINUTES = [10, 20, 30, 60, 90, 120];
  const BALL = `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="13" r="8" fill="currentColor"/><path d="M12 2v3" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M5.5 10h13M4.6 14h14.8M8 6.6c-1.3 4.2-1.3 8.6 0 12.8M16 6.6c1.3 4.2 1.3 8.6 0 12.8M12 5v16" stroke="var(--panel)" stroke-width="1.1" fill="none"/></svg>`;
  const presets = () => ds.s?.presets || PRESETS;
  const running = () => !!ds.s?.running;
  const room = (id) => st.layout.rooms.find((r) => r.id === id) || null;

  // ---------- DOM: menu item, sheet, running bar ----------
  const item = mk("button", null, "Disco…"); item.id = "discoBtn"; item.type = "button"; item.setAttribute("role", "menuitem");
  const anchor = $("roomsBtn"); if (anchor) anchor.after(item); else $("moreMenu").prepend(item);
  const sheet = mk("div", "sheet disco-sheet"); sheet.id = "discoSheet"; sheet.hidden = true;
  sheet.innerHTML = `<div class="sheet-body" role="dialog" aria-modal="true" aria-labelledby="discoTitle">
    <button class="close" id="discoClose" type="button" aria-label="Close">×</button><div id="discoContent"></div></div>`;
  const bar = mk("div", "disco-bar"); bar.id = "discoBar"; bar.hidden = true; bar.setAttribute("role", "status");
  const FACETS = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 7.5h19M1 12h22M2.5 16.5h19M12 0v24M7.2 1.2c-1.9 7-1.9 14.6 0 21.6M16.8 1.2c1.9 7 1.9 14.6 0 21.6"/></svg>`;
  bar.innerHTML = `<span class="disco-ball" aria-hidden="true">${FACETS}</span>
    <span class="disco-info"><b id="discoBarTitle">Disco</b><span id="discoBarSub" class="disco-sub"></span></span>
    <button id="discoStop" type="button" class="disco-stop">Stop</button>`;
  document.body.append(sheet, bar);

  // ---------- which lights ----------
  const colourLights = (devs) => devs.filter((d) => d?.kind === "light" && d.supports_color);
  function scopeLights() {
    const r = ds.scope && room(ds.scope);
    const all = r ? roomLights(r) : [...st.devices.values()].filter((d) => d.kind === "light" && usable(d) && !d.hidden);
    const lights = colourLights(all).sort((a, b) => a.name.localeCompare(b.name));
    return { r, lights, plain: all.length - lights.length };
  }

  // ---------- start sheet ----------
  function openSheet(scope = null) {
    if (st.editing) { setStatus("Finish editing first", true); return; }
    ds.scope = scope; ds.warn = false; ds.picked = null;
    draw(); sheet.hidden = false;
    $("discoClose").focus({ preventScroll: true });
  }
  function closeSheet() { sheet.hidden = true; ds.warn = false; }
  function radioRow(name, opts, value, onPick, cls) {
    const row = mk("div", cls); row.setAttribute("role", "radiogroup");
    for (const [v, label, extra] of opts) {
      const lab = mk("label", "disco-opt" + (v === value ? " on" : "")); const inp = mk("input"); inp.type = "radio"; inp.name = name; inp.value = v; inp.checked = v === value;
      inp.onchange = () => { onPick(v); draw(); sheet.querySelector(`input[name="${name}"][value="${v}"]`)?.focus(); };
      lab.append(inp, label); if (extra) lab.append(extra); row.append(lab);
    }
    return row;
  }
  function draw() {
    const c = $("discoContent"); c.replaceChildren();
    const { r, lights, plain } = scopeLights();
    if (ds.picked == null) ds.picked = new Set(lights.filter((d) => d.state === "on").map((d) => d.entity_id));
    for (const e of [...ds.picked]) if (!lights.some((d) => d.entity_id === e)) ds.picked.delete(e);
    const h = mk("h3", null, ds.warn ? "Photosensitivity warning" : "Disco"); h.id = "discoTitle"; c.append(h);
    if (ds.warn) return drawWarning(c);
    c.append(mk("div", "sub", (r ? r.name : "Whole home") + " · runs on the server, keeps going when this phone sleeps"));
    if (running()) {
      const now = mk("div", "disco-now"); now.append(mk("span", null, `${ds.s.preset_name} is on — starting another replaces it.`));
      const stop = mk("button", "disco-stop", "Stop"); stop.type = "button"; stop.onclick = () => stopDisco(); now.append(stop); c.append(now);
    }
    c.append(mk("div", "disco-label", "Style"));
    c.append(radioRow("discoPreset", presets().map((p) => {
      const name = mk("span", "disco-pname", p.name); if (p.warning) name.append(mk("span", "disco-warnchip", "Flashing"));
      const box = mk("span", "disco-ptext"); box.append(name, mk("span", "disco-about", p.about));
      return [p.id, box];
    }), ds.preset, (v) => { ds.preset = v; }, "disco-presets"));
    c.append(mk("div", "disco-label", "Speed"));
    c.append(radioRow("discoSpeed", SPEEDS.map(([v, l]) => [v, mk("span", null, l)]), ds.speed, (v) => { ds.speed = v; }, "disco-seg"));
    c.append(mk("div", "disco-label", "Lights"));
    const list = mk("ul", "disco-lights");
    if (!lights.length) list.append(mk("li", "hint", r ? `No colour lights in ${r.name}.` : "No colour lights found."));
    for (const d of lights) {
      const li = mk("li"), lab = mk("label"), cb = mk("input"); cb.type = "checkbox"; cb.value = d.entity_id; cb.checked = ds.picked.has(d.entity_id);
      cb.onchange = () => {
        cb.checked ? ds.picked.add(d.entity_id) : ds.picked.delete(d.entity_id);
        if (d.state !== "on") lab.querySelector(".disco-state").textContent = cb.checked ? "off · will switch on" : "off";
        $("discoGo").disabled = !ds.picked.size || ds.busy;
      };
      const dot = mk("span", "disco-dot" + (d.state === "on" ? " lit" : "")); const c0 = typeof lightColor === "function" && lightColor(d); if (c0) dot.style.background = c0;
      lab.append(cb, dot, mk("span", "name", d.name), mk("span", "disco-state", d.state === "on" ? "on" : cb.checked ? "off · will switch on" : "off"));
      li.append(lab); list.append(li);
    }
    c.append(list);
    if (plain) c.append(mk("p", "hint disco-hint", `${plain} light${plain > 1 ? "s" : ""} without colour stay${plain > 1 ? "" : "s"} as ${plain > 1 ? "they are" : "it is"}.`));
    const row = mk("label", "disco-mins"); row.append(mk("span", null, "Stop automatically after"));
    const sel = mk("select"); sel.id = "discoMinutes";
    for (const m of MINUTES.filter((m) => m <= (ds.s?.max_minutes || 120))) { const o = mk("option", null, m < 60 ? `${m} min` : `${m / 60} h`.replace(".5 h", "½ h")); o.value = m; sel.append(o); }
    sel.value = ds.minutes; sel.onchange = () => { ds.minutes = +sel.value; }; row.append(sel); c.append(row);
    c.append(mk("p", "hint disco-hint", "Lights go back to how they were when it stops. Changing one by hand, All off or Away stops it."));
    const go = mk("button", "big primary disco-go", `Start ${presets().find((p) => p.id === ds.preset)?.name.toLowerCase() || "disco"}`);
    go.type = "button"; go.id = "discoGo"; go.disabled = !ds.picked.size || ds.busy;
    go.onclick = () => { if (presets().find((p) => p.id === ds.preset)?.warning) { ds.warn = true; draw(); } else start(false); };
    c.append(go);
  }
  function drawWarning(c) {
    const box = mk("div", "disco-warning"); box.setAttribute("role", "alert");
    box.append(mk("p", null, "Party flash makes lights change colour suddenly, up to once a second."),
      mk("p", null, "Flashing lights can trigger seizures in people with photosensitive epilepsy. Don't use it if anyone in the room may be affected, and stop it if anyone feels unwell."));
    c.append(box);
    const row = mk("div", "disco-warn-acts"); const back = mk("button", null, "Back"); back.type = "button"; back.onclick = () => { ds.warn = false; draw(); };
    const ok = mk("button", "primary", "I understand, start"); ok.type = "button"; ok.id = "discoWarnOk"; ok.disabled = ds.busy; ok.onclick = () => start(true);
    row.append(back, ok); c.append(row);
  }
  async function start(warningOk) {
    if (ds.busy || !ds.picked?.size) return;
    ds.busy = true; draw();
    const body = { preset: ds.preset, speed: ds.speed, minutes: ds.minutes, entity_ids: [...ds.picked] };
    if (warningOk) body.warning_ok = true;
    try { apply(await api("/api/disco/start", { method: "POST", body: JSON.stringify(body) })); closeSheet(); setStatus(`Disco on · ${plural(ds.s.entity_ids.length, "light")}`); }
    catch (e) { setStatus(`Disco failed: ${e.message}`, true); }
    finally { ds.busy = false; if (!sheet.hidden) draw(); }
  }
  async function stopDisco() {
    const b = $("discoStop"); b.disabled = true;
    try { apply(await api("/api/disco/stop", { method: "POST" })); setStatus("Disco stopped — lights back as they were"); if (!sheet.hidden) draw(); }
    catch (e) { setStatus(`Stop failed: ${e.message}`, true); }
    finally { b.disabled = false; }
  }

  // ---------- running bar ----------
  function left() {
    const ms = ds.s.ends_at - (Date.now() + ds.skew), m = Math.max(0, Math.ceil(ms / 60000));
    return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} min`;
  }
  function paintBar() {
    const on = running(); bar.hidden = !on; document.body.classList.toggle("disco-on", on);
    item.textContent = on ? "Disco (on)…" : "Disco…";
    clearInterval(ds.tick);
    if (!on) return;
    const n = ds.s.entity_ids.length;
    $("discoBarTitle").textContent = `Disco · ${ds.s.preset_name}`;
    $("discoBarSub").textContent = `${plural(n, "light")} · stops in ${left()}`;
    bar.title = ds.s.entity_ids.map((e) => st.devices.get(e)?.name || e).join(", ");
    ds.tick = setInterval(() => { if (running()) $("discoBarSub").textContent = `${plural(n, "light")} · stops in ${left()}`; }, 15000);
  }
  function apply(s) {
    const was = running();
    ds.s = s; ds.skew = (s.now || Date.now()) - Date.now();
    if (s.max_minutes && ds.minutes > s.max_minutes) ds.minutes = s.default_minutes;
    paintBar();
    if (was && !s.running && s.last && s.last.reason !== "user") setStatus(`Disco stopped: ${s.last.text}`);
    if (st.room && typeof render === "function") render(); // the room view's chip
  }
  async function load() { try { apply(await api("/api/disco")); } catch {} }

  // ---------- room view chip ----------
  function roomChip(r) {
    if (!colourLights(roomLights(r)).length) return null;
    const on = running() && ds.s.entity_ids.some((e) => { const p = st.layout.placements.find((q) => q.entity_id === e); return p && inRoom(r, p); });
    const b = mk("button", "rf disco-chip" + (on ? " on" : "")); b.type = "button"; b.dataset.fact = "disco";
    b.innerHTML = BALL; b.append(on ? "Disco on" : "Disco");
    b.title = on ? "Disco is on — tap to change or stop" : `Disco in ${r.name}`;
    b.onclick = () => openSheet(r.id);
    return b;
  }

  // ---------- input ----------
  item.onclick = () => openSheet(null);
  $("discoClose").onclick = closeSheet;
  $("discoStop").onclick = () => stopDisco();
  sheet.addEventListener("click", (e) => { if (e.target === sheet) closeSheet(); });
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !sheet.hidden) { e.stopImmediatePropagation(); if (ds.warn) { ds.warn = false; draw(); } else closeSheet(); }
  }, true);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
  document.addEventListener("DOMContentLoaded", load);

  return { open: openSheet, close: closeSheet, apply, load, roomChip, get state() { return ds.s; } };
})();
function discoEvents(es) {
  es.addEventListener("disco", (e) => { try { DISCO.apply(JSON.parse(e.data)); } catch {} });
  es.addEventListener("snapshot", () => DISCO.load()); // reconnected: catch up on what happened meanwhile
}
function discoRoomChip(r) { return DISCO.roomChip(r); }
