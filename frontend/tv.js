"use strict";
// TVs and media players (kind "media", backend/media.py): Samsung via SmartThings / Samsung Smart TV, LG, Android TV,
// Chromecasts, speakers. A tap never switches anything: the marker, list row or linked TV opens the TV sheet with power,
// volume (slider throttled like brightness, −/+), mute, transport, source and sound mode — only what the entity's
// supported_features allow — plus now playing with artwork (proxied by the server). A TV piece of furniture can link
// one media player (layout furniture[].media): its screen lights up while on, with the app / title as a small label,
// and its media marker is hidden. With the TV's plug also linked, label and sheet show the watts too.
// Hooks: tvColor, tvValue, mediaIcon, tvHidesMarker, tvSheet (app.js); tvInAllOff, tvIsOn (controls.js All off);
// tvLinked, renderTvs (furniture.js, appliances.js); tvRoomFacts (roomview.js); tvWallPaint (wall.js).
const TV_OFF = ["off", "standby", "unavailable", "unknown"];
const TV_VOL_STEP = 0.05;
const tvMk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const tvSvg = (path, cls) => { const s = document.createElementNS(NS, "svg"); s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("aria-hidden", "true"); if (cls) s.setAttribute("class", cls); s.innerHTML = path; return s; };
const TV_IC = {
  power: '<path d="M12 3v8M6.3 6.3a8 8 0 1 0 11.4 0" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>',
  prev: '<path d="M6 5h2v14H6zm3.5 7L18 18V6z" fill="currentColor"/>',
  next: '<path d="M16 5h2v14h-2zM6 6v12l8.5-6z" fill="currentColor"/>',
  play: '<path d="M8 5v14l11-7z" fill="currentColor"/>',
  pause: '<path d="M7 5h4v14H7zm6 0h4v14h-4z" fill="currentColor"/>',
  vol: '<path d="M4 9v6h4l5 4V5L8 9zm12.5 3a4.5 4.5 0 0 0-2.5-4v8a4.5 4.5 0 0 0 2.5-4zM14 3.2v2.1a7 7 0 0 1 0 13.4v2.1a9 9 0 0 0 0-17.6z" fill="currentColor"/>',
  muted: '<path d="M4 9v6h4l5 4V5L8 9zm12.6 3 2.7-2.7-1.3-1.3-2.7 2.7-2.7-2.7-1.3 1.3 2.7 2.7-2.7 2.7 1.3 1.3 2.7-2.7 2.7 2.7 1.3-1.3z" fill="currentColor"/>',
  bolt: '<path d="M13 2 4 14h7l-1 8 9-12h-7z" fill="currentColor"/>',
};

// ---------- model ----------
const tvIsOn = (d) => !!d && d.kind === "media" && !TV_OFF.includes(d.state);
const tvCan = (d, cap) => !!d?.supports?.[cap];
const tvLinked = (f) => !!f?.media && f.type === "tv";
function tvPieceFor(eid, L = st.layout) { return eid ? (L.furniture || []).find((f) => f.media === eid && f.type === "tv") || null : null; }
function mediaIcon(d) { return d?.media_type === "speaker" ? "media-speaker" : "media"; }
// What's on: "The Crown — Season 2", else the app ("Netflix"), else the source ("HDMI1").
function tvNow(d) {
  if (!tvIsOn(d)) return "";
  if (d.media_title) return d.media_series_title && d.media_series_title !== d.media_title ? `${d.media_title} — ${d.media_series_title}` : d.media_artist ? `${d.media_title} — ${d.media_artist}` : d.media_title;
  return d.app_name || d.source || "";
}
// The plan label: shortest useful text.
function tvShort(d) { return !tvIsOn(d) ? "" : d.app_name || d.media_title || d.source || "On"; }
const TV_STATE = { on: "On", playing: "Playing", paused: "Paused", idle: "On", buffering: "Loading…", off: "Off", standby: "Standby" };
const tvStateText = (d) => TV_STATE[d.state] || d.state;
function tvColor(d) {
  if (!tvIsOn(d)) return "var(--off)";
  return d.state === "playing" ? "var(--tv-play, #7c6cff)" : "var(--tv-on, #4f8cff)";
}
function tvValue(d) {
  if (!tvIsOn(d)) return d.state === "standby" ? "standby" : "off";
  const parts = [tvShort(d) === "On" ? tvStateText(d).toLowerCase() : tvShort(d)];
  if (d.is_volume_muted) parts.push("muted");
  return parts.join(" · ");
}
function tvHidesMarker(eid) { return !st.editing && showFurniture && !!tvPieceFor(eid) && st.devices.has(eid); }
// controls.js All off: a TV only when ticked in its sheet (off by default).
function tvInAllOff(d) { return d?.kind === "media" && (st.layout.settings?.all_off_include || []).includes(d.entity_id) && tvCan(d, "turn_off"); }

// ---------- control ----------
async function tvSend(d, body, ok) {
  try { await api(`/api/devices/${encodeURIComponent(d.entity_id)}/media`, { method: "POST", body: JSON.stringify(body) }); if (ok) setStatus(ok); }
  catch (e) { setStatus(`${d.name}: ${e.message}`, true); if (st.sheetFor === d.entity_id && !st.holdSheet) renderSheet(); }
  if (!st.live) setTimeout(loadDevices, 800);
}
// Optimistic change, kept on screen until the live update confirms it.
// The live stream may have replaced the device object since the sheet was drawn: patch the current one.
function tvPatch(d, patch) {
  Object.assign(d, patch); const now = st.devices.get(d.entity_id); if (now && now !== d) Object.assign(now, patch);
  render(); if (st.sheetFor === d.entity_id) renderSheet();
}
// Live updates that arrive while a control is held are not drawn; draw the sheet once the hold is over.
let tvRedraw = null;
function tvHold(eid, ms = 1500) {
  holdSheet(ms); clearTimeout(tvRedraw);
  tvRedraw = setTimeout(() => { if (st.sheetFor === eid && !st.holdSheet) renderSheet(); }, ms + 100);
}

// ---------- sheet ----------
function tvSheet(d, c, unavailable) {
  const on = tvIsOn(d), wrap = tvMk("div", "tv-sheet" + (on ? " on" : "")); c.appendChild(wrap);
  // now playing card
  const card = tvMk("div", "tv-now" + (on ? " on" : "") + (d.picture ? " art" : ""));
  const art = tvMk("div", "tv-art");
  if (d.picture && on) {
    const img = tvMk("img"); img.alt = ""; img.src = d.picture; img.decoding = "async";
    img.onerror = () => { img.remove(); card.classList.remove("art"); };
    art.appendChild(img);
  }
  const glyph = tvSvg(`<use href="#ic-${mediaIcon(d)}"/>`, "tv-glyph"); art.prepend(glyph);
  const info = tvMk("div", "tv-info");
  const chip = tvMk("span", "tv-state " + (unavailable ? "unavail" : on ? d.state : "off"), unavailable ? d.state : tvStateText(d));
  info.appendChild(chip);
  const title = on ? (d.media_title || d.app_name || d.source || "On") : unavailable ? "Not reachable" : d.state === "standby" ? "Standby" : "Off";
  info.appendChild(tvMk("div", "tv-title", title));
  const subs = on ? [d.media_title ? (d.media_series_title || d.media_artist) : null, d.media_title && d.app_name ? d.app_name : null,
    d.source && d.source !== title && d.source !== d.app_name ? d.source : null].filter(Boolean) : [];
  if (subs.length) info.appendChild(tvMk("div", "tv-subtitle", subs.join(" · ")));
  card.append(art, info); wrap.appendChild(card);

  // power
  const pw = tvMk("div", "tv-power");
  if (on && tvCan(d, "turn_off")) {
    const b = tvMk("button", "tv-pwr is-on"); b.type = "button"; b.dataset.tv = "power";
    b.append(tvSvg(TV_IC.power), tvMk("span", null, "Turn off"));
    b.onclick = () => { tvPatch(d, { state: "off" }); tvSend(d, { action: "power", on: false }, `${d.name} off`); };
    pw.appendChild(b);
  } else if (!on && tvCan(d, "turn_on")) {
    const b = tvMk("button", "tv-pwr"); b.type = "button"; b.dataset.tv = "power";
    b.append(tvSvg(TV_IC.power), tvMk("span", null, "Turn on"));
    b.onclick = () => { b.disabled = true; b.lastChild.textContent = "Turning on…"; tvSend(d, { action: "power", on: true }, `Turning on ${d.name}…`); };
    pw.appendChild(b);
  } else if (!on) {
    const h = tvMk("div", "tv-hint"); h.dataset.tv = "no-turn-on";
    h.append(tvMk("b", null, "Can't be switched on from here"),
      tvMk("span", null, "Most TVs can't be woken over the network once they're fully off. Use the remote — or in Home Assistant enable Wake-on-LAN (Samsung Smart TV) or “Turn on” for this TV, then refresh devices."));
    pw.appendChild(h);
  }
  if (pw.childElementCount) wrap.appendChild(pw);

  if (on) {
    // transport
    const tr = tvMk("div", "tv-transport");
    const tbtn = (cap, icon, label, action) => {
      if (!tvCan(d, cap)) return;
      const b = tvMk("button", "tv-round"); b.type = "button"; b.dataset.tv = action; b.setAttribute("aria-label", label); b.title = label;
      b.appendChild(tvSvg(icon)); b.onclick = () => {
        if (action === "play_pause") tvPatch(d, { state: d.state === "playing" ? "paused" : "playing" });
        tvSend(d, { action });
      };
      tr.appendChild(b);
    };
    tbtn("previous", TV_IC.prev, "Previous", "previous");
    tbtn("play_pause", d.state === "playing" ? TV_IC.pause : TV_IC.play, d.state === "playing" ? "Pause" : "Play", "play_pause");
    tbtn("next", TV_IC.next, "Next", "next");
    if (tr.childElementCount) { tr.querySelector('[data-tv="play_pause"]')?.classList.add("main"); wrap.appendChild(tr); }

    // volume
    if (tvCan(d, "volume_set") || tvCan(d, "volume_step") || tvCan(d, "mute")) {
      const vol = tvMk("div", "tv-vol");
      if (tvCan(d, "mute")) {
        const m = tvMk("button", "tv-round tv-mute" + (d.is_volume_muted ? " muted" : "")); m.type = "button"; m.dataset.tv = "mute";
        m.setAttribute("aria-pressed", d.is_volume_muted ? "true" : "false"); m.setAttribute("aria-label", d.is_volume_muted ? "Unmute" : "Mute");
        m.title = d.is_volume_muted ? "Unmute" : "Mute";
        m.appendChild(tvSvg(d.is_volume_muted ? TV_IC.muted : TV_IC.vol));
        m.onclick = () => { const v = !d.is_volume_muted; tvPatch(d, { is_volume_muted: v }); tvSend(d, { action: "mute", muted: v }, v ? `${d.name} muted` : `${d.name} unmuted`); };
        vol.appendChild(m);
      }
      const level = d.volume_level ?? 0;
      const pct = (v) => `${Math.round(v * 100)}`;
      if (tvCan(d, "volume_set")) {
        const t = throttle((v) => tvSend(d, { action: "volume", level: v }));
        const go = (v) => { d.volume_level = v; const now = st.devices.get(d.entity_id); if (now) now.volume_level = v; tvHold(d.entity_id); };
        const s = slider("Volume", 0, 1, 0.01, level, pct, (v) => { go(v); t.push(v); }, (v) => { go(v); t.flush(v); });
        s.classList.add("tv-slider"); s.querySelector("input").dataset.tv = "volume"; s.querySelector("input").setAttribute("aria-label", "Volume");
        vol.appendChild(s);
        // −/+ move the slider by 5 and send through the same throttle
        const stepBtn = (dir) => {
          const b = tvMk("button", "tv-round tv-step", dir < 0 ? "−" : "+"); b.type = "button"; b.dataset.tv = dir < 0 ? "vol-down" : "vol-up";
          b.setAttribute("aria-label", dir < 0 ? "Volume down" : "Volume up");
          b.onclick = () => {
            const r = s.querySelector("input"), v = Math.min(1, Math.max(0, Math.round((+r.value + dir * TV_VOL_STEP) * 100) / 100));
            r.value = v; s.querySelector(".ctl-val").textContent = pct(v); go(v); t.push(v);
          };
          return b;
        };
        s.before(stepBtn(-1)); s.after(stepBtn(1));
      } else if (tvCan(d, "volume_step")) {
        for (const [dir, lab] of [["down", "−"], ["up", "+"]]) {
          const b = tvMk("button", "tv-round tv-step", lab); b.type = "button"; b.dataset.tv = `vol-${dir}`;
          b.setAttribute("aria-label", `Volume ${dir}`); b.onclick = () => tvSend(d, { action: "volume", step: dir });
          vol.appendChild(b);
        }
        if (d.volume_level != null) vol.insertBefore(tvMk("span", "tv-vol-val", pct(level)), vol.lastChild);
      }
      wrap.appendChild(vol);
    }

    // sources
    if (tvCan(d, "select_source") && d.source_list?.length) {
      const sec = tvMk("div", "tv-sec"); sec.appendChild(tvMk("div", "tv-sec-h", "Source"));
      if (d.source_list.length <= 12) {
        const row = tvMk("div", "tv-sources"); row.setAttribute("role", "radiogroup"); row.setAttribute("aria-label", "Source");
        for (const s of d.source_list) {
          const b = tvMk("button", "tv-src" + (s === d.source ? " cur" : ""), s); b.type = "button"; b.dataset.source = s;
          b.setAttribute("role", "radio"); b.setAttribute("aria-checked", s === d.source ? "true" : "false");
          b.onclick = () => { if (s === d.source) return; tvPatch(d, { source: s }); tvSend(d, { action: "source", source: s }, `${d.name}: ${s}`); };
          row.appendChild(b);
        }
        sec.appendChild(row);
      } else {
        const sel = tvMk("select", "tv-src-sel"); sel.setAttribute("aria-label", "Source");
        for (const s of d.source_list) sel.appendChild(new Option(s, s));
        if (d.source && !d.source_list.includes(d.source)) sel.appendChild(new Option(d.source, d.source));
        sel.value = d.source || ""; sel.onchange = () => { tvPatch(d, { source: sel.value }); tvSend(d, { action: "source", source: sel.value }, `${d.name}: ${sel.value}`); };
        sec.appendChild(sel);
      }
      wrap.appendChild(sec);
    }
    // sound mode
    if (tvCan(d, "sound_mode") && d.sound_mode_list?.length) {
      const lab = tvMk("label", "dh-mode tv-sound"); lab.appendChild(tvMk("span", null, "Sound"));
      const sel = tvMk("select"); sel.dataset.tv = "sound_mode";
      for (const m of d.sound_mode_list) sel.appendChild(new Option(m, m));
      if (d.sound_mode && !d.sound_mode_list.includes(d.sound_mode)) sel.appendChild(new Option(d.sound_mode, d.sound_mode));
      sel.value = d.sound_mode || "";
      sel.onchange = () => { tvPatch(d, { sound_mode: sel.value }); tvSend(d, { action: "sound_mode", sound_mode: sel.value }, `${d.name}: ${sel.value}`); };
      lab.appendChild(sel); wrap.appendChild(lab);
    }
  }

  // its plug (TV furniture with both links): watts, and the plug's own sheet
  const f = tvPieceFor(d.entity_id), plug = f?.plug ? st.devices.get(f.plug) : null;
  if (plug) {
    const row = tvMk("div", "tv-plug"); row.dataset.tv = "plug";
    row.appendChild(tvSvg(TV_IC.bolt));
    const parts = [!usable(plug) ? plug.state : plug.state !== "on" ? "Plug off" : plug.power != null ? fmtW(plug.power) : "Plug on"];
    if (plug.energy_today != null) parts.push(`${plug.energy_today.toFixed(2)} kWh today`);
    row.appendChild(tvMk("span", null, parts.join(" · ")));
    const b = tvMk("button", "linkish", "Plug →"); b.type = "button"; b.title = `${plug.name}: on/off and history`;
    b.onclick = () => openSheet(plug.entity_id); row.appendChild(b);
    wrap.appendChild(row);
  } else if (d.power != null) {
    const row = tvMk("div", "tv-plug"); row.appendChild(tvSvg(TV_IC.bolt)); row.appendChild(tvMk("span", null, fmtW(d.power))); wrap.appendChild(row);
  }

  // Include in All off (TVs are left out by default)
  if (tvCan(d, "turn_off")) {
    const lab = tvMk("label", "keep-on tv-alloff"); const cb = tvMk("input"); cb.type = "checkbox";
    cb.checked = (st.layout.settings?.all_off_include || []).includes(d.entity_id);
    lab.append(cb, tvMk("span", null, "Include in “All off”"));
    cb.onchange = async () => {
      const prev = st.layout.settings;
      const set = new Set(prev?.all_off_include || []); cb.checked ? set.add(d.entity_id) : set.delete(d.entity_id);
      st.layout.settings = { keep_on: [], ...(prev || {}), all_off_include: [...set] };
      try { st.layout = await api("/api/layout", { method: "PUT", body: JSON.stringify(st.layout) }); setStatus(cb.checked ? `“All off” also turns off ${d.name}` : "Saved"); }
      catch (e) { st.layout.settings = prev; cb.checked = !cb.checked; setStatus(`Save failed: ${e.message}`, true); }
    };
    wrap.appendChild(lab);
  }
}

// ---------- plan: linked TV furniture ----------
const TV_TAG_PX = 17;
function renderTvs() {
  const g = $("appliances"); if (!g) return;
  for (const n of g.querySelectorAll(".tvp")) n.remove();
  const top = $("applTags"); if (top) for (const n of top.querySelectorAll(".tv-tag")) n.remove();
  const fb = $("furMedia");
  if (st.editing) {
    const f = typeof furSel === "function" ? furSel() : null;
    if (fb) { fb.hidden = !f || f.type !== "tv"; fb.querySelector("span").textContent = f?.media ? `TV: ${st.devices.get(f.media)?.name || f.media}` : "Link TV"; }
    for (const p of st.draft?.furniture || []) { // a small screen badge on pieces showing a media player
      if (!tvLinked(p)) continue;
      const pg = $("furniture")?.querySelector(`[data-fur="${CSS.escape(p.id)}"]`); if (!pg) continue;
      const b = Math.min(0.2, p.w * 0.5, Math.max(p.h, 0.12));
      el("circle", { class: "fu-linkdot tv-linkdot", cx: -p.w / 2 + b * 0.55, cy: -p.h / 2 + b * 0.55, r: b / 2 }, pg);
      el("use", { href: "#ic-media", class: "fu-linkic", x: -p.w / 2 + b * 0.2, y: -p.h / 2 + b * 0.2, width: b * 0.7, height: b * 0.7 }, pg);
    }
    return;
  }
  if (fb) fb.hidden = true;
  if (!showFurniture) return;
  const k = st.labelScale || st.markerScale || 1, m = typeof mpp === "function" ? mpp() : 0.01;
  let drawn = 0;
  for (const f of st.layout.furniture || []) {
    const def = FURNITURE[f.type]; if (!def || !tvLinked(f)) continue;
    const d = st.devices.get(f.media), on = tvIsOn(d), plug = f.plug ? st.devices.get(f.plug) : null;
    const off = !d || !usable(d);
    const fg = el("g", { class: `fur tvp fu-${f.type}` + (on ? " on" : "") + (d?.state === "playing" ? " playing" : "") + (off ? " offline" : ""),
      "data-tv": f.id, "data-dev": f.media, transform: `translate(${f.x} ${f.y}) rotate(${f.rot || 0})` }, g);
    // the light a lit screen throws into the room in front of it
    const x0 = -f.w / 2, y0 = -f.h / 2, sh = Math.max(0.03, Math.min(0.07, f.h * 0.3)), reach = Math.min(1.1, Math.max(0.5, f.w * 0.8));
    if (on) el("path", { class: "tv-spill", fill: "url(#tvSpill)", d: `M${x0 + 0.02} ${y0 + sh}L${-x0 - 0.02} ${y0 + sh}L${-x0 + reach * 0.35} ${y0 + sh + reach}L${x0 - reach * 0.35} ${y0 + sh + reach}Z` }, fg);
    def.draw(fg, f.w, f.h);
    if (on) el("rect", { class: "tv-lit", x: x0 + 0.01, y: y0, width: f.w - 0.02, height: sh, rx: 0.01 }, fg);
    const hw = Math.max(f.w, 40 * m), hh = Math.max(f.h, 40 * m);
    el("rect", { class: "fu-hit tv-hit", x: -hw / 2, y: -hh / 2, width: hw, height: hh }, fg);
    const t = el("title", {}, fg); t.textContent = `${d?.name || f.media} — ${d ? tvValue(d) : "not in Home Assistant"}`;
    drawn++;
    const w = plug?.state === "on" && plug.power != null ? fmtW(plug.power) : "";
    if (top && (on || w)) tvTag(top, f, on ? tvShort(d) : "Standby", w, k, on);
  }
  if (drawn && top && !top.querySelector(".fur-names") && !g.querySelector(".appl") && typeof furRaiseNames === "function") furRaiseNames(top);
}
function tvTag(g, f, text, watts, k, on) {
  const s = (0.17 * k) / TV_TAG_PX, ph = TV_TAG_PX * 1.6;
  const tg = el("g", { class: "appl-tag tv-tag" + (on ? " on" : ""), "data-tag": f.id }, g);
  const bg = el("rect", { class: "bg", y: -ph / 2, height: ph, rx: ph / 2 }, tg);
  const t = el("text", { x: 0, y: 0, style: `font-size:${TV_TAG_PX}px` }, tg);
  const label = text.length > 22 ? text.slice(0, 21) + "…" : text;
  const s1 = el("tspan", { class: "s" }, t); s1.textContent = label;
  if (watts) { const s2 = el("tspan", { class: "w" }, t); s2.textContent = ` · ${watts}`; }
  let w = 0;
  try { w = t.getComputedTextLength(); } catch {}
  if (!w) w = (label.length + (watts ? watts.length + 3 : 0)) * TV_TAG_PX * 0.56;
  const pw = w + ph * 0.8;
  bg.setAttribute("x", -pw / 2); bg.setAttribute("width", pw);
  // In front of the screen (where its light falls), so a TV on a wall labels its own room.
  const b = furBox(f), vb = st.viewBox || [-1e3, -1e3, 2e3, 2e3], gap = 0.05 * k, H = ph * s, W = pw * s;
  const t0 = (f.rot || 0) * Math.PI / 180, ux = -Math.sin(t0), uy = Math.cos(t0);
  let x = f.x, y = f.y;
  if (Math.abs(uy) >= Math.abs(ux)) y += Math.sign(uy) * (b.h / 2 + gap + H / 2);
  else x += Math.sign(ux) * (b.w / 2 + gap + W / 2);
  x = Math.min(vb[0] + vb[2] - W / 2, Math.max(vb[0] + W / 2, x));
  y = Math.min(vb[1] + vb[3] - H / 2, Math.max(vb[1] + H / 2, y));
  tg.setAttribute("transform", `translate(${+x.toFixed(4)} ${+y.toFixed(4)}) scale(${+s.toFixed(6)})`);
}
svg.addEventListener("click", (e) => {
  if (st.editing) return;
  const t = e.target.closest?.("[data-tv]"); if (!t || e.target.closest(".marker")) return;
  const f = (st.layout.furniture || []).find((x) => x.id === t.dataset.tv); if (!f) return;
  if (st.devices.has(f.media)) openSheet(f.media); else setStatus("This TV isn't in Home Assistant right now", true);
});

// The light spill fades with distance from the screen (gradient in the piece's own, rotated, frame).
(() => {
  const defs = svg.querySelector("defs"); if (!defs || $("tvSpill")) return;
  const g = el("linearGradient", { id: "tvSpill", x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
  el("stop", { offset: 0, class: "tv-spill-a" }, g); el("stop", { offset: 1, class: "tv-spill-b" }, g);
})();

// ---------- room view / wall mode ----------
function tvsIn(r) {
  const ids = new Set(st.layout.placements.filter((p) => inRoom(r, p)).map((p) => p.entity_id));
  for (const f of st.layout.furniture || []) if (tvLinked(f) && inRoom(r, f)) ids.add(f.media);
  return [...ids].map((e) => st.devices.get(e)).filter((d) => d?.kind === "media" && !d.hidden);
}
// roomview.js: media players linked to TV furniture in the room count as in the room; a "TV · Netflix" chip.
function tvRoomIds(r) { return (st.layout.furniture || []).filter((f) => tvLinked(f) && inRoom(r, f)).map((f) => f.media); }
function tvRoomFacts(r) {
  return tvsIn(r).filter(tvIsOn).map((d) => {
    const c = tvMk("span", "rf tv-on"); c.dataset.fact = "tv";
    c.appendChild(tvSvg(`<use href="#ic-${mediaIcon(d)}"/>`)); c.append(tvShort(d) === "On" ? `${d.name} on` : `${d.name} · ${tvShort(d)}`);
    return c;
  });
}
// wall.js paint(): a "TV on" chip in the stats bar while any TV is on.
function tvWallPaint() {
  const bar = document.querySelector(".wall-stats"); if (!bar) return;
  let chip = $("wallTv");
  if (!chip) {
    chip = tvMk("span", "wall-stat tv"); chip.id = "wallTv"; chip.title = "TV on — tap for its controls";
    chip.appendChild(tvSvg('<use href="#ic-media"/>')); chip.appendChild(tvMk("b"));
    chip.onclick = () => { const d = [...st.devices.values()].find((x) => tvIsOn(x) && !x.hidden); if (d) openSheet(d.entity_id); };
    bar.appendChild(chip);
  }
  const on = [...st.devices.values()].filter((d) => tvIsOn(d) && !d.hidden && d.media_type !== "speaker");
  chip.hidden = !on.length;
  chip.querySelector("b").textContent = on.length === 1 ? (tvShort(on[0]) === "On" ? "TV on" : `TV · ${tvShort(on[0])}`) : `${on.length} TVs on`;
}

// ---------- edit mode: Link TV ----------
document.body.insertAdjacentHTML("beforeend", `
<dialog id="mediaDialog">
  <form method="dialog" id="mediaForm">
    <h3 id="mediaDialogTitle">Link TV</h3>
    <p class="hint">The TV on the plan lights up while it's on and opens its controls when tapped.</p>
    <div id="mediaList" class="plug-list" role="listbox"></div>
    <menu><button value="cancel" formnovalidate>Cancel</button></menu>
  </form>
</dialog>`);
(() => {
  const btn = tvMk("button"); btn.id = "furMedia"; btn.type = "button"; btn.hidden = true; btn.title = "Show a TV from Home Assistant on this piece";
  btn.innerHTML = `<svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor" style="vertical-align:-2px" aria-hidden="true"><use href="#ic-media"/></svg> <span>Link TV</span>`;
  ($("furLink") || $("furEdit") || $("deleteSel")).after(btn);
  btn.onclick = mediaLinkDialog;
})();
function mediaLinkDialog() {
  const f = typeof furSel === "function" ? furSel() : null; if (!f || f.type !== "tv") return;
  const dlg = $("mediaDialog"), list = $("mediaList"); list.replaceChildren();
  $("mediaDialogTitle").textContent = `Link ${f.label || "TV"} to a media player`;
  const others = new Map((st.draft.furniture || []).filter((o) => o.media && o !== f).map((o) => [o.media, o]));
  const players = [...st.devices.values()].filter((d) => d.kind === "media")
    .sort((a, b) => (a.media_type === "tv" ? 0 : 1) - (b.media_type === "tv" ? 0 : 1) || a.name.localeCompare(b.name));
  if (f.media && !st.devices.has(f.media)) players.push({ entity_id: f.media, name: f.media, kind: "media", state: "unavailable" });
  const opt = (eid, name, sub, cur, kind) => {
    const b = tvMk("button", "plug-opt" + (cur ? " cur" : "")); b.type = "button"; b.dataset.media = eid; b.setAttribute("role", "option"); b.setAttribute("aria-selected", cur);
    const ic = document.createElementNS(NS, "svg"); ic.setAttribute("viewBox", "0 0 24 24"); el("use", { href: `#ic-${kind}` }, ic); ic.setAttribute("class", "pi");
    const txt = tvMk("span", "pt"); txt.append(tvMk("span", "name", name)); if (sub) txt.append(tvMk("span", "sub", sub));
    b.append(ic, txt, tvMk("span", "ck", cur ? "✓" : "")); list.append(b); return b;
  };
  opt("", "None", "Not linked", !f.media, "media");
  for (const d of players) {
    const o = others.get(d.entity_id);
    opt(d.entity_id, d.name, [tvValue(d) || d.state, d.model, o ? "linked to another TV — moves here" : ""].filter(Boolean).join(" · "), f.media === d.entity_id, mediaIcon(d))
      .classList.toggle("taken", !!o);
  }
  if (!players.length) list.append(tvMk("p", "hint", "No TVs or media players in Home Assistant yet — see “TV / media players” in the README, then ⋯ → Refresh devices."));
  list.onclick = (e) => {
    const b = e.target.closest(".plug-opt"); if (!b) return;
    const eid = b.dataset.media; dlg.close("ok");
    if (!eid) { if (f.media) { f.media = null; setStatus("TV unlinked — Save to keep it"); } render(); return; }
    if (eid === f.media) return;
    const o = others.get(eid); if (o) o.media = null;
    f.media = eid; render();
    setStatus(`TV linked to ${st.devices.get(eid)?.name || eid} — Save to keep it`);
  };
  dlg.returnValue = ""; dlg.showModal();
}
