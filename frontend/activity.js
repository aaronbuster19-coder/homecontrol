"use strict";
// Activity timeline (⋯ → Activity): one feed, newest first, a local day per GET /api/activity page, older days loaded
// on scroll (up to 7). Filters by room and type are applied by the server. A tap on an entry opens that device's sheet.
// Hook from app.js: renderSheet -> renderActivity(c) for st.sheetFor === ACTIVITY.
const ACTIVITY = "@activity";
const ACT_TYPES = [["", "All types"], ["doors", "Doors & windows"], ["lights", "Lights & plugs"], ["heating", "Heating"],
  ["appliances", "Appliances"], ["people", "People"], ["alerts", "Alerts"], ["security", "Security"]];
const ACT_FILL = 15; // with a filter, keep loading older days until this many entries show (or 7 days are in)
const act = { pages: [], next: undefined, loading: false, error: null, room: "", type: "", gen: 0, root: null, io: null };
const ax = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

// Icons in the plan's style: the marker symbols where there is one, else simple strokes in currentColor.
const ACT_STROKE = (d) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const ACT_USE = (id) => `<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><use href="#ic-${id}"/></svg>`;
const ACT_ICONS = {
  door: ACT_STROKE('<path d="M5 21V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v17M3 21h18"/><circle cx="14" cy="12" r="1" fill="currentColor"/>'),
  window: ACT_STROKE('<rect x="4" y="3" width="16" height="18" rx="1"/><path d="M12 3v18M4 12h16"/>'),
  light: ACT_USE("light"), plug: ACT_USE("plug"), valve: ACT_USE("valve"), dehumidifier: ACT_USE("dehumidifier"),
  media: ACT_STROKE('<rect x="3" y="5" width="18" height="12" rx="2"/><path d="M8 21h8"/>'),
  person: ACT_STROKE('<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>'),
  away: ACT_STROKE('<path d="M3 11 12 4l9 7"/><path d="M5 10v10h14V10"/>'),
  washer: ACT_STROKE('<rect x="4" y="3" width="16" height="18" rx="2"/><circle cx="12" cy="13" r="4.5"/><path d="M7 6.5h2"/>'),
  kettle: ACT_STROKE('<path d="M6 20h11l-1.2-10.5A2 2 0 0 0 13.8 8H9.2a2 2 0 0 0-2 1.5zM9 8a2.5 2.5 0 0 1 5 0M16.5 11l2.5-1.5v5L16 17"/>'),
  push: ACT_STROKE('<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0"/>'),
  login: ACT_STROKE('<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>'),
  alert: ACT_STROKE('<path d="M12 3 2 20h20zM12 10v4"/><circle cx="12" cy="17" r=".6" fill="currentColor"/>'),
  schedule: ACT_STROKE('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
};
const actIcon = (k) => ACT_ICONS[k] || ACT_ICONS.alert;

function actReset() {
  act.pages = []; act.next = undefined; act.error = null; act.gen++; act.loading = false; act.failed = false;
}
async function actLoad() {
  if (act.loading || act.next === null) return;
  act.loading = true; actDraw();
  const gen = act.gen, q = new URLSearchParams({ days: "1" });
  if (act.next) q.set("before", act.next);
  if (act.room) q.set("room", act.room);
  if (act.type) q.set("type", act.type);
  try {
    const r = await api(`/api/activity?${q}`);
    if (gen !== act.gen) return;
    act.pages.push(r); act.next = r.next_before; act.error = r.error || null;
  } catch (e) {
    if (gen !== act.gen) return;
    act.error = e.message;
    act.next = act.next ?? null; // keep what we have; "Try again" resets
    act.failed = true;
  } finally {
    if (gen === act.gen) act.loading = false;
  }
  if (gen !== act.gen) return;
  actDraw();
  const shown = act.pages.reduce((n, p) => n + p.entries.length, 0);
  if (!act.failed && act.next != null && (shown < ACT_FILL || actNearEnd())) actLoad();
}
function actNearEnd() {
  const sc = act.root?.closest(".sheet-body"), s = act.root?.querySelector(".act-more");
  if (!sc || !s) return false;
  return s.getBoundingClientRect().top < sc.getBoundingClientRect().bottom + 200;
}
function actBuild() {
  const root = ax("div", "act"); root.id = "activity";
  root.append(ax("h3", null, "Activity"), ax("div", "sub", "What happened at home, newest first — the last 7 days."));
  const f = ax("div", "act-filters");
  const room = ax("select"); room.id = "actRoom"; room.setAttribute("aria-label", "Room");
  const type = ax("select"); type.id = "actType"; type.setAttribute("aria-label", "Type");
  for (const [v, t] of ACT_TYPES) { const o = ax("option", null, t); o.value = v; type.append(o); }
  room.onchange = () => { act.room = room.value; actReset(); actLoad(); };
  type.onchange = () => { act.type = type.value; actReset(); actLoad(); };
  f.append(room, type); root.append(f);
  const list = ax("ul", "act-list"); list.id = "actList"; root.append(list);
  const more = ax("div", "act-more"); more.id = "actMore"; root.append(more);
  list.addEventListener("click", (e) => {
    const li = e.target.closest("li.act-ev[data-dev]");
    if (li && st.devices.has(li.dataset.dev)) openSheet(li.dataset.dev);
  });
  list.addEventListener("keydown", (e) => { if ((e.key === "Enter" || e.key === " ") && e.target.matches("li.act-ev[data-dev]")) { e.preventDefault(); e.target.click(); } });
  act.root = root;
  if (window.IntersectionObserver) {
    act.io = new IntersectionObserver((es) => { if (es.some((x) => x.isIntersecting) && st.sheetFor === ACTIVITY) actLoad(); },
      { root: null, rootMargin: "0px 0px 200px 0px" });
    act.io.observe(more);
  }
}
function actRooms() {
  const sel = act.root.querySelector("#actRoom"), want = [["", "All rooms"], ...st.layout.rooms.map((r) => [r.id, r.name])];
  const key = JSON.stringify(want);
  if (sel.dataset.key !== key) {
    sel.replaceChildren(...want.map(([v, t]) => { const o = ax("option", null, t); o.value = v; return o; }));
    sel.dataset.key = key;
  }
  if (act.room && !want.some(([v]) => v === act.room)) { act.room = ""; actReset(); }
  sel.value = act.room;
  act.root.querySelector("#actType").value = act.type;
}
function actTime(t) { const d = new Date(t); return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; }
function actDraw() {
  if (!act.root) return;
  const list = act.root.querySelector("#actList"), more = act.root.querySelector("#actMore");
  const labels = new Map(), items = [];
  for (const p of act.pages) { for (const d of p.days) labels.set(d.date, d.label); items.push(...p.entries); }
  list.replaceChildren();
  let day = null;
  for (const e of items) {
    if (e.day !== day) { day = e.day; list.append(ax("li", "act-day", labels.get(day) || day)); }
    const li = ax("li", `act-ev t-${e.type}`);
    if (e.entity_id && st.devices.has(e.entity_id)) { li.dataset.dev = e.entity_id; li.tabIndex = 0; li.setAttribute("role", "button"); }
    const ic = ax("span", `act-ic i-${e.icon}`); ic.innerHTML = actIcon(e.icon);
    const body = ax("div", "act-body"), main = ax("div", "act-text", e.text);
    if (e.by) main.append(ax("span", "act-by" + (e.by === "manually / other" ? " other" : ""), ` — ${e.by}`));
    body.append(main);
    const meta = [e.room_name, e.detail].filter(Boolean).join(" · ");
    if (meta) body.append(ax("div", "act-meta", meta));
    li.append(ic, body, ax("time", "act-t", actTime(e.t)));
    list.append(li);
  }
  more.replaceChildren();
  if (act.loading) more.append(ax("div", "hist-msg loading", "Loading…"));
  else if (act.failed) {
    more.append(ax("div", "hist-msg err", `Couldn't load: ${act.error}`));
    const b = ax("button", null, "Try again"); b.type = "button";
    b.onclick = () => { act.failed = false; if (!act.pages.length) actReset(); else act.next = act.pages.at(-1).next_before; actLoad(); };
    more.append(b);
  } else {
    if (act.error) more.append(ax("div", "hist-msg err", act.error));
    if (act.next != null) {
      const b = ax("button", "act-older", "Load older"); b.type = "button"; b.onclick = () => actLoad(); more.append(b);
    } else more.append(ax("div", "hist-msg", items.length ? "That's the last 7 days." : "Nothing in the last 7 days" + (act.room || act.type ? " with these filters." : ".")));
  }
}
function renderActivity(c) {
  if (!act.root) actBuild();
  actRooms();
  c.appendChild(act.root);
  if (!act.pages.length && !act.loading && !act.failed) actLoad(); // device updates re-append the same list: no redraw

}
function openActivity() { actReset(); openSheet(ACTIVITY); }
$("activityBtn").onclick = openActivity;
