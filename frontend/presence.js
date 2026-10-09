"use strict";
// Auto Away (⋯ → Auto Away…, or "Automatic…" in the Away dialog): who counts, the delay, active hours, live status,
// recent actions. Run by the server (backend/presence.py); its push toggle lives in the bell sheet.
(() => {
  const node = (tag, props = {}, ...kids) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (k === "class") e.className = v; else if (k in e) e[k] = v; else e.setAttribute(k, v);
    }
    for (const k of kids) if (k != null) e.append(k);
    return e;
  };
  const hm = (ms) => new Date(ms).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const dayHm = (ms) => {
    const d = new Date(ms), today = new Date(data?.now ?? Date.now()).toDateString() === d.toDateString();  // the server's today
    return today ? hm(ms) : `${d.toLocaleDateString("en-GB", { weekday: "short" })} ${hm(ms)}`;
  };
  const names = (ps) => ps.map((p) => p.name).join(ps.length === 2 ? " & " : ", ");
  let data = null, timer = null;

  // ---------- sheet ----------
  const s = node("div", { id: "presSheet", class: "sheet sched-sheet pres-sheet", hidden: true });
  const body = node("div", { class: "sheet-body" });
  const close = () => { s.hidden = true; clearInterval(timer); timer = null; };
  body.append(node("button", { class: "close", type: "button", "aria-label": "Close", onclick: close }), node("h3", {}, "Auto Away"),
    node("div", { class: "sub" }, "Switches to Away when everyone has left and back Home when someone arrives — exactly like the Away / I'm home button. Runs on the server."));
  const status = node("p", { id: "presStatus", class: "pres-status" });
  const msg = node("p", { id: "presMsg", class: "hint" });
  const on = node("input", { type: "checkbox", id: "presOn" });
  const people = node("div", { id: "presPeople", class: "pres-people" });
  const mins = node("input", { type: "number", id: "presMins", min: "2", max: "120", step: "1", inputMode: "numeric" });
  const home = node("input", { type: "checkbox", id: "presHome" });
  const win = node("input", { type: "checkbox", id: "presWin" });
  const from = node("input", { type: "time", id: "presFrom", step: "60" }), to = node("input", { type: "time", id: "presTo", step: "60" });
  const opts = node("div", { class: "pres-opts", id: "presOpts" },
    node("div", { class: "sched-field" }, node("div", { class: "sched-lbl" }, "Presence"), people),
    node("label", { class: "pres-line" }, "Go Away when everyone has left for ", mins, " min"),
    node("label", { class: "check pres-check" }, home, node("span", {}, "Come Home when anyone arrives")),
    node("label", { class: "check pres-check" }, win, node("span", {}, "Only between ", from, " and ", to)),
    node("p", { class: "hint" }, "Away or I'm home pressed by hand wins: no automatic Away for 30 min after it, and not again until someone has been home. Phones that are offline count as unchanged."));
  const logBox = node("div", { class: "sum-sec pres-log", id: "presLog" });
  body.append(status, node("label", { class: "sched-master" }, on, node("b", {}, "Automatic Away / Home")), msg, opts, logBox);
  s.appendChild(body);
  s.addEventListener("click", (e) => { if (e.target === s) close(); });
  document.body.appendChild(s);

  const say = (text, warn = false) => { msg.textContent = text; msg.classList.toggle("warn", warn); };

  function statusText(d) {
    if (!d.enabled) return "Off — Away and Home only when you press them.";
    const tracked = d.people.filter((p) => p.tracked);
    if (!tracked.length) return d.people.length ? "Auto: choose who counts below." : "Auto: no phones to follow yet.";
    if (d.retry_at && d.last?.status === "failed") return `Auto: Home Assistant didn't respond — trying again at ${hm(d.retry_at)}.`;
    if (d.arrival_at) return `Auto: ${names(tracked.filter((p) => p.known_home))} arrived — Home in a moment.`;
    if (d.everyone_out) {
      const since = `Auto: everyone out since ${dayHm(d.out_since)}`;
      if (d.mode === "away") return `${since} — Away.`;
      if (d.away_at) {
        if (d.blocked === "manual") return `${since} — set by hand, so not before ${hm(d.manual_until)}.`;
        if (d.blocked === "hours") return `${since} — Away when ${d.from}–${d.to} starts.`;
        const m = Math.ceil((d.away_at - d.now) / 60000);
        return m <= 0 ? `${since}, Away now.` : `${since}, Away in ${m} min.`;
      }
      return `${since} — you chose Home, so it stays Home until someone comes back.`;
    }
    const inside = tracked.filter((p) => p.known_home);
    return inside.length ? `Auto: ${names(inside)} ${inside.length === 1 ? "is" : "are"} home.` : "Auto: waiting for the phones' locations.";
  }
  function stateText(p) {
    if (p.state === "home") return "Home";
    if (p.state === "not_home") return "Away";
    if (p.state === "unavailable" || p.state === "unknown") return `${p.state} — counts as unchanged`;
    return `Away (${p.state})`;
  }

  function render(d) {
    data = d;
    status.textContent = statusText(d);
    on.checked = d.enabled; mins.value = d.away_minutes; home.checked = d.come_home;
    win.checked = d.only_between; from.value = d.from; to.value = d.to;
    opts.classList.toggle("off", !d.enabled);
    people.replaceChildren();
    if (!d.people.length) {
      people.append(node("p", { class: "hint warn", id: "presNone" },
        "No phones found in Home Assistant. Install the Home Assistant companion app on your phone, sign in, and allow location " +
        "(“Always”) — your phone then appears here as a person. Then ⋯ → Refresh devices."));
    }
    for (const p of d.people) {
      const cb = node("input", { type: "checkbox", checked: p.tracked });
      cb.dataset.person = p.entity_id;
      cb.onchange = () => {
        const ids = [...people.querySelectorAll("input[data-person]")].filter((x) => x.checked).map((x) => x.dataset.person);
        save({ people: ids.length === d.people.length ? null : ids });
      };
      people.append(node("label", { class: "check pres-person" }, cb, node("span", {}, p.name),
        node("span", { class: "pres-state" + (p.home ? " home" : "") }, stateText(p))));
    }
    logBox.replaceChildren(node("h4", {}, "Recent"));
    if (!d.log.length) logBox.append(node("div", { class: "hist-msg" }, "Nothing yet."));
    for (const x of d.log.slice(0, 8)) {
      logBox.append(node("div", { class: "sum-row" }, node("span", {}, x.text), node("span", {}, dayHm(x.at))));
    }
  }

  async function refresh() {
    try { render(await api("/api/presence")); } catch (e) { say(`Auto Away: ${e.message}`, true); }
  }
  async function save(body) {
    try { render(await api("/api/presence/settings", { method: "PUT", body: JSON.stringify(body) })); say("Saved."); }
    catch (e) { say(`Couldn't save: ${e.message}`, true); refresh(); }
  }
  on.onchange = () => save({ enabled: on.checked });
  home.onchange = () => save({ come_home: home.checked });
  win.onchange = () => save({ only_between: win.checked });
  mins.onchange = () => {
    const m = Math.round(Number(mins.value));
    if (!(m >= 2 && m <= 120)) { say("Minutes must be 2–120.", true); return; }
    save({ away_minutes: m });
  };
  const times = () => {
    if (!/^\d\d:\d\d$/.test(from.value) || !/^\d\d:\d\d$/.test(to.value)) { say("Pick both times.", true); return; }
    save({ from: from.value, to: to.value });
  };
  from.onchange = times; to.onchange = times;

  function open() {
    s.hidden = false; say("");
    refresh();
    clearInterval(timer);
    timer = setInterval(() => { if (!s.hidden && !document.hidden) refresh(); }, 15000);
  }
  window.openAutoAway = open;

  // ⋯ menu entry, right after Away…
  const item = node("button", { id: "autoAwayBtn", role: "menuitem" }, "Auto Away…");
  item.onclick = open;
  $("awayBtn").after(item);
  // "Automatic…" in the Away / Home dialog
  const auto = node("button", { type: "button", id: "modeAuto", class: "linkish" }, "Automatic…");
  auto.onclick = () => { $("modeDialog").close("auto"); open(); };
  $("modeList").after(auto);

  // ---------- bell sheet: the pushes ----------
  const sec = node("section", { class: "auto-sec", id: "presSec" });
  const notify = node("input", { type: "checkbox", id: "presNotify" });
  sec.append(node("h4", {}, "Auto Away"),
    node("label", { class: "check" }, notify, " Push when it switches (“Switched to Away — everyone left”, “Welcome home”)"),
    node("p", { class: "hint" }, "Held during quiet hours like the other automation pushes. Settings: ⋯ → Auto Away."));
  $("alertTest").before(sec);
  const bellMsg = (text, warn = false) => { $("alertMsg").textContent = text; $("alertMsg").classList.toggle("warn", warn); };
  notify.onchange = async () => {
    try { await api("/api/presence/settings", { method: "PUT", body: JSON.stringify({ notify: notify.checked }) }); bellMsg("Saved."); }
    catch (e) { bellMsg(`Couldn't save: ${e.message}`, true); }
  };
  $("alertsBtn").addEventListener("click", async () => {
    try { notify.checked = (await api("/api/presence")).notify; } catch {}
  });
})();
