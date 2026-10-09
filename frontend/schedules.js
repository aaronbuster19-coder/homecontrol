"use strict";
// Schedules (⋯ → Schedules): list with per-schedule on/off and a master switch, editor sheet. Run by the server.
(() => {
  const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const PRESETS = { "Every day": [0, 1, 2, 3, 4, 5, 6], Weekdays: [0, 1, 2, 3, 4], Weekends: [5, 6] };
  const KINDS = { on: ["light", "plug"], off: ["light", "plug"], brightness: ["light"], temperature: ["valve"] };
  const KIND_NAME = { light: "Lights", plug: "Plugs", valve: "Radiators" };
  const node = (tag, props = {}, ...kids) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (k === "class") e.className = v; else if (k in e && k !== "list") e[k] = v; else e.setAttribute(k, v);
    }
    for (const k of kids) if (k != null) e.append(k);
    return e;
  };
  const sheet = (id, title) => {
    const body = node("div", { class: "sheet-body" }, node("button", { class: "close", type: "button", "aria-label": "Close", onclick: () => { s.hidden = true; } }),
      node("h3", {}, title));
    const s = node("div", { id, class: "sheet sched-sheet", hidden: true }, body);
    s.addEventListener("click", (e) => { if (e.target === s) s.hidden = true; });
    document.body.appendChild(s);
    return { s, body };
  };
  let data = { enabled: true, schedules: [], tz: undefined };

  // ---------- formatting ----------
  const fmt = (ms, opts) => new Intl.DateTimeFormat("en-GB", { timeZone: data.tz, ...opts }).format(new Date(ms));
  const when = (ms) => fmt(ms, { weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).replace(",", "");
  function daysText(days) {
    for (const [name, d] of Object.entries(PRESETS)) if (d.length === days.length && d.every((x) => days.includes(x))) return name;
    return days.map((d) => DAYS[d]).join(" ");
  }
  function timeText(t) {
    if (t.type === "fixed") return t.at;
    const o = t.offset;
    return `${t.type === "sunrise" ? "Sunrise" : "Sunset"}${o ? ` ${o > 0 ? "+" : "−"}${Math.abs(o)} min` : ""}`;
  }
  const devName = (e) => st.devices.get(e)?.name || e;
  function targetText(s) {
    if (s.target.room) return `${st.layout.rooms.find((r) => r.id === s.target.room)?.name || "Room"} lights`;
    const n = s.target.entity_ids.map(devName);
    return n.length <= 2 ? n.join(", ") : `${n.slice(0, 2).join(", ")} +${n.length - 2}`;
  }
  function actionText(a) {
    return { on: "on", off: "off", brightness: `${a.value} %`, temperature: `${a.value}°` }[a.type];
  }

  // ---------- list ----------
  const L = sheet("schedSheet", "Schedules");
  const master = node("input", { type: "checkbox", id: "schedMaster" });
  const msg = node("p", { id: "schedMsg", class: "hint" });
  const list = node("ul", { id: "schedList", class: "sched-list" });
  const addBtn = node("button", { id: "schedAdd", class: "primary", type: "button" }, "+ Add schedule");
  const lat = node("input", { type: "number", id: "schedLat", step: "0.0001", min: "-90", max: "90", inputMode: "decimal" });
  const lon = node("input", { type: "number", id: "schedLon", step: "0.0001", min: "-180", max: "180", inputMode: "decimal" });
  L.body.append(
    node("div", { class: "sub" }, "Run by homecontrol on the server, also with the app closed. Missed times are never caught up."),
    node("label", { class: "sched-master" }, master, node("b", {}, "Schedules on")),
    msg, list, addBtn,
    node("details", { class: "sched-loc" }, node("summary", {}, "Location for sunrise / sunset"),
      node("div", { class: "sched-row" }, node("label", {}, "Latitude ", lat), node("label", {}, "Longitude ", lon),
        node("button", { type: "button", id: "schedLocSave", onclick: saveLoc }, "Save"))));
  const say = (t, warn = false) => { msg.textContent = t; msg.classList.toggle("warn", warn); };

  function render() {
    master.checked = data.enabled;
    list.classList.toggle("off", !data.enabled);
    lat.value = data.lat; lon.value = data.lon;
    list.replaceChildren();
    if (!data.schedules.length) list.append(node("li", { class: "hint sched-empty" }, "No schedules yet."));
    for (const s of data.schedules) {
      const sw = node("input", { type: "checkbox", checked: s.enabled, "aria-label": `${s.name} on` });
      sw.onchange = () => save({ ...pick(s), enabled: sw.checked }, s.id, true);
      const status = [];
      if (s.enabled && data.enabled && s.next) status.push(`Next: ${when(s.next)}`);
      else status.push(!data.enabled ? "Schedules are off" : s.enabled ? "No upcoming time" : "Off");
      if (s.last) status.push(`Last ran: ${when(s.last.at)} — ${s.last.status}`);
      const main = node("button", { type: "button", class: "sched-main", onclick: () => edit(s) },
        node("b", {}, s.name),
        node("span", {}, `${daysText(s.days)} · ${timeText(s.time)} · ${targetText(s)} ${actionText(s.action)}`),
        node("span", { class: "sched-status" }, status.join(" · ")));
      list.append(node("li", { class: `sched-item${s.enabled ? "" : " disabled"}`, "data-id": s.id }, main,
        node("label", { class: "sched-switch" }, sw)));
    }
  }
  async function load() {
    try { data = await api("/api/schedules"); render(); } catch (e) { say(`Schedules: ${e.message}`, true); }
  }
  async function open() { say(""); L.s.hidden = false; await load(); }
  master.onchange = async () => {
    try { data = await api("/api/schedules/settings", { method: "PUT", body: JSON.stringify({ enabled: master.checked }) }); render(); say(master.checked ? "Schedules on." : "Schedules off — nothing runs."); }
    catch (e) { say(`Couldn't save: ${e.message}`, true); load(); }
  };
  async function saveLoc() {
    const la = Number(lat.value), lo = Number(lon.value);
    if (!(lat.value !== "" && la >= -90 && la <= 90 && lon.value !== "" && lo >= -180 && lo <= 180)) return say("Latitude −90…90, longitude −180…180.", true);
    try { data = await api("/api/schedules/settings", { method: "PUT", body: JSON.stringify({ lat: la, lon: lo }) }); render(); say("Location saved."); }
    catch (e) { say(`Couldn't save: ${e.message}`, true); }
  }
  const pick = (s) => ({ name: s.name, enabled: s.enabled, days: s.days, time: s.time, action: s.action, target: s.target });
  async function save(body, id, quiet = false) {
    try {
      await api(id ? `/api/schedules/${id}` : "/api/schedules", { method: id ? "PUT" : "POST", body: JSON.stringify(body) });
      await load();
      if (!quiet) say(id ? "Schedule saved." : "Schedule added.");
      else say(`${body.name} ${body.enabled ? "on" : "off"}.`);
      return true;
    } catch (e) { say(`Couldn't save: ${e.message}`, true); if (quiet) load(); return e.message; }
  }

  // ---------- editor ----------
  const E = sheet("schedEdit", "Schedule");
  const f = node("form", { id: "schedForm", novalidate: true });
  const name = node("input", { name: "name", maxLength: 60, required: true, autocomplete: "off", placeholder: "e.g. Wake up" });
  const action = node("select", { name: "action" },
    ...[["on", "Turn on"], ["off", "Turn off"], ["brightness", "Brightness %"], ["temperature", "Set temperature"]].map(([v, t]) => node("option", { value: v }, t)));
  const value = node("input", { name: "value", type: "number", inputMode: "decimal" });
  const valueUnit = node("span", { class: "sched-unit" });
  const tDevices = node("input", { type: "radio", name: "ttype", value: "devices" });
  const tRoom = node("input", { type: "radio", name: "ttype", value: "room" });
  const roomLabel = node("label", { class: "check" }, tRoom, " All lights in ");
  const room = node("select", { name: "room" });
  roomLabel.append(room);
  const devBox = node("div", { class: "sched-devs", id: "schedDevs" });
  const dayBox = node("div", { class: "sched-days" });
  const presetBox = node("div", { class: "sched-presets" });
  const dayBtns = DAYS.map((d, i) => node("button", { type: "button", class: "chip", "data-day": i, "aria-pressed": "false",
    onclick: (e) => { const b = e.currentTarget; b.setAttribute("aria-pressed", b.getAttribute("aria-pressed") === "true" ? "false" : "true"); } }, d));
  dayBox.append(...dayBtns);
  for (const [n, d] of Object.entries(PRESETS)) presetBox.append(node("button", { type: "button", class: "chip preset", onclick: () => setDays(d) }, n));
  const ttype = node("select", { name: "ttype_time" },
    ...[["fixed", "At a time"], ["sunrise", "Sunrise"], ["sunset", "Sunset"]].map(([v, t]) => node("option", { value: v }, t)));
  const at = node("input", { type: "time", name: "at", step: 60 });
  const offset = node("input", { type: "number", name: "offset", min: -180, max: 180, step: 1, inputMode: "numeric" });
  const offWrap = node("label", { class: "sched-off" }, offset, " min (− before, + after)");
  const emsg = node("p", { class: "hint", id: "schedEditMsg" });
  const del = node("button", { type: "button", id: "schedDelete", class: "danger" }, "Delete");
  const cancel = node("button", { type: "button", onclick: () => { E.s.hidden = true; } }, "Cancel");
  const ok = node("button", { type: "submit", class: "primary", id: "schedSave" }, "Save");
  const row = (label, ...kids) => node("div", { class: "sched-field" }, node("div", { class: "sched-lbl" }, label), ...kids);
  f.append(
    row("Name", name),
    row("Do", node("div", { class: "sched-row" }, action, node("span", { class: "sched-val" }, value, valueUnit))),
    row("Which", node("label", { class: "check" }, tDevices, " These devices"), devBox, roomLabel),
    row("Days", dayBox, presetBox),
    row("When", node("div", { class: "sched-row" }, ttype, at, offWrap)),
    emsg,
    node("menu", { class: "sched-actions" }, del, node("span", { class: "spacer" }), cancel, ok));
  E.body.append(f);
  let editing = null;

  const setDays = (d) => dayBtns.forEach((b, i) => b.setAttribute("aria-pressed", d.includes(i) ? "true" : "false"));
  const chosen = new Set();
  function renderDevs() {
    const kinds = KINDS[action.value];
    devBox.replaceChildren();
    for (const k of kinds) {
      const ds = [...st.devices.values()].filter((d) => d.kind === k).sort((a, b) => a.name.localeCompare(b.name));
      if (!ds.length) continue;
      devBox.append(node("div", { class: "sched-kind" }, KIND_NAME[k]));
      for (const d of ds) {
        const cb = node("input", { type: "checkbox", value: d.entity_id, checked: chosen.has(d.entity_id) });
        cb.onchange = () => { cb.checked ? chosen.add(d.entity_id) : chosen.delete(d.entity_id); tDevices.checked = true; };
        devBox.append(node("label", { class: "check" }, cb, ` ${d.name}`));
      }
    }
    if (!devBox.children.length) devBox.append(node("p", { class: "hint" }, "No matching devices in Home Assistant."));
  }
  function sync() {
    const a = action.value, valve = a === "temperature";
    value.hidden = valueUnit.hidden = a === "on" || a === "off";
    if (a === "brightness") { value.min = 1; value.max = 100; value.step = 1; valueUnit.textContent = "%"; if (!(+value.value >= 1 && +value.value <= 100)) value.value = 50; }
    if (valve) { value.min = 5; value.max = 35; value.step = 0.5; valueUnit.textContent = "°C"; if (!(+value.value >= 5 && +value.value <= 35)) value.value = 19; }
    roomLabel.hidden = valve || !st.layout.rooms.length;
    if (valve && tRoom.checked) tDevices.checked = true;
    const fixed = ttype.value === "fixed";
    at.hidden = !fixed; offWrap.hidden = fixed;
    renderDevs();
  }
  action.onchange = sync; ttype.onchange = sync;
  room.onchange = () => { tRoom.checked = true; };

  function edit(s) {
    editing = s || null;
    E.body.querySelector("h3").textContent = s ? "Edit schedule" : "New schedule";
    room.replaceChildren(...st.layout.rooms.map((r) => node("option", { value: r.id }, r.name)));
    name.value = s?.name || "";
    action.value = s?.action.type || "on";
    value.value = s?.action.value ?? "";
    chosen.clear(); (s?.target.entity_ids || []).forEach((e) => chosen.add(e));
    (s?.target.room ? tRoom : tDevices).checked = true;
    if (s?.target.room) room.value = s.target.room;
    setDays(s?.days || PRESETS["Every day"]);
    ttype.value = s?.time.type || "fixed";
    at.value = s?.time.at || "07:00";
    offset.value = s?.time.offset ?? 0;
    del.hidden = !s; emsg.textContent = "";
    sync();
    E.s.hidden = false;
    name.focus({ preventScroll: true });
  }
  const err = (t) => { emsg.textContent = t; emsg.classList.add("warn"); };
  f.onsubmit = async (e) => {
    e.preventDefault();
    const days = dayBtns.filter((b) => b.getAttribute("aria-pressed") === "true").map((b) => +b.dataset.day);
    if (!name.value.trim()) return err("Give it a name.");
    if (!days.length) return err("Pick at least one day.");
    const a = { type: action.value };
    if (a.type === "brightness" || a.type === "temperature") {
      a.value = Number(value.value);
      if (a.type === "brightness" && !(a.value >= 1 && a.value <= 100)) return err("Brightness must be 1–100 %.");
      if (a.type === "temperature" && !(a.value >= 5 && a.value <= 35)) return err("Temperature must be 5–35°.");
    }
    let target;
    if (tRoom.checked && !roomLabel.hidden) target = { room: room.value };
    else {
      const ok = new Set(KINDS[a.type]);
      const ids = [...chosen].filter((x) => ok.has(st.devices.get(x)?.kind) || (!st.devices.has(x) && editing?.target.entity_ids?.includes(x)));
      if (!ids.length) return err("Pick at least one device.");
      target = { entity_ids: ids };
    }
    let time;
    if (ttype.value === "fixed") {
      if (!/^\d\d:\d\d$/.test(at.value)) return err("Pick a time.");
      time = { type: "fixed", at: at.value };
    } else {
      const o = Math.round(Number(offset.value || 0));
      if (!(o >= -180 && o <= 180)) return err("Offset must be −180…180 min.");
      time = { type: ttype.value, offset: o };
    }
    ok.disabled = true;
    const r = await save({ name: name.value.trim(), enabled: editing ? editing.enabled : true, days, time, action: a, target }, editing?.id);
    ok.disabled = false;
    if (r === true) E.s.hidden = true; else err(`Couldn't save: ${r}`);
  };
  del.onclick = async () => {
    if (!editing || !confirm(`Delete “${editing.name}”?`)) return;
    try { await api(`/api/schedules/${editing.id}`, { method: "DELETE" }); E.s.hidden = true; await load(); say("Schedule deleted."); }
    catch (e) { err(`Couldn't delete: ${e.message}`); }
  };

  addBtn.onclick = () => edit(null);
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!E.s.hidden) E.s.hidden = true; else L.s.hidden = true;
  });
  $("schedulesBtn").onclick = open;
  // keep "Next" / "Last ran" fresh while the list is open
  setInterval(() => { if (!L.s.hidden && E.s.hidden && !document.hidden) load(); }, 30000);
})();
