"use strict";
// Room view: the live plan zoomed to one room, the rest of the home dimmed, with that room's devices and facts.
// Ways in: the ⤢ button in a room's corner, double-tap or long-press on a room's floor, ⋯ → Rooms…, or /#room=<id>.
// Back (header strip, wall bar, browser / Android back) or Escape returns; swipe or ←/→ go to the previous / next room.
// Hooks from app.js: roomViewBox() (computeViewBox), roomSideFilter() (renderSide), renderRoomView() (end of render).
// Uses floorplan.js (roomPoly, inRoom, walls, onSeg), controls.js (roomLights, toggleRoomLights), modes.js (roomTemp),
// dehumidifier.js (roomHumidity) and wall.js (WALL). Loaded before wall.js so its Escape runs first.
const ROOMVIEW = (() => {
  const PAD = 0.4, ANIM_MS = 320, LP_MS = 450, LP_MOVE = 10, DTAP_MS = 400, DTAP_PX = 30, SWIPE_PX = 50;
  const rv = { anim: null, cur: null, raf: 0, done: 0, pending: null, lastTap: null, lp: null, lpFired: 0, touch: null };
  const room = (id = st.room) => (id && st.layout.rooms.find((r) => r.id === id)) || null;
  const base = () => location.pathname + location.search;
  const wallOn = () => typeof WALL !== "undefined" && WALL.on;
  const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const mk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const ICON = (id) => `<svg viewBox="0 0 24 24" aria-hidden="true" fill="currentColor"><use href="#ic-${id}"/></svg>`;
  const EXPAND = "M14 4h6v6M20 4l-6.5 6.5M10 20H4v-6M4 20l6.5-6.5";

  // ---------- DOM: header strip, facts, rooms sheet ----------
  const bar = mk("div", "room-bar"); bar.id = "roomBar"; bar.hidden = true;
  bar.innerHTML = `<button id="roomBack" type="button" class="rb-back" aria-label="Back to the whole home">‹ <span>Home</span></button>
    <div class="rb-title"><h2 id="roomName"></h2><div id="roomClimate" class="rb-climate"></div></div>
    <span class="rb-nav"><button id="roomPrev" type="button" aria-label="Previous room" title="Previous room (←)">‹</button><button id="roomNext" type="button" aria-label="Next room" title="Next room (→)">›</button></span>
    <button id="roomLights" type="button" class="rb-lights" aria-pressed="false"><span class="sw"></span><span>Lights</span></button>`;
  $("planWrap").prepend(bar);
  const facts = mk("div", "room-facts"); facts.id = "roomFacts"; facts.hidden = true;
  $("sideHint").after(facts);
  const sheet = mk("div", "sheet"); sheet.id = "roomsSheet"; sheet.hidden = true;
  sheet.innerHTML = `<div class="sheet-body"><button class="close" id="roomsClose" aria-label="Close">×</button>
    <h3>Rooms</h3><div class="sub">Open a room to see it up close.</div><ul id="roomsList" class="rooms-list"></ul></div>`;
  document.body.append(sheet);

  // ---------- geometry ----------
  function vbFor(r) {
    const p = roomPoly(r), xs = p.map((q) => q[0]), ys = p.map((q) => q[1]);
    const x0 = Math.min(...xs), y0 = Math.min(...ys), x1 = Math.max(...xs), y1 = Math.max(...ys);
    return [x0 - PAD, y0 - PAD, x1 - x0 + 2 * PAD, y1 - y0 + 2 * PAD].map((v) => +v.toFixed(3));
  }
  const mppOf = (vb) => Math.max(vb[2] / (svg.clientWidth || 800), vb[3] / (svg.clientHeight || 600));
  // Markers and labels at a comfortable size on screen, whatever the zoom.
  function scaleFor(vb) {
    svg.style.setProperty("--rv-ar", `${vb[2]} / ${vb[3]}`); // phones: the plan takes the room's shape, the list moves up
    const m = mppOf(vb), wall = wallOn();
    st.markerScale = st.labelScale = +clamp((wall ? 30 : 22) * m / 0.26, 0.5, wall ? 2.4 : 1.6).toFixed(3);
    svg.style.setProperty("--rv-mk", st.markerScale);
    svg.style.setProperty("--rv-tk", +clamp((wall ? 24 : 17) * m / 0.32, 0.3, 1.4).toFixed(3));
  }
  function resetScale() { st.markerScale = wallOn() ? 1.5 : 1; st.labelScale = null; for (const p of ["--rv-mk", "--rv-tk", "--rv-ar"]) svg.style.removeProperty(p); }
  // app.js computeViewBox(): the animated frame, the room's box, or null for the whole home.
  function roomViewBox() {
    if (rv.anim) return rv.cur;
    const r = !st.editing && room();
    if (!r) return null;
    const vb = vbFor(r); scaleFor(vb); return vb;
  }

  // ---------- animation ----------
  function finish() {
    if (!rv.anim) return;
    rv.anim = null; rv.cur = null; cancelAnimationFrame(rv.raf); clearTimeout(rv.done); render();
  }
  function animateTo(to, animate) {
    const from = rv.cur || st.viewBox;
    cancelAnimationFrame(rv.raf); clearTimeout(rv.done); rv.anim = null; rv.cur = null;
    if (!animate || reduced() || !from) { render(); return; }
    rv.anim = { from: [...from], to, t0: performance.now() }; rv.cur = [...from];
    const step = (now) => {
      const a = rv.anim; if (!a) return;
      const t = Math.min(1, (now - a.t0) / ANIM_MS), e = 1 - Math.pow(1 - t, 3);
      if (t >= 1) { finish(); return; }
      rv.cur = a.from.map((v, i) => v + (a.to[i] - v) * e);
      st.viewBox = rv.cur; svg.setAttribute("viewBox", rv.cur.join(" "));
      rv.raf = requestAnimationFrame(step);
    };
    rv.raf = requestAnimationFrame(step);
    rv.done = setTimeout(finish, ANIM_MS + 250); // rAF doesn't run in a background tab
    render();
  }

  // ---------- open / close / step ----------
  function open(id, { push = true, animate = true } = {}) {
    const r = room(id); if (!r || st.editing) return false;
    const was = st.room;
    st.room = r.id;
    if (push) {
      const url = `${base()}#room=${encodeURIComponent(r.id)}`;
      if (was) history.replaceState({ ...(history.state || {}), hcRoom: r.id }, "", url);
      else history.pushState({ hcRoom: r.id, hcBack: true }, "", url);
    }
    document.body.classList.add("room-view"); bar.hidden = false; facts.hidden = false;
    closeRooms(); $("moreMenu").hidden = true;
    if (typeof resetPlanZoom === "function") resetPlanZoom(); // zoom.js: start from the whole room
    const vb = vbFor(r); scaleFor(vb); // after the strip is shown: the plan is a little smaller now
    animateTo(vb, animate);
    return true;
  }
  function close({ fromPop = false, animate = true } = {}) {
    if (!st.room) return;
    st.room = null;
    document.body.classList.remove("room-view"); bar.hidden = true; facts.hidden = true;
    if ($("wallRoomBack")) $("wallRoomBack").hidden = true;
    resetScale();
    if (typeof resetPlanZoom === "function") resetPlanZoom(); // zoom.js: back to the whole home
    if (!fromPop) {
      if (history.state?.hcBack) history.back(); // the popstate that follows finds nothing left to do
      else history.replaceState(null, "", base());
    }
    animateTo(computeViewBoxWhole(), animate);
  }
  function computeViewBoxWhole() { const a = rv.anim; rv.anim = null; const vb = computeViewBox(); rv.anim = a; return vb; }
  function step(dir) {
    const rs = st.layout.rooms, i = rs.findIndex((r) => r.id === st.room);
    if (i < 0 || rs.length < 2) return;
    open(rs[(i + dir + rs.length) % rs.length].id);
  }
  // Browser back / forward, and a #room= typed into the address bar (hashchange only: no popstate).
  const fromUrl = () => {
    const id = roomFromHash();
    if (id && room(id) && !st.editing) {
      if (!history.state) history.replaceState({ hcRoom: id, hcBack: true }, "", location.href); // a new entry on top of this page
      if (id !== st.room) open(id, { push: false });
    }
    else if (st.room) close({ fromPop: true });
    else if (id) history.replaceState(null, "", base());
  };
  window.addEventListener("popstate", fromUrl);
  window.addEventListener("hashchange", fromUrl);
  function roomFromHash() { const m = /(?:^#|&)room=([^&]*)/.exec(location.hash); return m ? decodeURIComponent(m[1]) : null; }
  rv.pending = roomFromHash();

  // ---------- facts ----------
  function roomDevices(r) {
    return st.layout.placements.filter((p) => inRoom(r, p)).map((p) => st.devices.get(p.entity_id)).filter((d) => d && !d.hidden);
  }
  // Door / window sensors placed in the room or linked to an opening on its walls.
  function roomOpenings(r) {
    const ids = new Set(roomDevices(r).filter((d) => d.kind === "sensor").map((d) => d.entity_id));
    const ws = walls(r);
    for (const o of st.layout.openings || []) {
      if (!o.entity_id) continue;
      const m = o.orient === "h" ? [o.x + o.len / 2, o.y] : [o.x, o.y + o.len / 2];
      if (ws.some((w) => w.orient === o.orient && onSeg(m, w))) ids.add(o.entity_id);
    }
    return [...ids].map((e) => st.devices.get(e)).filter((d) => d && !d.hidden);
  }
  function roomFacts(r) {
    const devs = roomDevices(r);
    const plugs = devs.filter((d) => d.kind === "plug" && d.power != null);
    const lights = roomLights(r);
    const sensors = roomOpenings(r);
    return {
      temp: typeof roomTemp === "function" ? roomTemp(r) : null,
      hum: typeof roomHumidity === "function" ? roomHumidity(r) : null,
      power: plugs.length ? plugs.reduce((a, d) => a + d.power, 0) : null,
      lights, lightsOn: lights.filter((d) => d.state === "on").length,
      sensors, open: sensors.filter((d) => d.state === "on"),
    };
  }
  const chip = (cls, icon, text, fact) => { const c = mk("span", `rf ${cls}`); c.dataset.fact = fact; c.innerHTML = icon; c.append(text); return c; };
  function paint(r) {
    const f = roomFacts(r), wall = wallOn();
    $("roomName").textContent = r.name;
    const cl = $("roomClimate"); cl.replaceChildren();
    if (f.temp != null) cl.append(chip("temp", ICON("valve"), `${(Math.round(f.temp * 10) / 10).toFixed(1)}°`, "temp"));
    if (f.hum != null) cl.append(chip("hum", ICON("dehumidifier"), `${Math.round(f.hum)} %`, "hum"));
    const lb = $("roomLights"), on = f.lightsOn > 0;
    lb.disabled = !f.lights.length; lb.classList.toggle("on", on); lb.setAttribute("aria-pressed", on ? "true" : "false");
    lb.title = f.lights.length ? `Turn ${on ? "off" : "on"} ${f.lights.length} light${f.lights.length > 1 ? "s" : ""}` : "No lights in this room";
    lb.lastChild.textContent = f.lights.length ? "Lights" : "No lights";
    $("roomPrev").disabled = $("roomNext").disabled = st.layout.rooms.length < 2;
    facts.replaceChildren();
    if (f.lights.length) facts.append(chip(on ? "lit" : "", ICON("light"), on ? `${f.lightsOn} of ${f.lights.length} light${f.lights.length > 1 ? "s" : ""} on` : "Lights off", "lights"));
    if (f.power != null) facts.append(chip("power", `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M13 2 4 14h7l-1 8 9-12h-7z" fill="currentColor"/></svg>`, fmtW(f.power), "power"));
    if (f.open.length) for (const d of f.open) facts.append(chip("open", ICON("sensor"), `${d.name} open`, "open"));
    else if (f.sensors.length) facts.append(chip("closed", ICON("sensor"), f.sensors.length > 1 ? "Doors & windows closed" : `${f.sensors[0].name} closed`, "open"));
    if (typeof tvRoomFacts === "function") facts.append(...tvRoomFacts(r)); // "TV · Netflix"
    const disco = typeof discoRoomChip === "function" && discoRoomChip(r); if (disco) facts.append(disco); // disco.js
    if (f.temp != null) facts.append(chip("temp", ICON("valve"), `${(Math.round(f.temp * 10) / 10).toFixed(1)}°`, "temp"));
    if (f.hum != null) facts.append(chip("hum", ICON("dehumidifier"), `${Math.round(f.hum)} %`, "hum"));
    facts.hidden = !facts.children.length;
    const wb = $("wallRoomBack"); if (wb) wb.hidden = !wall;
  }

  // ---------- app.js hooks ----------
  function roomSideFilter(devs) {
    const r = !st.editing && room(); if (!r) return devs;
    const ids = new Set(st.layout.placements.filter((p) => inRoom(r, p)).map((p) => p.entity_id));
    if (typeof tvRoomIds === "function") tvRoomIds(r).forEach((e) => ids.add(e)); // a TV on its furniture (tv.js)
    const out = devs.filter((d) => ids.has(d.entity_id));
    $("sideTitle").textContent = `In ${r.name}`;
    $("sideHint").textContent = out.length ? "" : "No devices placed in this room.";
    return out;
  }
  function layer(id) { let g = $(id); if (!g) g = el("g", { id }); return g; }
  function renderRoomView() {
    if (rv.pending && st.layout.rooms.length) { // deep link: home underneath, so Back (and Android back) lands there
      const id = rv.pending; rv.pending = null;
      queueMicrotask(() => {
        history.replaceState(null, "", base());
        if (room(id) && !st.editing) { history.pushState({ hcRoom: id, hcBack: true }, "", `${base()}#room=${encodeURIComponent(id)}`); open(id, { push: false, animate: false }); }
      });
    }
    const r = room();
    if (st.room && (!r || st.editing)) { queueMicrotask(() => close({ animate: false })); }
    // Below the markers, above everything else (furniture included): outside markers are dimmed instead.
    const openers = layer("roomOpeners"), mask = layer("roomMask");
    $("markers").before(openers, mask);
    openers.replaceChildren(); mask.replaceChildren();
    if (!st.editing && !st.room) renderOpeners(openers);
    if (r && !st.editing) {
      const pts = roomPoly(r).map((p) => p.join(",")).join(" ");
      el("path", { class: "rv-dim", "fill-rule": "evenodd", d: `M-1000,-1000H1000V1000H-1000Z M${roomPoly(r).map((p) => p.join(",")).join(" L")}Z` }, mask);
      el("polygon", { class: "rv-outline", points: pts }, mask);
      for (const m of $("markers").querySelectorAll(".marker")) {
        const p = st.layout.placements.find((q) => q.entity_id === m.dataset.dev);
        if (p && !inRoom(r, p)) m.classList.add("rv-out");
      }
      for (const t of svg.querySelectorAll("[data-roomtap]")) if (t.dataset.roomtap !== r.id) t.classList.add("rv-out");
      paint(r);
    }
  }
  // ⤢ in a free corner of each room: top right, else bottom right / bottom left if a marker sits there.
  function renderOpeners(g) {
    const m = mppOf(st.viewBox), k = st.markerScale || 1, R = 0.26 * k, inset = 0.08;
    // Markers and door swings (or windows) to keep clear of.
    const obstacles = st.layout.placements.filter((p) => !st.devices.get(p.entity_id)?.hidden).map((p) => ({ x: p.x, y: p.y, r: R }));
    for (const o of st.layout.openings || []) {
      if (o.type === "door") obstacles.push({ x: o.x, y: o.y, r: o.len }, { x: openEnd(o)[0], y: openEnd(o)[1], r: o.len });
      else for (let t = 0; t <= 1; t += 0.25) obstacles.push({ x: o.orient === "h" ? o.x + o.len * t : o.x, y: o.orient === "h" ? o.y : o.y + o.len * t, r: 0.1 });
    }
    for (const r of st.layout.rooms) {
      const poly = roomPoly(r), ys = poly.map((p) => p[1]);
      const yT = Math.min(...ys), yB = Math.max(...ys);
      const ext = (y, f) => f(...poly.filter((p) => Math.abs(p[1] - y) < 1e-6).map((p) => p[0]));
      const s = clamp(22 * m * k, 0.3, Math.min(0.8, r.w / 3, r.h / 3));
      const corners = [[ext(yT, Math.max), yT, -1, 1], [ext(yB, Math.max), yB, -1, -1], [ext(yB, Math.min), yB, 1, -1]]
        .map(([x, y, dx, dy]) => ({ x, y, dx, dy, cx: x + dx * (inset + s / 2), cy: y + dy * (inset + s / 2) }));
      const free = (c) => !obstacles.some((o) => Math.hypot(o.x - c.cx, o.y - c.cy) < s * 0.72 + o.r);
      const c = corners.find(free) || corners[0];
      const hit = clamp(40 * m, s + inset, Math.min(r.w, r.h) / 2);
      const b = el("g", { class: "room-open", "data-roomopen": r.id, role: "button", tabindex: "0", "aria-label": `Open ${r.name}` }, g);
      el("rect", { class: "hit", x: c.dx < 0 ? c.x - hit : c.x, y: c.dy > 0 ? c.y : c.y - hit, width: hit, height: hit }, b);
      el("rect", { class: "bg", x: c.cx - s / 2, y: c.cy - s / 2, width: s, height: s, rx: s * 0.28 }, b);
      el("path", { d: EXPAND, transform: `translate(${c.cx - s * 0.3} ${c.cy - s * 0.3}) scale(${(s * 0.6) / 24})` }, b);
      const t = el("title", {}, b); t.textContent = `Open ${r.name}`;
    }
  }

  // ---------- rooms sheet (⋯ → Rooms…) ----------
  function openRooms() {
    const list = $("roomsList"); list.replaceChildren();
    if (!st.layout.rooms.length) list.append(mk("li", "hint", "No rooms yet — add some in Edit."));
    for (const r of st.layout.rooms) {
      const f = roomFacts(r), li = mk("li"), b = mk("button", "rooms-item"); b.type = "button"; b.dataset.room = r.id;
      const info = [f.lights.length ? (f.lightsOn ? `${f.lightsOn} of ${f.lights.length} on` : "lights off") : "",
        f.temp != null ? `${(Math.round(f.temp * 10) / 10).toFixed(1)}°` : "", f.open.length ? `${f.open.length} open` : ""].filter(Boolean).join(" · ");
      b.append(mk("span", "name", r.name), mk("span", "sub", info), mk("span", "go", "›"));
      if (f.lightsOn) b.classList.add("lit");
      b.onclick = () => { closeRooms(); if (st.editing) { setStatus("Finish editing first", true); return; } open(r.id); };
      li.append(b); list.append(li);
    }
    sheet.hidden = false;
  }
  function closeRooms() { sheet.hidden = true; }
  $("roomsClose").onclick = closeRooms;
  sheet.addEventListener("click", (e) => { if (e.target === sheet) closeRooms(); });
  if ($("roomsBtn")) $("roomsBtn").onclick = openRooms;

  // ---------- input ----------
  $("roomBack").onclick = () => close();
  $("roomPrev").onclick = () => step(-1);
  $("roomNext").onclick = () => step(1);
  $("roomLights").onclick = () => { const r = room(); if (r) toggleRoomLights(r); };
  svg.addEventListener("click", (e) => {
    if (st.editing) return;
    const ob = e.target.closest?.("[data-roomopen]");
    if (ob) { open(ob.dataset.roomopen); return; }
    // Double-tap / double-click on a room's floor (names and markers keep their own taps).
    const rm = !st.room && !e.target.closest?.(".marker, [data-roomtap]") && e.target.closest?.(".room");
    if (!rm) { rv.lastTap = null; return; }
    const now = Date.now(), lt = rv.lastTap;
    if (lt && lt.id === rm.dataset.room && now - lt.t < DTAP_MS && Math.hypot(e.clientX - lt.x, e.clientY - lt.y) < DTAP_PX) {
      rv.lastTap = null; open(rm.dataset.room);
    } else rv.lastTap = { id: rm.dataset.room, t: now, x: e.clientX, y: e.clientY };
  });
  svg.addEventListener("keydown", (e) => {
    const ob = e.target.closest?.("[data-roomopen]");
    if (ob && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); open(ob.dataset.roomopen); }
  });
  // Long-press on a room's floor.
  const lpCancel = () => { if (rv.lp) clearTimeout(rv.lp.timer); rv.lp = null; };
  svg.addEventListener("pointerdown", (e) => {
    lpCancel();
    if (st.editing || st.room || e.button > 0 || e.target.closest?.(".marker, [data-roomtap], [data-roomopen]")) return;
    const rm = e.target.closest?.(".room"); if (!rm) return;
    rv.lp = { id: rm.dataset.room, x: e.clientX, y: e.clientY, pid: e.pointerId };
    rv.lp.timer = setTimeout(() => { const id = rv.lp?.id; rv.lp = null; rv.lpFired = Date.now(); navigator.vibrate?.(12); open(id); }, LP_MS);
  });
  svg.addEventListener("pointermove", (e) => { if (rv.lp && e.pointerId === rv.lp.pid && Math.hypot(e.clientX - rv.lp.x, e.clientY - rv.lp.y) > LP_MOVE) lpCancel(); });
  for (const ev of ["pointerup", "pointercancel", "pointerleave"]) svg.addEventListener(ev, lpCancel);
  svg.addEventListener("contextmenu", (e) => { if (!st.editing && e.target.closest?.(".room, [data-roomopen]")) e.preventDefault(); });
  // The click that ends a long-press lands on the zoomed plan: it must not toggle whatever is under the finger now.
  document.addEventListener("click", (e) => {
    if (rv.lpFired && Date.now() - rv.lpFired < 1200) { rv.lpFired = 0; e.stopPropagation(); e.preventDefault(); }
  }, true);
  // Swipe between rooms.
  const wrap = $("planWrap");
  wrap.addEventListener("touchstart", (e) => {
    const t = e.touches[0];
    rv.touch = st.room && e.touches.length === 1 ? { x: t.clientX, y: t.clientY, t: Date.now() } : null;
  }, { passive: true });
  wrap.addEventListener("touchend", (e) => {
    const s = rv.touch; rv.touch = null;
    if (!s || !st.room || e.touches.length) return;
    const t = e.changedTouches[0], dx = t.clientX - s.x, dy = t.clientY - s.y;
    if (Math.abs(dx) >= SWIPE_PX && Math.abs(dx) > 1.5 * Math.abs(dy) && Date.now() - s.t < 900) step(dx < 0 ? 1 : -1);
  });
  wrap.addEventListener("touchcancel", () => { rv.touch = null; });
  const busy = () => !$("sheet").hidden || !$("moreMenu").hidden || !!document.querySelector("dialog[open]")
    || [...document.querySelectorAll(".sheet")].some((s) => !s.hidden);
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !sheet.hidden) { closeRooms(); e.stopImmediatePropagation(); return; }
    if (!st.room || st.editing || (typeof WALL !== "undefined" && WALL.dimmed) || busy()) return;
    if (e.target.closest?.("input, select, textarea")) return;
    if (e.key === "Escape") { e.preventDefault(); e.stopImmediatePropagation(); close(); }
    else if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); step(e.key === "ArrowLeft" ? -1 : 1); }
  }, true);
  // Edit always starts from the whole home.
  document.addEventListener("click", (e) => { if (st.room && e.target.closest?.("#editToggle")) close({ animate: false }); }, true);
  window.addEventListener("resize", () => { if (st.room) render(); });
  document.addEventListener("DOMContentLoaded", () => {
    // Wall mode: Back in the wall bar; dimming returns to the whole home.
    const acts = document.querySelector("#wallBar .wall-acts");
    if (acts) {
      const b = mk("button", "wall-back"); b.id = "wallRoomBack"; b.type = "button"; b.hidden = true;
      b.innerHTML = `<span aria-hidden="true">‹</span><span>Home</span>`; b.setAttribute("aria-label", "Back to the whole home");
      b.onclick = () => close(); acts.prepend(b);
    }
    const dim = $("wallDim");
    if (dim) new MutationObserver(() => { if (!dim.hidden && st.room) close({ animate: false }); }).observe(dim, { attributes: true, attributeFilter: ["hidden"] });
  });

  window.roomViewBox = roomViewBox; window.roomSideFilter = roomSideFilter; window.renderRoomView = renderRoomView;
  return { open, close, step, openRooms, roomFacts, get room() { return st.room || null; }, get animating() { return !!rv.anim; } };
})();
