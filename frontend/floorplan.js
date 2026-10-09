"use strict";
// L-shaped rooms (room.cut) and door/window openings on walls. Hooks are called from app.js.
const CUT_ORDER = [null, "ne", "se", "sw", "nw"];
const MIN_ARM = 0.3, MIN_CUT = 0.1;
const OPEN_LEN = { door: 0.9, window: 1.2 }, OPEN_MIN = 0.2, OPEN_MAX = 5;
const WALL_PICK_PX = 40; // how close (screen px) a tap must be to a wall when adding an opening

// ---------- geometry ----------
// Map local coords (cut always "ne") to the room's real corner by mirroring.
function cutMap(r) {
  const c = r.cut?.corner || "ne", fx = c.includes("w"), fy = c.includes("s");
  return { fx, fy, to: (u, v) => [fx ? r.x + r.w - u : r.x + u, fy ? r.y + r.h - v : r.y + v] };
}
function roomPoly(r) {
  if (!r.cut) return [[r.x, r.y], [r.x + r.w, r.y], [r.x + r.w, r.y + r.h], [r.x, r.y + r.h]];
  const { to } = cutMap(r), a = r.w - r.cut.w, b = r.cut.h;
  return [[0, 0], [a, 0], [a, b], [r.w, b], [r.w, r.h], [0, r.h]].map(([u, v]) => to(u, v));
}
function cutCorner(r) { return cutMap(r).to(r.w - r.cut.w, r.cut.h); }
function inRoom(r, p) {
  if (p.x < r.x || p.x > r.x + r.w || p.y < r.y || p.y > r.y + r.h) return false;
  if (!r.cut) return true;
  const { fx, fy } = cutMap(r);
  const u = fx ? r.x + r.w - p.x : p.x - r.x, v = fy ? r.y + r.h - p.y : p.y - r.y;
  return !(u > r.w - r.cut.w && v < r.cut.h);
}
// Top-left of the remaining area, where the name label goes.
function labelPos(r) {
  const c = r.cut?.corner;
  if (c === "nw") return r.w - r.cut.w >= 1.4 ? { x: r.x + r.cut.w, y: r.y } : { x: r.x, y: r.y + r.cut.h };
  if (c === "ne" && r.w - r.cut.w < 1.4) return { x: r.x, y: r.y + r.cut.h };
  return { x: r.x, y: r.y };
}
function clampCut(r) {
  if (!r?.cut) return;
  if (r.w < MIN_ARM + MIN_CUT || r.h < MIN_ARM + MIN_CUT) { delete r.cut; return; }
  r.cut.w = +Math.min(Math.max(r.cut.w, MIN_CUT), r.w - MIN_ARM).toFixed(3);
  r.cut.h = +Math.min(Math.max(r.cut.h, MIN_CUT), r.h - MIN_ARM).toFixed(3);
}
function walls(r) {
  const p = roomPoly(r);
  return p.map((a, i) => { const b = p[(i + 1) % p.length]; return { a, b, orient: a[1] === b[1] ? "h" : "v" }; });
}
const openEnd = (o) => o.orient === "h" ? [o.x + o.len, o.y] : [o.x, o.y + o.len];
const onSeg = (pt, w, tol = 0.03) => w.orient === "h"
  ? Math.abs(pt[1] - w.a[1]) < tol && pt[0] >= Math.min(w.a[0], w.b[0]) - tol && pt[0] <= Math.max(w.a[0], w.b[0]) + tol
  : Math.abs(pt[0] - w.a[0]) < tol && pt[1] >= Math.min(w.a[1], w.b[1]) - tol && pt[1] <= Math.max(w.a[1], w.b[1]) + tol;
function openingsOnRoom(r) {
  const ws = walls(r);
  return (st.draft.openings || []).filter((o) => {
    const m = o.orient === "h" ? [o.x + o.len / 2, o.y] : [o.x, o.y + o.len / 2];
    return ws.some((w) => w.orient === o.orient && onSeg(m, w));
  });
}
function nearestWall(pt, maxDist) {
  let best = null;
  for (const r of cur().rooms) for (const w of walls(r)) {
    const i = w.orient === "h" ? 0 : 1, j = 1 - i;
    const lo = Math.min(w.a[i], w.b[i]), hi = Math.max(w.a[i], w.b[i]);
    const along = Math.min(hi, Math.max(lo, i ? pt.y : pt.x));
    const d = Math.hypot((i ? pt.y : pt.x) - along, (j ? pt.y : pt.x) - w.a[j]);
    if (d <= maxDist && (!best || d < best.d)) best = { ...w, d, lo, hi };
  }
  return best;
}
const mpp = () => st.viewBox[2] / (svg.clientWidth || 800); // metres per screen pixel

// ---------- rendering ----------
function doorSwingSign(o) { // swing into a room if one is on the +perp side, else the other way
  const m = o.orient === "h" ? { x: o.x + o.len / 2, y: o.y + 0.1 } : { x: o.x + 0.1, y: o.y + o.len / 2 };
  return cur().rooms.some((r) => inRoom(r, m)) ? 1 : -1;
}
function renderOpenings() {
  const g = $("openings"); g.replaceChildren();
  const k = mpp();
  for (const o of cur().openings || []) {
    const [ex, ey] = openEnd(o), h = o.orient === "h";
    const og = el("g", { class: `opening ${o.type}` + (st.sel?.type === "open" && st.sel.id === o.id ? " sel" : ""), "data-open": o.id }, g);
    el("line", { class: "hit", x1: o.x, y1: o.y, x2: ex, y2: ey }, og);
    el("line", { class: "gap", x1: o.x, y1: o.y, x2: ex, y2: ey }, og);
    const d = o.entity_id && st.devices.get(o.entity_id);
    const linked = d && !st.editing;
    if (o.type === "window") {
      const off = 2.5 * k;
      for (const s of [-1, 1]) {
        const ln = el("line", { class: "pane", x1: o.x + (h ? 0 : s * off), y1: o.y + (h ? s * off : 0), x2: ex + (h ? 0 : s * off), y2: ey + (h ? s * off : 0) }, og);
        if (linked) { ln.style.stroke = deviceColor(d); ln.classList.add("linked"); }
      }
      const t = el("title", {}, og); t.textContent = d ? `${d.name} — ${deviceValue(d)}` : "Window";
    } else {
      const s = doorSwingSign(o), L = o.len;
      const tip = h ? [o.x, o.y + s * L] : [o.x + s * L, o.y];
      const sweep = h ? (s > 0 ? 0 : 1) : (s > 0 ? 1 : 0);
      const p = el("path", { class: "swing", d: `M${o.x},${o.y} L${tip[0]},${tip[1]} A${L},${L} 0 0 ${sweep} ${ex},${ey}` }, og);
      if (linked) { p.style.stroke = deviceColor(d); p.classList.add("linked"); }
      const t = el("title", {}, og); t.textContent = d ? `${d.name} — ${deviceValue(d)}` : "Door";
    }
  }
}
function handle(parent, cls, data, x, y, k) {
  const hs = 6 * k, hit = 18 * k;
  const g = el("g", { class: `handle ${cls}`, "data-h": data }, parent);
  el("rect", { class: "hit", x: x - hit, y: y - hit, width: hit * 2, height: hit * 2 }, g);
  el("rect", { x: x - hs, y: y - hs, width: hs * 2, height: hs * 2 }, g);
}
function renderFloorplanHandles(parent) {
  const L = cur(), k = mpp();
  const room = st.editing && st.sel?.type === "room" ? L.rooms.find((r) => r.id === st.sel.id) : null;
  const op = st.editing && st.sel?.type === "open" ? (L.openings || []).find((o) => o.id === st.sel.id) : null;
  if (room?.cut) { // below the 8 resize handles so a tiny room stays resizable
    const [x, y] = cutCorner(room), hg = parent.querySelector(".handles");
    handle(hg || parent, "h-cut", "cut", x, y, k);
    if (hg) hg.prepend(hg.lastChild);
  }
  if (op) { handle(parent, "h-olen", "o0", op.x, op.y, k); const [x, y] = openEnd(op); handle(parent, "h-olen", "o1", x, y, k); }
  // toolbar state
  const ls = $("lShape");
  ls.hidden = !room;
  if (room) ls.textContent = room.cut ? `L: ${room.cut.corner.toUpperCase()} ↻` : "L-shape";
  $("linkSensor").hidden = !op;
  for (const b of document.querySelectorAll(".add-open")) b.classList.toggle("primary", st.adding === b.dataset.type);
}

// ---------- editing ----------
function setAdding(type) {
  st.adding = type;
  if (type) { if (st.drawing) setDrawing(false); st.sel = null; st.picked = null; setStatus(`Tap a wall to add a ${type}`); }
  document.body.classList.toggle("drawing", !!type || !!st.drawing);
  if (st.editing) render();
}
function addOpening(pt) {
  const w = nearestWall(pt, WALL_PICK_PX * mpp());
  const type = st.adding; setAdding(null);
  if (!w) { setStatus("Tap closer to a wall"); return; }
  const len = Math.min(OPEN_LEN[type], Math.max(OPEN_MIN, snap(w.hi - w.lo)));
  const centre = w.orient === "h" ? pt.x : pt.y;
  const start = +snap(Math.min(w.hi - len, Math.max(w.lo, centre - len / 2))).toFixed(3);
  const o = { id: "o" + Date.now().toString(36) + Math.random().toString(36).slice(2, 5), type,
    x: w.orient === "h" ? start : w.a[0], y: w.orient === "h" ? w.a[1] : start, len, orient: w.orient };
  (st.draft.openings ||= []).push(o);
  st.sel = { type: "open", id: o.id }; setStatus(`${type === "door" ? "Door" : "Window"} added`); render();
}
function floorplanPointerDown(e, pt) {
  if (st.adding) { addOpening(pt); return true; }
  const start = (d) => { st.drag = { start: pt, moved: false, pid: e.pointerId, ...d }; svg.setPointerCapture(e.pointerId); render(); return true; };
  const hd = e.target.closest(".handle");
  if (hd?.dataset.h === "cut" && st.sel?.type === "room") return start({ cut: st.draft.rooms.find((r) => r.id === st.sel.id) });
  if (hd && hd.dataset.h[0] === "o" && st.sel?.type === "open") {
    const o = st.draft.openings.find((o) => o.id === st.sel.id);
    return start({ olen: hd.dataset.h, obj: o, orig: { ...o } });
  }
  const og = e.target.closest(".opening");
  if (og && !st.drawing) {
    const o = st.draft.openings.find((o) => o.id === og.dataset.open);
    st.sel = { type: "open", id: o.id };
    return start({ obj: o, axis: o.orient === "h" ? "x" : "y", ox: o.x, oy: o.y });
  }
  return false;
}
function floorplanPointerMove(d, pt, dx, dy) {
  if (d.cut) {
    const r = d.cut, { fx, fy } = cutMap(r);
    const u = fx ? r.x + r.w - pt.x : pt.x - r.x, v = fy ? r.y + r.h - pt.y : pt.y - r.y;
    r.cut.w = snap(r.w - u); r.cut.h = snap(v); clampCut(r);
    snapCut(d, pt);
    return true;
  }
  if (d.olen) {
    const o = d.obj, o0 = d.orig, h = o.orient === "h", delta = h ? dx : dy;
    const clampLen = (l) => Math.min(OPEN_MAX, Math.max(OPEN_MIN, l));
    if (d.olen === "o1") o.len = +clampLen(snap(o0.len + delta)).toFixed(3);
    else {
      const len = +clampLen(snap(o0.len - delta)).toFixed(3), endPos = (h ? o0.x : o0.y) + o0.len;
      o.len = len; o[h ? "x" : "y"] = +(endPos - len).toFixed(3);
    }
    return true;
  }
  if (d.axis) { d.obj[d.axis] = +snap(d.axis === "x" ? d.ox + dx : d.oy + dy).toFixed(3); return true; }
  return false;
}
function cycleCut() {
  const r = st.sel?.type === "room" && st.draft.rooms.find((r) => r.id === st.sel.id); if (!r) return;
  const next = CUT_ORDER[(CUT_ORDER.indexOf(r.cut?.corner || null) + 1) % CUT_ORDER.length];
  if (!next) delete r.cut;
  else { r.cut = { corner: next, w: r.cut?.w ?? snap(r.w / 2), h: r.cut?.h ?? snap(r.h / 2) }; clampCut(r); }
  if (r.cut === undefined && next) setStatus("Room too small for an L-shape");
  render();
}
function linkDialog() {
  const o = st.sel?.type === "open" && st.draft.openings.find((o) => o.id === st.sel.id); if (!o) return;
  const sel = $("linkSelect"); sel.replaceChildren(new Option("None", ""));
  for (const d of st.devices.values()) if (d.kind === "sensor") sel.appendChild(new Option(d.name, d.entity_id));
  if (o.entity_id && !st.devices.has(o.entity_id)) sel.appendChild(new Option(o.entity_id, o.entity_id));
  sel.value = o.entity_id || "";
  const dlg = $("linkDialog");
  dlg.onclose = () => {
    if (dlg.returnValue !== "ok") return;
    if (sel.value) o.entity_id = sel.value; else delete o.entity_id;
    render();
  };
  dlg.returnValue = ""; dlg.showModal();
}
for (const b of document.querySelectorAll(".add-open")) b.onclick = () => setAdding(st.adding === b.dataset.type ? null : b.dataset.type);
$("lShape").onclick = cycleCut;
$("linkSensor").onclick = linkDialog;
