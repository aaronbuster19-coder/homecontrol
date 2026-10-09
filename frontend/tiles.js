"use strict";
// Quick tiles: a compact grid of pinned favourite devices — /?view=tiles (the app's home-screen shortcut) or
// ⋯ → Favourites. Pins live on the server (GET/PUT/POST/DELETE /api/tiles), shared by all devices; pin or unpin from
// any device sheet (☆ Add to favourites) or the view's Edit → + Add.
// A tap does what a tap on the plan does (tapDevice in app.js): lights and plugs toggle, everything else — and a
// fridge, home server or keep-on plug — opens its sheet, where switching off asks first. Long-press opens the sheet.
// Uses app.js (st, api, $, render hooks, deviceColor/deviceValue, tapDevice, openSheet, iconKind, icon, setStatus).
const TILES = (() => {
  const KINDS = ["light", "plug", "valve", "dehumidifier", "media", "sensor"];
  const MAX = 24, LP_MS = 450, LP_MOVE = 8;
  const t = { pins: [], loaded: false, on: false, edit: false, lpFired: 0 };
  const mkEl = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const pinnable = (d) => !!d && KINDS.includes(d.kind);

  // ---------- DOM ----------
  const view = mkEl("section", "tiles-view"); view.id = "tilesView"; view.hidden = true;
  view.innerHTML = `<div class="tiles-bar">
      <h2>Favourites</h2><span class="tiles-status" id="tilesStatus"></span>
      <button type="button" id="tilesEdit">Edit</button>
      <button type="button" id="tilesExit" title="Back to the floor plan">Plan</button>
    </div>
    <div class="tiles-grid" id="tilesGrid" role="list"></div>
    <div class="tiles-empty" id="tilesEmpty" hidden>
      <p>No favourites yet.</p>
      <p class="hint">Pin the lights, plugs and radiators you use most: tap <b>+ Add</b>, or open any device's sheet and
        tap <b>☆ Add to favourites</b>.</p>
      <button type="button" class="primary" id="tilesAddFirst">+ Add</button>
    </div>`;
  const pick = mkEl("div", "sheet"); pick.id = "tilesPick"; pick.hidden = true;
  pick.innerHTML = `<div class="sheet-body"><button class="close" id="tilesPickClose" aria-label="Close">×</button>
    <h3>Favourites</h3><div class="sub">Tap to add or remove. Shared by all your devices.</div>
    <ul class="tiles-pick" id="tilesPickList"></ul></div>`;
  document.querySelector("main").before(view);
  document.body.append(pick);
  const grid = $("tilesGrid");

  // ⋯ → Favourites (after Wall mode)
  const item = mkEl("button", null, "Favourites"); item.id = "tilesBtn"; item.setAttribute("role", "menuitem");
  const wallBtn = $("wallBtn"); if (wallBtn) wallBtn.after(item); else $("moreMenu").prepend(item);
  item.onclick = () => enter();

  // ---------- pins ----------
  async function load() {
    try { t.pins = (await api("/api/tiles")).pins || []; t.loaded = true; } catch {}
    paint();
  }
  async function save(req) {
    try { t.pins = (await req).pins || []; } catch (e) { setStatus(`Favourites: ${e.message}`, true); }
    paint(); if (st.sheetFor) renderSheet();
    if (!pick.hidden) paintPick();
  }
  const pin = (eid) => save(api(`/api/tiles/${encodeURIComponent(eid)}`, { method: "POST" }));
  const unpin = (eid) => save(api(`/api/tiles/${encodeURIComponent(eid)}`, { method: "DELETE" }));
  const order = (pins) => { t.pins = pins; paint(); return save(api("/api/tiles", { method: "PUT", body: JSON.stringify({ pins }) })); };

  // ---------- tiles ----------
  function tileClass(d) {
    if (!d || d.state === "unavailable" || d.state === "unknown") return "off na";
    if (d.kind === "valve") return d.state === "off" ? "off" : "on heat";
    if (d.kind === "sensor") return d.state === "on" ? "on alert" : "off";
    if (d.kind === "media") return ["off", "standby"].includes(d.state) ? "off" : "on";
    return d.state === "on" ? "on" : "off";
  }
  function tile(eid, i, n) {
    const d = st.devices.get(eid);
    const b = mkEl("div", `tile ${tileClass(d)}`); b.dataset.tile = eid; b.setAttribute("role", "listitem");
    const main = mkEl("button", "tile-main"); main.type = "button";
    const af = d && typeof applianceFor === "function" ? applianceFor(eid) : null; // a linked appliance's drawing
    const ic = mkEl("span", "tile-ic");
    if (af) { ic.classList.add("appl"); ic.append(applianceIcon(af, d)); }
    else {
      ic.append(icon(iconKind(d, d?.kind || "light"), (d && iconFill(d)) || "var(--state-text)"));
      ic.style.background = deviceColor(d); // as the plan's marker
    }
    const name = mkEl("span", "tile-name", d ? (af && typeof applName === "function" ? applName(af) : d.name) : eid);
    const val = mkEl("span", "tile-val", !d ? "not in Home Assistant" : af ? applianceText(af, d) : deviceValue(d));
    main.append(ic, name, val);
    main.setAttribute("aria-label", `${name.textContent} — ${val.textContent}`);
    b.append(main);
    if (t.edit) {
      const ctl = mkEl("span", "tile-ctl");
      const btn = (txt, label, fn, off) => { const x = mkEl("button", null, txt); x.type = "button"; x.setAttribute("aria-label", label); x.disabled = !!off; x.onclick = (e) => { e.stopPropagation(); fn(); }; return x; };
      const move = (k) => { const p = [...t.pins]; [p[i], p[i + k]] = [p[i + k], p[i]]; order(p); };
      ctl.append(btn("‹", "Move earlier", () => move(-1), i === 0), btn("›", "Move later", () => move(1), i === n - 1),
        btn("×", `Remove ${name.textContent}`, () => unpin(eid)));
      b.append(ctl);
    }
    return b;
  }
  function paint() {
    if (!t.on) return;
    const pins = t.pins.filter((eid) => !st.devices.get(eid)?.hidden);
    grid.replaceChildren(...pins.map((eid, i) => tile(eid, i, pins.length)));
    if (t.edit && t.pins.length < MAX) {
      const add = mkEl("button", "tile tile-add", "+ Add"); add.type = "button"; add.id = "tilesAdd"; add.onclick = openPick;
      grid.append(add);
    }
    view.classList.toggle("editing", t.edit);
    $("tilesEmpty").hidden = !t.loaded || pins.length > 0 || t.edit;
    $("tilesEdit").textContent = t.edit ? "Done" : "Edit";
    $("tilesEdit").classList.toggle("primary", t.edit);
    mirrorStatus();
  }
  // The header (hidden here) keeps the status line: power, live, errors. Mirror it.
  function mirrorStatus() {
    $("tilesStatus").textContent = $("status").textContent;
    $("tilesStatus").classList.toggle("err", $("status").classList.contains("err"));
  }
  new MutationObserver(() => { if (t.on) mirrorStatus(); }).observe($("status"), { childList: true, characterData: true, subtree: true, attributes: true });

  // tap / long-press (the plan's long-press in controls.js only covers markers and list rows)
  let lp = null;
  const lpCancel = () => { if (lp) clearTimeout(lp.timer); lp = null; };
  grid.addEventListener("pointerdown", (e) => {
    lpCancel();
    const b = e.target.closest(".tile-main"); if (!b || t.edit || e.button > 0) return;
    const eid = b.parentElement.dataset.tile;
    lp = { x: e.clientX, y: e.clientY, id: e.pointerId };
    lp.timer = setTimeout(() => { lp = null; t.lpFired = Date.now(); navigator.vibrate?.(12); if (st.devices.has(eid)) openSheet(eid); }, LP_MS);
  });
  grid.addEventListener("pointermove", (e) => { if (lp && e.pointerId === lp.id && Math.hypot(e.clientX - lp.x, e.clientY - lp.y) > LP_MOVE) lpCancel(); });
  for (const ev of ["pointerup", "pointercancel", "pointerleave"]) grid.addEventListener(ev, lpCancel);
  grid.addEventListener("contextmenu", (e) => { if (e.target.closest(".tile-main")) e.preventDefault(); });
  // The click that ends a long-press (on touch it lands on the sheet's backdrop) must not close the sheet or toggle.
  document.addEventListener("click", (e) => {
    if (t.lpFired && Date.now() - t.lpFired < 1500) { t.lpFired = 0; e.stopPropagation(); e.preventDefault(); }
  }, true);
  grid.addEventListener("click", (e) => {
    const b = e.target.closest(".tile-main"); if (!b) return;
    const eid = b.parentElement.dataset.tile;
    if (t.edit) return;
    if (!st.devices.has(eid)) { setStatus("That device isn't in Home Assistant right now", true); return; }
    tapDevice(eid); // same rules (and protections) as a tap on the plan
  });

  // ---------- picker ----------
  function openPick() { pick.hidden = false; paintPick(); }
  function paintPick() {
    const list = $("tilesPickList"); list.replaceChildren();
    const pinned = new Set(t.pins);
    for (const kind of KIND_ORDER) {
      const devs = [...st.devices.values()].filter((d) => d.kind === kind && !d.hidden && pinnable(d));
      if (!devs.length) continue;
      list.append(mkEl("li", "group", KIND_LABEL[kind]));
      for (const d of devs) {
        const li = mkEl("li"); const b = mkEl("button", pinned.has(d.entity_id) ? "pinned" : ""); b.type = "button";
        b.dataset.pick = d.entity_id; b.setAttribute("aria-pressed", pinned.has(d.entity_id) ? "true" : "false");
        b.append(icon(iconKind(d, kind), deviceColor(d)), mkEl("span", "name", d.name), mkEl("span", "star", pinned.has(d.entity_id) ? "★" : "☆"));
        b.disabled = !pinned.has(d.entity_id) && t.pins.length >= MAX;
        b.onclick = () => (pinned.has(d.entity_id) ? unpin : pin)(d.entity_id);
        li.append(b); list.append(li);
      }
    }
  }
  const closePick = () => { pick.hidden = true; };
  $("tilesPickClose").onclick = closePick;
  pick.addEventListener("click", (e) => { if (e.target === pick) closePick(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !pick.hidden) closePick(); });

  // ---------- device sheet: ☆ Add to favourites ----------
  function sheetRow(d, c) {
    if (!pinnable(d) || !t.loaded) return;
    const on = t.pins.includes(d.entity_id);
    const b = mkEl("button", "tiles-pin" + (on ? " pinned" : ""), on ? "★ In favourites — remove" : "☆ Add to favourites");
    b.type = "button"; b.id = "tilesPin"; b.setAttribute("aria-pressed", on ? "true" : "false");
    b.disabled = !on && t.pins.length >= MAX;
    b.onclick = () => (on ? unpin : pin)(d.entity_id);
    c.append(b);
  }

  // ---------- enter / exit ----------
  function setUrl(on) {
    const q = new URLSearchParams(location.search);
    if (on) q.set("view", "tiles"); else q.delete("view");
    const s = q.toString();
    history.replaceState(history.state, "", location.pathname + (s ? `?${s}` : "") + location.hash);
  }
  function enter() {
    if (st.editing) setEditing(false);
    if (typeof WALL !== "undefined" && WALL.on) WALL.exit();
    t.on = true; t.edit = false;
    document.body.classList.add("tiles"); view.hidden = false;
    if (new URLSearchParams(location.search).get("view") !== "tiles") setUrl(true);
    closeSheet(); paint();
    if (!t.loaded) load();
  }
  function exit() {
    if (!t.on) return;
    t.on = false; t.edit = false;
    document.body.classList.remove("tiles"); view.hidden = true; closePick();
    setUrl(false); render();
  }
  $("tilesExit").onclick = exit;
  $("tilesEdit").onclick = () => { t.edit = !t.edit; if (!t.edit) closePick(); paint(); };
  $("tilesAddFirst").onclick = () => { t.edit = true; paint(); openPick(); };
  // Pins changed on another device: pick them up when this one comes back.
  document.addEventListener("visibilitychange", () => { if (!document.hidden && t.loaded) load(); });

  load();
  if (new URLSearchParams(location.search).get("view") === "tiles") enter();
  return { enter, exit, paint, sheetRow, get on() { return t.on; }, get pins() { return t.pins; } };
})();
// Hooks called from app.js (render, renderSheet).
function renderTiles() { TILES.paint(); }
function tilesSheetRow(d, c) { TILES.sheetRow(d, c); }
