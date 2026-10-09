"use strict";
// Wall tablet mode: full-screen plan with a clock bar, wake lock and a dim screen when idle / at night.
// Entered from ⋯ → Wall mode or by opening /?wall. Uses app.js (st, render, totalPower, fmtW, closeSheet),
// controls.js (allOff) and modes.js (modeSt, modeDialog).
const WALL = (() => {
  const SET_KEY = "hc.wall.settings", ON_KEY = "hc.wall.on";
  const DEFAULTS = { start: "23:00", end: "07:00", idle: 120, nightIdle: 30 };
  const HOLD_MS = 1000, RELOAD_AFTER = 24 * 3600e3, SHIFT_PX = 36;
  const started = Date.now();
  const w = { on: false, dimmed: false, last: Date.now(), lock: null, wokeAt: 0, minute: -1, popTimer: null, toastTimer: null };

  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch {} },
  };
  function settings() {
    let s = {};
    try { s = JSON.parse(store.get(SET_KEY) || "{}") || {}; } catch {}
    const okTime = (t) => typeof t === "string" && /^\d\d:\d\d$/.test(t);
    const num = (v, d) => (Number.isFinite(+v) && +v >= 0 ? +v : d);
    return { start: okTime(s.start) ? s.start : DEFAULTS.start, end: okTime(s.end) ? s.end : DEFAULTS.end,
      idle: num(s.idle, DEFAULTS.idle), nightIdle: Math.max(5, num(s.nightIdle, DEFAULTS.nightIdle)) };
  }
  const mins = (t) => +t.slice(0, 2) * 60 + +t.slice(3, 5);
  function isNight(now = new Date()) {
    const s = settings(), a = mins(s.start), b = mins(s.end), m = now.getHours() * 60 + now.getMinutes();
    if (a === b) return false;
    return a < b ? m >= a && m < b : m >= a || m < b;
  }

  // ---------- DOM ----------
  const h = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstChild; };
  const BOLT = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M13 2 4 14h7l-1 8 9-12h-7z" fill="currentColor"/></svg>`;
  const THERMO = `<svg viewBox="0 0 24 24" aria-hidden="true" fill="currentColor"><use href="#ic-valve"/></svg>`;
  const DROP = `<svg viewBox="0 0 24 24" aria-hidden="true" fill="currentColor"><use href="#ic-dehumidifier"/></svg>`;
  const bar = h(`<div id="wallBar" class="wall-bar" hidden>
    <button id="wallClock" class="wall-clock" type="button" aria-label="Clock (hold to exit wall mode)">
      <span class="t" id="wallTime">--:--</span><span class="d" id="wallDate"></span></button>
    <div class="wall-stats">
      <span class="wall-stat power" id="wallPower" title="Total power now">${BOLT}<b>–</b></span>
      <span class="wall-stat temp" id="wallTemp" title="Average indoor temperature (radiator valves)">${THERMO}<b>–</b></span>
      <span class="wall-stat hum" id="wallHum" title="Indoor humidity (dehumidifier)" hidden>${DROP}<b>–</b></span>
      <span class="wall-conn" id="wallConn" hidden></span>
    </div>
    <div class="wall-acts">
      <button id="wallAway" class="wall-mode" type="button"><span class="dot"></span><span class="st">Home</span><span class="act">Away…</span></button>
      <button id="wallAllOff" class="wall-off" type="button"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M12 3v8M6.3 6.3a8 8 0 1 0 11.4 0"/></svg><span>All off</span></button>
    </div>
    <div id="wallPop" class="wall-pop" hidden>
      <button id="wallExit" type="button">Exit wall mode</button>
      <button id="wallSettings" type="button">Wall settings…</button>
    </div>
    <div id="wallToast" class="wall-toast" hidden></div>
  </div>`);
  const dim = h(`<div id="wallDim" class="wall-dim" hidden>
    <div class="wall-dim-in" id="wallDimIn">
      <div class="t" id="wallDimTime">--:--</div><div class="d" id="wallDimDate"></div>
      <div class="p" id="wallDimInfo"></div><div class="x" id="wallDimConn"></div>
    </div></div>`);
  const dlg = h(`<dialog id="wallDialog"><form method="dialog" id="wallForm">
    <h3>Wall settings</h3>
    <p class="hint">Saved on this device only.</p>
    <div class="row"><label>Dim at night from <input name="start" type="time" required></label>
      <label>until <input name="end" type="time" required></label></div>
    <label>Dim after idle (seconds, 0 = never) <input name="idle" type="number" min="0" max="86400" step="1" inputmode="numeric" required></label>
    <label>At night, dim again after (seconds) <input name="nightIdle" type="number" min="5" max="3600" step="1" inputmode="numeric" required></label>
    <menu><button value="cancel" formnovalidate>Cancel</button><button value="ok" class="primary">Save</button></menu>
  </form></dialog>`);
  document.body.prepend(bar);
  document.body.append(dim, dlg);
  const menuItem = document.getElementById("wallBtn");

  // ---------- bar contents ----------
  const pad = (n) => String(n).padStart(2, "0");
  const hhmm = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const dateText = (d) => d.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" });
  function avgTemp() {
    const ts = [...st.devices.values()].filter((d) => d.kind === "valve" && typeof d.current_temperature === "number")
      .map((d) => d.current_temperature);
    return ts.length ? ts.reduce((a, b) => a + b, 0) / ts.length : null;
  }
  function conn() {
    if (offline || !navigator.onLine) return ["offline", "Offline"];
    if (!st.live) return ["wait", "Reconnecting…"];
    if (st.ws === false) return ["wait", "HA reconnecting"];
    return null;
  }
  function paint() {
    const now = new Date(), t = hhmm(now), dt = dateText(now);
    const pw = totalPower(), tp = avgTemp(), c = conn();
    $("wallTime").textContent = t; $("wallDate").textContent = dt;
    $("wallPower").querySelector("b").textContent = pw == null ? "–" : fmtW(pw);
    $("wallPower").hidden = pw == null;
    $("wallTemp").querySelector("b").textContent = tp == null ? "–" : `${(Math.round(tp * 10) / 10).toFixed(1)}°`;
    $("wallTemp").hidden = tp == null;
    const hu = typeof indoorHumidity === "function" ? indoorHumidity() : null;
    $("wallHum").querySelector("b").textContent = hu == null ? "–" : `${Math.round(hu)} %`;
    $("wallHum").hidden = hu == null;
    const cn = $("wallConn"); cn.hidden = !c; if (c) { cn.textContent = c[1]; cn.className = `wall-conn ${c[0]}`; }
    const away = modeSt.mode === "away", mb = $("wallAway");
    mb.classList.toggle("away", away);
    mb.querySelector(".st").textContent = away ? "Away" : "Home";
    mb.querySelector(".act").textContent = away ? "I'm home" : "Away…";
    mb.setAttribute("aria-label", away ? "Away — tap if you're home" : "Home — tap to go away");
    if (w.dimmed) {
      $("wallDimTime").textContent = t; $("wallDimDate").textContent = dt;
      const open = [...st.devices.values()].filter((d) => d.kind === "sensor" && d.state === "on").map((d) => d.name);
      const info = $("wallDimInfo"); info.replaceChildren();
      const part = (icon, text) => { const p = document.createElement("span"); p.innerHTML = icon; p.append(text); info.append(p); };
      if (pw != null) part(BOLT, fmtW(pw));
      if (tp != null) part(THERMO, `${tp.toFixed(1)}°`);
      if (hu != null) part(DROP, `${Math.round(hu)} %`);
      if (away) part("", "Away");
      $("wallDimConn").textContent = [open.length ? `${open.join(", ")} open` : "", c ? c[1] : ""].filter(Boolean).join(" · ");
      if (now.getMinutes() !== w.minute) { w.minute = now.getMinutes(); shift(); }
    }
  }
  // Move the dim clock a little every minute so nothing burns in.
  function shift() {
    const r = () => Math.round((Math.random() * 2 - 1) * SHIFT_PX);
    $("wallDimIn").style.transform = `translate(${r()}px, ${r()}px)`;
  }
  function toast(msg, ms = 4000) {
    const t = $("wallToast"); t.textContent = msg; t.hidden = false;
    clearTimeout(w.toastTimer); w.toastTimer = setTimeout(() => { t.hidden = true; }, ms);
  }
  // app.js rewrites the (hidden) status line on every device update: repaint then, and surface its errors as a toast.
  new MutationObserver(() => {
    if (!w.on) return;
    const s = $("status"); paint();
    if (s.classList.contains("err") && s.textContent) toast(s.textContent);
  })
    .observe($("status"), { childList: true, characterData: true, subtree: true, attributes: true, attributeFilter: ["class"] });

  // ---------- wake lock ----------
  async function lock() {
    if (!w.on || w.lock || document.hidden || !navigator.wakeLock) return;
    try { w.lock = await navigator.wakeLock.request("screen"); w.lock.addEventListener?.("release", () => { w.lock = null; }); }
    catch { w.lock = null; }
  }
  function unlock() { try { w.lock?.release(); } catch {} w.lock = null; }
  document.addEventListener("visibilitychange", () => { if (!document.hidden && w.on) { w.last = Date.now(); lock(); } });

  // ---------- dimming ----------
  function setDim(on) {
    if (on === w.dimmed) return;
    w.dimmed = on;
    if (on) {
      // Nothing may stay open on top of (or under) the dim screen.
      closeSheet(); hidePop();
      document.querySelectorAll("dialog[open]").forEach((d) => d.close());
      $("moreMenu").hidden = true;
      w.minute = -1;
      dim.hidden = false; requestAnimationFrame(() => dim.classList.add("show"));
      paint();
      // Quietly pick up new deploys once a day, while nobody is looking.
      if (Date.now() - started > RELOAD_AFTER && navigator.onLine && !offline) location.reload();
    } else {
      dim.classList.remove("show"); dim.hidden = true;
      w.last = Date.now();
    }
  }
  function wake() { w.wokeAt = Date.now(); setDim(false); }
  // The touch that wakes the screen must not reach the plan: swallow the whole gesture on the overlay.
  for (const ev of ["pointerdown", "pointermove", "touchstart", "mousedown", "contextmenu"]) dim.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); });
  dim.addEventListener("pointerup", (e) => { e.preventDefault(); e.stopPropagation(); wake(); });
  dim.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); wake(); });
  document.addEventListener("click", (e) => {
    if (w.wokeAt && Date.now() - w.wokeAt < 600) { e.preventDefault(); e.stopPropagation(); }
  }, true);
  const activity = () => { w.last = Date.now(); };
  for (const ev of ["pointerdown", "wheel", "touchstart"]) document.addEventListener(ev, activity, { capture: true, passive: true });

  function tick() {
    if (!w.on) return;
    const s = settings(), idle = (Date.now() - w.last) / 1000;
    const limit = isNight() ? s.nightIdle : s.idle;
    if (!w.dimmed && limit > 0 && idle >= limit) setDim(true);
    paint();
  }

  // ---------- exit popover ----------
  function hidePop() { $("wallPop").hidden = true; clearTimeout(w.popTimer); }
  function showPop() {
    $("wallPop").hidden = false; navigator.vibrate?.(15);
    clearTimeout(w.popTimer); w.popTimer = setTimeout(hidePop, 8000);
  }
  let hold = null;
  const clock = $("wallClock");
  clock.addEventListener("pointerdown", (e) => {
    if (e.button > 0) return;
    clearTimeout(hold?.t); hold = { x: e.clientX, y: e.clientY, t: setTimeout(() => { hold = null; showPop(); }, HOLD_MS) };
  });
  clock.addEventListener("pointermove", (e) => { if (hold && Math.hypot(e.clientX - hold.x, e.clientY - hold.y) > 12) { clearTimeout(hold.t); hold = null; } });
  for (const ev of ["pointerup", "pointercancel", "pointerleave"]) clock.addEventListener(ev, () => { clearTimeout(hold?.t); hold = null; });
  clock.addEventListener("contextmenu", (e) => e.preventDefault());
  clock.addEventListener("click", () => { if ($("wallPop").hidden) toast("Hold the clock for a second to exit", 2500); });
  document.addEventListener("pointerdown", (e) => { if (!$("wallPop").hidden && !e.target.closest("#wallPop, #wallClock")) hidePop(); });
  $("wallExit").onclick = () => exit();
  $("wallSettings").onclick = () => { hidePop(); openSettings(); };

  function openSettings() {
    const f = $("wallForm"), s = settings();
    f.start.value = s.start; f.end.value = s.end; f.idle.value = s.idle; f.nightIdle.value = s.nightIdle;
    dlg.onclose = () => {
      if (dlg.returnValue !== "ok") return;
      const v = { start: f.start.value, end: f.end.value, idle: Math.round(+f.idle.value), nightIdle: Math.max(5, Math.round(+f.nightIdle.value)) };
      store.set(SET_KEY, JSON.stringify(v));
      w.last = Date.now();
      toast(v.start === v.end ? "Saved — no night dimming" : `Saved — dims ${v.start}–${v.end}`);
    };
    dlg.returnValue = ""; dlg.showModal();
  }

  // ---------- enter / exit ----------
  function setUrl(on) {
    const rest = location.search.slice(1).split("&").filter((p) => p && p !== "wall" && !p.startsWith("wall="));
    if (on) rest.push("wall");
    history.replaceState(history.state, "", location.pathname + (rest.length ? `?${rest.join("&")}` : "") + location.hash);
  }
  function enter(gesture) {
    if (st.editing) setEditing(false);
    if (!w.on) {
      w.on = true; w.last = Date.now();
      document.body.classList.add("wall"); bar.hidden = false;
      st.markerScale = 1.5;
      if (!new URLSearchParams(location.search).has("wall")) setUrl(true);
      store.set(ON_KEY, "1");
      closeSheet(); render(); paint();
      w.timer = setInterval(tick, 1000);
      if (gesture) toast("Hold the clock to exit", 3500);
    }
    if (gesture && !document.fullscreenElement) {
      try { document.documentElement.requestFullscreen?.({ navigationUI: "hide" })?.catch(() => {}); } catch {}
    }
    lock();
  }
  function exit() {
    if (!w.on) return;
    w.on = false; clearInterval(w.timer);
    setDim(false); hidePop(); unlock();
    document.body.classList.remove("wall"); bar.hidden = true;
    st.markerScale = 1;
    setUrl(false); store.set(ON_KEY, null);
    if (document.fullscreenElement) document.exitFullscreen?.().catch(() => {});
    render();
  }
  // Escape exits on desktop, unless it is closing something else first (captured before app.js closes the sheet).
  window.addEventListener("keydown", (e) => {
    if (!w.on) return;
    if (w.dimmed) { e.preventDefault(); e.stopPropagation(); wake(); return; }
    activity();
    if (e.key !== "Escape") return;
    if (!$("wallPop").hidden) { hidePop(); return; }
    if (st.sheetFor || !$("alertSheet").hidden || document.querySelector("dialog[open]") || !$("moreMenu").hidden) return;
    exit();
  }, true);

  $("wallAway").onclick = () => modeDialog();
  $("wallAllOff").onclick = () => allOff();
  if (menuItem) menuItem.onclick = () => enter(true);
  if (new URLSearchParams(location.search).has("wall")) enter(false);
  return { enter, exit, isNight, settings, get on() { return w.on; }, get dimmed() { return w.dimmed; } };
})();
