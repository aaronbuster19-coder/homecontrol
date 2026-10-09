"use strict";
// Quiet hours + mute for automation pushes, as a section of the bell sheet (door alerts always come through).
(() => {
  const el = (id) => document.getElementById(id);
  const sec = document.createElement("section");
  sec.className = "auto-sec"; sec.id = "quietSec";
  sec.innerHTML = `<h4>Quiet hours</h4>
    <label class="check"><input type="checkbox" id="quietOn"> Hold automation pushes at night</label>
    <div class="auto-sub" id="quietOpts">
      <label class="quiet-times">From <input type="time" id="quietFrom" step="60"> to <input type="time" id="quietTo" step="60"></label>
    </div>
    <p class="hint">Battery, offline, window, appliance and weekly-summary pushes wait and arrive as one “While you were asleep” push
      when quiet hours end. Door alerts and the test always come through.</p>
    <div class="row alert-dev"><button type="button" id="mute1h">Mute 1 h</button><button type="button" id="muteMorning">Mute until morning</button></div>
    <p class="hint quiet-state" id="quietState"><span id="quietText"></span> <button type="button" id="muteCancel" class="linkish" hidden>Cancel mute</button></p>`;
  el("alertTest").before(sec);
  const msg = (text, warn = false) => { el("alertMsg").textContent = text; el("alertMsg").classList.toggle("warn", warn); };
  const hm = (ms) => new Date(ms).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const day = (ms) => {
    const d = new Date(ms), now = new Date();
    return d.toDateString() === now.toDateString() ? "" : ` ${d.toLocaleDateString("en-GB", { weekday: "short" })}`;
  };

  function show(q) {
    const parts = [];
    if (q.muted) parts.push(`Muted until${day(q.mute_until)} ${hm(q.mute_until)}.`);
    else if (q.quiet) parts.push(`Quiet hours now, until ${hm(q.until)}.`);
    if (q.held) parts.push(`${q.held} push${q.held === 1 ? "" : "es"} waiting.`);
    el("quietText").textContent = parts.join(" ");
    el("muteCancel").hidden = !q.muted;
    el("quietState").hidden = !parts.length;
  }
  async function refresh() {
    try {
      const [s, q] = await Promise.all([api("/api/alerts/settings"), api("/api/alerts/quiet")]);
      el("quietOn").checked = s.quiet_hours; el("quietFrom").value = s.quiet_from; el("quietTo").value = s.quiet_to;
      el("quietOpts").classList.toggle("off", !s.quiet_hours);
      show(q);
    } catch (e) { msg(`Quiet hours: ${e.message}`, true); }
  }
  async function save() {
    const from = el("quietFrom").value, to = el("quietTo").value;
    el("quietOpts").classList.toggle("off", !el("quietOn").checked);
    if (!/^\d\d:\d\d$/.test(from) || !/^\d\d:\d\d$/.test(to)) return msg("Pick both quiet-hours times.", true);
    try {
      await api("/api/alerts/settings", { method: "PUT", body: JSON.stringify({ quiet_hours: el("quietOn").checked, quiet_from: from, quiet_to: to }) });
      show(await api("/api/alerts/quiet"));
      msg("Saved.");
    } catch (e) { msg(`Couldn't save: ${e.message}`, true); }
  }
  async function mute(kind) {
    try {
      const q = await api("/api/alerts/mute", { method: "POST", body: JSON.stringify({ for: kind }) });
      show(q);
      msg(kind === "off" ? "Mute cancelled." : "Muted — door alerts still come through.");
    } catch (e) { msg(`Couldn't mute: ${e.message}`, true); }
  }
  ["quietOn", "quietFrom", "quietTo"].forEach((id) => el(id).addEventListener("change", save));
  el("mute1h").onclick = () => mute("1h");
  el("muteMorning").onclick = () => mute("morning");
  el("muteCancel").onclick = () => mute("off");
  el("alertsBtn").addEventListener("click", refresh);
})();
