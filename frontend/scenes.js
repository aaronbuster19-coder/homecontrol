"use strict";
// Scenes: one-tap presets for lights (on/off, brightness, white or colour), plugs and TVs (on/off, source), optionally
// starting a disco (backend/scenes.py runs them: one batched set of HA calls; fridges, the home server and keep-on plugs
// are never switched off). Ways in: ⋯ → Scenes…, the scene chips in a room view (scenes saved for that room) and a
// row above the quick tiles. Build one by ticking devices — each starts from its current state ("capture") — then
// adjust by hand; "Use current state" re-reads them all from Home Assistant. Guests see and run light-only scenes.
// Hooks: scenesRoomChips(r) (roomview.js facts). Uses app.js (st, api, $, setStatus, KIND_LABEL, icon, iconKind,
// deviceColor), controls.js (SWATCHES, hsToRgb, hex, keepOn, usable), appliances.js (protectedPlugs), modes.js (plural).
const SCENES = (() => {
  const sc = { list: [], meta: {}, loaded: false, ed: null, busy: false, msg: "", err: false, running: null };
  const sx = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const KINDS = ["light", "plug", "media"];
  const PLAY = `<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M8 5.5v13a1 1 0 0 0 1.5.86l10.4-6.5a1 1 0 0 0 0-1.72L9.5 4.64A1 1 0 0 0 8 5.5z"/></svg>`;
  const role = () => document.documentElement.dataset.role || "admin";
  const editor = () => role() !== "guest";
  const room = (id) => st.layout.rooms.find((r) => r.id === id) || null;
  const guarded = () => new Set([...(typeof keepOn === "function" ? keepOn() : []), ...(typeof protectedPlugs === "function" ? protectedPlugs() : [])]);
  const name = (eid) => st.devices.get(eid)?.name || eid;
  const MINUTES = [10, 20, 30, 60, 90, 120];

  // ---------- DOM ----------
  const item = sx("button", null, "Scenes…"); item.id = "scenesBtn"; item.type = "button"; item.setAttribute("role", "menuitem");
  const anchor = $("discoBtn") || $("roomsBtn"); if (anchor) anchor.after(item); else $("moreMenu").prepend(item);
  const sheet = sx("div", "sheet scenes-sheet"); sheet.id = "scenesSheet"; sheet.hidden = true;
  sheet.innerHTML = `<div class="sheet-body" role="dialog" aria-modal="true" aria-labelledby="scenesTitle">
    <button class="close" id="scenesClose" type="button" aria-label="Close">×</button><div id="scenesContent"></div></div>`;
  document.body.append(sheet);
  const tilesRow = sx("div", "tiles-scenes"); tilesRow.id = "tilesScenes"; tilesRow.hidden = true;
  $("tilesGrid")?.before(tilesRow);

  // ---------- summary text ----------
  function summary(s) {
    const n = { light: 0, plug: 0, media: 0 }, on = s.actions.filter((a) => a.on).length;
    for (const a of s.actions) n[a.entity_id.startsWith("light.") ? "light" : a.entity_id.startsWith("switch.") ? "plug" : "media"]++;
    const parts = [n.light && plural(n.light, "light"), n.plug && plural(n.plug, "plug"), n.media && (n.media === 1 ? "TV" : `${n.media} TVs`)].filter(Boolean);
    const what = !s.actions.length ? "" : on === s.actions.length ? " on" : on === 0 ? " off" : "";
    return [parts.join(" · ") + what, s.disco ? "disco" : "", s.room && room(s.room) ? room(s.room).name : ""].filter(Boolean).join(" · ");
  }

  // ---------- run ----------
  async function run(s) {
    if (sc.running) return;
    sc.running = s.id; paintAll();
    try {
      const r = await api(`/api/scenes/${encodeURIComponent(s.id)}/run`, { method: "POST" });
      const bad = [...r.failed.map((f) => `${f.name} failed`), ...r.skipped.map((x) => `${x.name}: ${x.reason}`)];
      const disco = r.disco ? (r.disco.started ? " · disco on" : ` · no disco (${r.disco.reason})`) : "";
      setStatus(`Scene “${r.name}”${r.repeat ? " (already done)" : ""}${disco}${bad.length ? " · " + bad.join(", ") : ""}`, !!r.failed.length);
      if (r.disco?.started && typeof DISCO !== "undefined") DISCO.load();
    } catch (e) { setStatus(`Scene failed: ${e.message}`, true); }
    finally { sc.running = null; paintAll(); }
    if (!st.live) setTimeout(loadDevices, 800);
  }
  function runButton(s, cls) {
    const b = sx("button", cls); b.type = "button"; b.dataset.scene = s.id; b.disabled = sc.running === s.id;
    b.innerHTML = PLAY; b.append(sx("span", "scene-name", s.name));
    b.setAttribute("aria-label", `Run scene ${s.name}`); b.title = summary(s) || s.name;
    b.onclick = () => run(s);
    return b;
  }

  // ---------- list ----------
  function openSheet() { if (st.editing) { setStatus("Finish editing first", true); return; } sc.ed = null; sc.msg = ""; draw(); sheet.hidden = false; load().then(() => { if (!sheet.hidden && !sc.ed) draw(); }); $("scenesClose").focus({ preventScroll: true }); }
  function closeSheet() { sheet.hidden = true; sc.ed = null; }
  function draw() { const c = $("scenesContent"); c.replaceChildren(); (sc.ed ? drawEdit : drawList)(c); }
  function drawList(c) {
    const h = sx("h3", null, "Scenes"); h.id = "scenesTitle"; c.append(h);
    c.append(sx("div", "sub", "One tap sets lights, plugs and TVs the way you saved them."));
    const ul = sx("ul", "scene-list"); ul.id = "sceneList";
    if (sc.loaded && !sc.list.length) ul.append(sx("li", "hint", editor() ? "No scenes yet — make one for movie night or bedtime." : "No scenes yet."));
    for (const s of sc.list) {
      const li = sx("li", "scene-item"); const b = runButton(s, "scene-run");
      b.append(sx("span", "scene-sum", summary(s))); li.append(b);
      if (editor()) { const e = sx("button", "scene-edit", "Edit"); e.type = "button"; e.setAttribute("aria-label", `Edit ${s.name}`); e.onclick = () => edit(s); li.append(e); }
      ul.append(li);
    }
    c.append(ul);
    if (editor()) {
      const add = sx("button", "big primary", "+ New scene"); add.type = "button"; add.id = "sceneNew";
      add.disabled = sc.list.length >= (sc.meta.max || 30); add.onclick = () => edit(null); c.append(add);
    }
  }

  // ---------- editor ----------
  // A device's current state as a scene action (the server's capture does the same; this is the instant preview).
  function captureLocal(d) {
    if (d.kind === "media") {
      const on = typeof tvIsOn === "function" ? tvIsOn(d) : !["off", "standby"].includes(d.state);
      return { entity_id: d.entity_id, on, ...(on && d.source && (d.source_list || []).includes(d.source) ? { source: d.source } : {}) };
    }
    const a = { entity_id: d.entity_id, on: d.state === "on" };
    if (d.kind === "plug" && !a.on && guarded().has(d.entity_id)) a.on = true; // protected: a scene only switches it on
    if (d.kind === "light" && a.on) {
      if (d.supports_brightness && d.brightness != null) a.brightness_pct = Math.max(1, Math.min(100, Math.round(d.brightness / 2.55)));
      if (d.color_mode === "color_temp" && d.color_temp_kelvin) a.color_temp_kelvin = d.color_temp_kelvin;
      else if (d.supports_color && Array.isArray(d.hs_color)) a.hs_color = d.hs_color.map((v) => Math.round(v * 10) / 10);
    }
    return a;
  }
  function edit(s) {
    sc.ed = s ? { id: s.id, name: s.name, room: s.room || "", acts: new Map(s.actions.map((a) => [a.entity_id, { ...a }])), disco: s.disco ? { ...s.disco } : null, all: !s.room }
      : { id: null, name: "", room: st.room || "", acts: new Map(), disco: null, all: !st.room };
    sc.msg = ""; draw();
    $("sceneName")?.focus({ preventScroll: true });
  }
  const candidates = () => [...st.devices.values()].filter((d) => KINDS.includes(d.kind) && !d.hidden);
  function inRoomIds(r) {
    const ids = new Set(st.layout.placements.filter((p) => inRoom(r, p)).map((p) => p.entity_id));
    for (const f of st.layout.furniture || []) if (f.media && inRoom(r, f)) ids.add(f.media);
    return ids;
  }
  function seg(a, onPick, offDisabled) {
    const g = sx("div", "scene-seg"); g.setAttribute("role", "radiogroup");
    for (const [v, label] of [[true, "On"], [false, "Off"]]) {
      const b = sx("button", a.on === v ? "sel" : "", label); b.type = "button"; b.setAttribute("role", "radio");
      b.setAttribute("aria-checked", a.on === v ? "true" : "false"); b.dataset.on = v ? "1" : "0";
      if (!v && offDisabled) { b.disabled = true; b.title = "Protected — scenes never switch it off"; }
      b.onclick = () => onPick(v); g.append(b);
    }
    return g;
  }
  function lightControls(d, a, box) {
    if (d.supports_brightness) {
      if (a.brightness_pct == null) a.brightness_pct = 100; // what the slider shows is what the scene sets
      const lab = sx("label", "scene-ctl"); const top = sx("span", "ctl-top");
      const pct = a.brightness_pct ?? 100; const out = sx("span", "ctl-val", `${pct} %`);
      top.append(sx("span", null, "Brightness"), out);
      const r = sx("input"); Object.assign(r, { type: "range", min: 1, max: 100, step: 1, value: pct });
      r.setAttribute("aria-label", `${d.name} brightness`);
      r.oninput = () => { a.brightness_pct = +r.value; out.textContent = `${r.value} %`; };
      lab.append(top, r); box.append(lab);
    }
    if (!d.supports_color && !d.supports_color_temp) return;
    const mode = a.hs_color || a.rgb_color ? "colour" : a.color_temp_kelvin ? "white" : "keep";
    const sel = sx("select", "scene-colour"); sel.setAttribute("aria-label", `${d.name} colour`);
    for (const [v, l, ok] of [["keep", "Colour: keep as it is", true], ["white", "White", d.supports_color_temp], ["colour", "Colour", d.supports_color]]) {
      if (!ok) continue; const o = sx("option", null, l); o.value = v; sel.append(o);
    }
    sel.value = mode;
    sel.onchange = () => {
      delete a.hs_color; delete a.rgb_color; delete a.color_temp_kelvin;
      if (sel.value === "white") a.color_temp_kelvin = d.color_temp_kelvin || 2700;
      if (sel.value === "colour") a.hs_color = Array.isArray(d.hs_color) ? [...d.hs_color] : [28, 100];
      draw();
    };
    box.append(sel);
    if (mode === "white") {
      const lo = d.min_color_temp_kelvin || 2500, hi = d.max_color_temp_kelvin || 6500;
      const lab = sx("label", "scene-ctl kelvin"); const top = sx("span", "ctl-top"); const out = sx("span", "ctl-val", `${a.color_temp_kelvin} K`);
      top.append(sx("span", null, "White"), out);
      const r = sx("input"); Object.assign(r, { type: "range", min: lo, max: hi, step: 50, value: Math.min(hi, Math.max(lo, a.color_temp_kelvin)) });
      r.setAttribute("aria-label", `${d.name} white temperature`);
      r.oninput = () => { a.color_temp_kelvin = +r.value; out.textContent = `${r.value} K`; };
      lab.append(top, r); box.append(lab);
    }
    if (mode === "colour") {
      const row = sx("div", "swatches scene-swatches");
      const cur = a.hs_color ? hex(hsToRgb(...a.hs_color)) : a.rgb_color ? hex(a.rgb_color) : null;
      for (const hs of SWATCHES) {
        const c = hex(hsToRgb(...hs)); const b = sx("button", "swatch" + (c === cur ? " sel" : "")); b.type = "button";
        b.style.background = c; b.setAttribute("aria-label", `Colour hue ${hs[0]}`); b.setAttribute("aria-pressed", c === cur ? "true" : "false");
        b.onclick = () => { delete a.rgb_color; a.hs_color = [...hs]; draw(); };
        row.append(b);
      }
      box.append(row);
    }
  }
  function mediaControls(d, a, box) {
    const list = d.source_list || [];
    if (!d.supports?.select_source || !list.length) return;
    const sel = sx("select", "scene-source"); sel.setAttribute("aria-label", `${d.name} source`);
    const k = sx("option", null, "Source: keep as it is"); k.value = ""; sel.append(k);
    for (const s of list) { const o = sx("option", null, s); o.value = s; sel.append(o); }
    if (a.source && !list.includes(a.source)) { const o = sx("option", null, a.source); o.value = a.source; sel.append(o); }
    sel.value = a.source || "";
    sel.onchange = () => { if (sel.value) a.source = sel.value; else delete a.source; };
    box.append(sel);
  }
  function deviceRow(d, keep) {
    const ed = sc.ed, a = ed.acts.get(d.entity_id);
    const li = sx("li", "scene-dev" + (a ? " picked" : "")); li.dataset.dev = d.entity_id;
    const lab = sx("label", "scene-pick"); const cb = sx("input"); cb.type = "checkbox"; cb.checked = !!a;
    cb.setAttribute("aria-label", `Include ${d.name}`);
    cb.onchange = () => { if (cb.checked) ed.acts.set(d.entity_id, captureLocal(d)); else ed.acts.delete(d.entity_id); draw(); };
    lab.append(cb, icon(iconKind(d, d.kind), deviceColor(d)), sx("span", "name", d.name));
    if (!usable(d)) lab.append(sx("span", "scene-na", d.state));
    li.append(lab);
    if (a) {
      const box = sx("div", "scene-ctls");
      const prot = d.kind === "plug" && keep.has(d.entity_id);
      box.append(seg(a, (v) => {
        a.on = v;
        if (!v) { delete a.brightness_pct; delete a.hs_color; delete a.rgb_color; delete a.color_temp_kelvin; delete a.source; }
        else if (d.kind === "light") Object.assign(a, captureLocal({ ...d, state: "on" }), { on: true });
        draw();
      }, prot));
      if (a.on && d.kind === "light") lightControls(d, a, box);
      if (a.on && d.kind === "media") mediaControls(d, a, box);
      li.append(box);
    }
    return li;
  }
  function drawEdit(c) {
    const ed = sc.ed;
    const h = sx("h3", null, ed.id ? "Edit scene" : "New scene"); h.id = "scenesTitle"; c.append(h);
    c.append(sx("div", "sub", "Tick devices: each starts as it is now. Change anything, then save."));
    const nm = sx("label", "scene-field"); const ni = sx("input"); ni.id = "sceneName"; ni.maxLength = 40; ni.value = ed.name; ni.placeholder = "Movie night";
    ni.autocomplete = "off"; ni.oninput = () => { ed.name = ni.value; }; nm.append(sx("span", null, "Name"), ni); c.append(nm);
    const rm = sx("label", "scene-field"); const rs = sx("select"); rs.id = "sceneRoom";
    const w = sx("option", null, "Whole home"); w.value = ""; rs.append(w);
    for (const r of st.layout.rooms) { const o = sx("option", null, r.name); o.value = r.id; rs.append(o); }
    rs.value = ed.room; rs.onchange = () => { ed.room = rs.value; ed.all = !rs.value; draw(); };
    rm.append(sx("span", null, "Show in"), rs); c.append(rm);
    const bar = sx("div", "scene-bar"); bar.append(sx("span", "scene-label", `Devices · ${ed.acts.size} picked`));
    const cap = sx("button", "linkish", "Use current state"); cap.type = "button"; cap.id = "sceneCapture"; cap.disabled = !ed.acts.size || sc.busy;
    cap.title = "Read every ticked device from Home Assistant again"; cap.onclick = captureAll; bar.append(cap); c.append(bar);
    const r = ed.room && room(ed.room), here = r ? inRoomIds(r) : null, keep = guarded();
    const ul = sx("ul", "scene-devs"); ul.id = "sceneDevs";
    for (const kind of KINDS) {
      const devs = candidates().filter((d) => d.kind === kind && (ed.all || !here || here.has(d.entity_id) || ed.acts.has(d.entity_id)))
        .sort((a, b) => a.name.localeCompare(b.name));
      if (!devs.length) continue;
      ul.append(sx("li", "group", KIND_LABEL[kind]));
      for (const d of devs) ul.append(deviceRow(d, keep));
    }
    for (const [eid, a] of ed.acts) if (!st.devices.has(eid)) { // gone from HA: kept, shown, removable
      const li = sx("li", "scene-dev picked"); const lab = sx("label", "scene-pick"); const cb = sx("input"); cb.type = "checkbox"; cb.checked = true;
      cb.onchange = () => { ed.acts.delete(eid); draw(); }; lab.append(cb, sx("span", "name", eid), sx("span", "scene-na", `not in HA · ${a.on ? "on" : "off"}`));
      li.append(lab); ul.append(li);
    }
    c.append(ul);
    if (r && !ed.all) { const more = sx("button", "linkish scene-more", "Show devices in other rooms"); more.type = "button"; more.onclick = () => { ed.all = true; draw(); }; c.append(more); }
    drawDisco(c);
    const msg = sx("p", "hint scene-msg" + (sc.err ? " warn" : ""), sc.msg); msg.id = "sceneMsg"; msg.setAttribute("role", "status"); c.append(msg);
    const acts = sx("div", "scene-acts");
    if (ed.id) { const del = sx("button", "danger", "Delete"); del.type = "button"; del.id = "sceneDelete"; del.onclick = remove; acts.append(del); }
    acts.append(sx("span", "spacer"));
    const cancel = sx("button", null, "Cancel"); cancel.type = "button"; cancel.onclick = () => { sc.ed = null; sc.msg = ""; draw(); };
    const save = sx("button", "primary", "Save"); save.type = "button"; save.id = "sceneSave"; save.disabled = sc.busy; save.onclick = submit;
    acts.append(cancel, save); c.append(acts);
  }
  function drawDisco(c) {
    const ed = sc.ed;
    const colour = [...ed.acts.values()].some((a) => a.on && st.devices.get(a.entity_id)?.supports_color);
    if (!colour && !ed.disco) return;
    const box = sx("div", "scene-disco");
    const lab = sx("label", "check"); const cb = sx("input"); cb.type = "checkbox"; cb.id = "sceneDisco"; cb.checked = !!ed.disco;
    cb.onchange = () => { ed.disco = cb.checked ? { preset: "rainbow", speed: "normal", minutes: 30 } : null; draw(); };
    lab.append(cb, sx("span", null, "Then start a disco with its colour lights")); box.append(lab);
    if (ed.disco) {
      const row = sx("div", "scene-disco-opts");
      const ps = sx("select"); ps.setAttribute("aria-label", "Disco style");
      for (const p of sc.meta.disco_presets || [{ id: "rainbow", name: "Rainbow fade" }, { id: "chill", name: "Slow chill" }]) { const o = sx("option", null, p.name); o.value = p.id; ps.append(o); }
      ps.value = ed.disco.preset; ps.onchange = () => { ed.disco.preset = ps.value; };
      const sp = sx("select"); sp.setAttribute("aria-label", "Disco speed");
      for (const v of sc.meta.disco_speeds || ["slow", "normal", "fast"]) { const o = sx("option", null, v[0].toUpperCase() + v.slice(1)); o.value = v; sp.append(o); }
      sp.value = ed.disco.speed; sp.onchange = () => { ed.disco.speed = sp.value; };
      const mn = sx("select"); mn.setAttribute("aria-label", "Disco stops after");
      for (const m of MINUTES) { const o = sx("option", null, m < 60 ? `${m} min` : `${m / 60} h`.replace(".5 h", "½ h")); o.value = m; mn.append(o); }
      mn.value = ed.disco.minutes; mn.onchange = () => { ed.disco.minutes = +mn.value; };
      row.append(ps, sp, mn); box.append(row);
    }
    c.append(box);
  }
  async function captureAll() {
    const ed = sc.ed; if (!ed.acts.size) return;
    sc.busy = true; draw();
    try {
      const r = await api("/api/scenes/capture", { method: "POST", body: JSON.stringify({ entity_ids: [...ed.acts.keys()].filter((e) => st.devices.has(e)) }) });
      for (const a of r.actions) ed.acts.set(a.entity_id, a);
      sc.msg = r.skipped.length ? `Kept as before: ${r.skipped.map((x) => `${name(x.entity_id)} (${x.reason})`).join(", ")}` : "Set from how everything is now."; sc.err = false;
    } catch (e) { sc.msg = e.message; sc.err = true; }
    finally { sc.busy = false; if (sc.ed === ed) draw(); }
  }
  async function submit() {
    const ed = sc.ed;
    const body = { name: ed.name.trim(), room: ed.room || null, actions: [...ed.acts.values()], disco: ed.disco };
    if (!body.name) { sc.msg = "Give the scene a name."; sc.err = true; draw(); $("sceneName")?.focus(); return; }
    sc.busy = true; draw();
    try {
      await api(ed.id ? `/api/scenes/${encodeURIComponent(ed.id)}` : "/api/scenes", { method: ed.id ? "PUT" : "POST", body: JSON.stringify(body) });
      setStatus(`Scene “${body.name}” saved`); sc.ed = null; sc.msg = ""; await load();
    } catch (e) { sc.msg = e.message; sc.err = true; }
    finally { sc.busy = false; if (!sheet.hidden) draw(); }
  }
  async function remove() {
    const ed = sc.ed; if (!confirm(`Delete the scene “${ed.name}”?`)) return;
    try { await api(`/api/scenes/${encodeURIComponent(ed.id)}`, { method: "DELETE" }); sc.ed = null; setStatus(`Scene “${ed.name}” deleted`); await load(); }
    catch (e) { sc.msg = e.message; sc.err = true; }
    if (!sheet.hidden) draw();
  }

  // ---------- room view chips, tiles row ----------
  function roomChips(r) {
    return sc.list.filter((s) => s.room === r.id).map((s) => { const b = runButton(s, "rf scene-chip"); b.dataset.fact = "scene"; return b; });
  }
  function paintTiles() {
    tilesRow.replaceChildren();
    tilesRow.hidden = !sc.list.length;
    if (!sc.list.length) return;
    tilesRow.append(sx("span", "tiles-scenes-label", "Scenes"));
    const row = sx("div", "tiles-scenes-row");
    for (const s of sc.list) row.append(runButton(s, "scene-tile"));
    tilesRow.append(row);
  }
  function paintAll() {
    paintTiles();
    if (!sheet.hidden && !sc.ed) draw();
    if (st.room && typeof render === "function") render();
  }
  async function load() {
    try { const r = await api("/api/scenes"); sc.list = r.scenes || []; sc.meta = r; sc.loaded = true; } catch {}
    paintAll();
  }

  item.onclick = () => { $("moreMenu").hidden = true; openSheet(); };
  $("scenesClose").onclick = closeSheet;
  sheet.addEventListener("click", (e) => { if (e.target === sheet) closeSheet(); });
  window.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || sheet.hidden) return;
    e.stopImmediatePropagation();
    if (sc.ed) { sc.ed = null; sc.msg = ""; draw(); } else closeSheet();
  }, true);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
  document.addEventListener("DOMContentLoaded", load);
  return { open: openSheet, close: closeSheet, load, run, roomChips, get list() { return sc.list; } };
})();
function scenesRoomChips(r) { return SCENES.roomChips(r); }
