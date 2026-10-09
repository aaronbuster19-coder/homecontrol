"use strict";
// Rooms that fit together: edit-mode snapping to other rooms' walls, shared walls drawn once, and "Tidy up".
// Hooks are called from app.js (render, pointer handlers) and floorplan.js (L-cut corner).
const SNAP_PX = 12, SNAP_MAX = 0.4;  // snap distance: 12 screen px, at most 0.4 m
const WALL_TOL = 0.02;               // walls this close (and collinear) are one wall
const OVERLAP_OK = 0.03;             // a snap may not push rooms into each other deeper than this
const TIDY_MAX = 0.3;                // Tidy up closes gaps/overlaps below this
const r3 = (v) => +v.toFixed(3);

// ---------- geometry (pure) ----------
// Every wall of the rooms as a line: {o: "h"|"v", at, lo, hi, room}. L-cut inner walls included.
function wallLines(rooms) {
  const out = [];
  for (const r of rooms) for (const w of walls(r)) {
    const v = w.orient === "v", i = v ? 1 : 0;
    out.push({ o: w.orient, at: v ? w.a[0] : w.a[1], lo: Math.min(w.a[i], w.b[i]), hi: Math.max(w.a[i], w.b[i]), room: r.id });
  }
  return out;
}
// A room as 1 (or 2 for an L) rectangles [x0, y0, x1, y1].
function roomRects(r) {
  if (!r.cut) return [[r.x, r.y, r.x + r.w, r.y + r.h]];
  const { to } = cutMap(r), a = r.w - r.cut.w, b = r.cut.h;
  return [[0, 0, a, r.h], [a, b, r.w, r.h]].map(([u0, v0, u1, v1]) => {
    const [x0, y0] = to(u0, v0), [x1, y1] = to(u1, v1);
    return [Math.min(x0, x1), Math.min(y0, y1), Math.max(x0, x1), Math.max(y0, y1)];
  });
}
// How deep the room pokes into any of the others (0 = no overlap).
function overlapDepth(room, others) {
  let m = 0;
  for (const a of roomRects(room)) for (const o of others) for (const b of roomRects(o)) {
    const ox = Math.min(a[2], b[2]) - Math.max(a[0], b[0]), oy = Math.min(a[3], b[3]) - Math.max(a[1], b[1]);
    if (ox > 0 && oy > 0) m = Math.max(m, Math.min(ox, oy));
  }
  return m;
}
// Snap candidates on one axis: moving lines `feats` [{at, lo, hi}] against target lines of the same orientation.
// Lines that touch (extents overlap or nearly) beat lines that are only in line with each other.
function axisCands(feats, lines, thr, nearOnly) {
  const all = [];
  for (const f of feats) for (const l of lines) {
    const d = l.at - f.at; if (Math.abs(d) > thr) continue;
    const near = Math.max(l.lo - f.hi, f.lo - l.hi) <= thr;
    if (nearOnly && !near) continue;
    all.push({ d, at: l.at, near, score: Math.abs(d) + (near ? 0 : thr * 0.3) });
  }
  all.sort((a, b) => a.score - b.score);
  const out = [];
  for (const c of all) if (!out.some((x) => Math.abs(x.d - c.d) < 1e-3)) out.push(c);
  return out.slice(0, 5);
}
// Best x/y snap. build(cx, cy) turns candidates (null = no snap) into the resulting room; a combination that would
// push it into a room (deeper than OVERLAP_OK and than without snapping) is skipped.
function solveSnap({ fx, fy, targets, others = targets, thr, build, nearOnly = false }) {
  const lines = wallLines(targets);
  const cx = [null, ...axisCands(fx, lines.filter((l) => l.o === "v"), thr, nearOnly)];
  const cy = [null, ...axisCands(fy, lines.filter((l) => l.o === "h"), thr, nearOnly)];
  const none = thr * 2, base = build(null, null);
  const limit = Math.max(OVERLAP_OK, overlapDepth(base, others) + 1e-6);
  let best = { s: none * 2, g: base, x: null, y: null };
  for (const a of cx) for (const b of cy) {
    if (!a && !b) continue;
    const s = (a ? a.score : none) + (b ? b.score : none);
    if (s >= best.s) continue;
    const g = build(a, b);
    if (g && overlapDepth(g, others) <= limit) best = { s, g, x: a, y: b };
  }
  return best;
}
// Walls of all rooms with collinear overlapping pieces merged: [{o, at, lo, hi, shared}].
function mergedWalls(rooms) {
  const out = [];
  for (const o of ["h", "v"]) {
    const ls = wallLines(rooms).filter((l) => l.o === o).sort((a, b) => a.at - b.at);
    for (let i = 0; i < ls.length;) {
      let j = i; while (j + 1 < ls.length && ls[j + 1].at - ls[i].at <= WALL_TOL + 1e-9) j++;
      const grp = ls.slice(i, j + 1); i = j + 1;
      const at = grp.reduce((s, l) => s + l.at, 0) / grp.length;
      const pts = [...new Set(grp.flatMap((l) => [l.lo, l.hi]))].sort((a, b) => a - b);
      let seg = null;
      for (let k = 0; k + 1 < pts.length; k++) {
        const p = pts[k], q = pts[k + 1]; if (q - p < 1e-6) continue;
        const m = (p + q) / 2, n = new Set(grp.filter((l) => l.lo <= m && l.hi >= m).map((l) => l.room)).size;
        if (!n) { seg = null; continue; }
        if (seg && seg.shared === n > 1 && Math.abs(seg.hi - p) < 1e-6) seg.hi = q;
        else { seg = { o, at, lo: p, hi: q, shared: n > 1 }; out.push(seg); }
      }
    }
  }
  return out;
}
// Tidy up, pass 1: move rooms (largest first, then nearest to what's placed) so nearly-touching walls touch.
// Pass 2: a side that still has a small gap to (or overlap with) a facing wall is stretched onto it.
// Returns [{id, dx, dy, x, y, w, h}] for rooms that change (dx/dy = how far it moved); rooms are not modified.
function tidyMoves(rooms, thr = TIDY_MAX) {
  if (rooms.length < 2) return [];
  const work = rooms.map((r) => ({ ...r })), orig = new Map(work.map((r) => [r, { ...r }])), moved = new Map();
  const area = (r) => roomRects(r).reduce((s, b) => s + (b[2] - b[0]) * (b[3] - b[1]), 0);
  const gap = (a, b) => Math.max(a.x - (b.x + b.w), b.x - (a.x + a.w), a.y - (b.y + b.h), b.y - (a.y + a.h), 0);
  const left = [...work].sort((a, b) => area(b) - area(a)), placed = [left.shift()];
  while (left.length) {
    let bi = 0, bd = Infinity;
    left.forEach((r, i) => { const d = Math.min(...placed.map((p) => gap(r, p))); if (d < bd) { bd = d; bi = i; } });
    const r = left.splice(bi, 1)[0], x0 = r.x, y0 = r.y, ls = wallLines([r]);
    const res = solveSnap({
      fx: ls.filter((l) => l.o === "v"), fy: ls.filter((l) => l.o === "h"), thr, nearOnly: true,
      targets: placed, others: work.filter((o) => o !== r),
      build: (a, b) => ({ ...r, x: x0 + (a ? a.d : 0), y: y0 + (b ? b.d : 0) }),
    });
    r.x = r3(res.g.x); r.y = r3(res.g.y); moved.set(r, { dx: r3(r.x - x0), dy: r3(r.y - y0) });
    placed.push(r);
  }
  for (const r of placed) for (const side of ["e", "w", "s", "n"]) {
    if (r.cut?.corner.includes(side)) continue; // that side is shared with the cut; leave L shapes alone there
    const v = side === "e" || side === "w", dir = side === "e" || side === "s" ? 1 : -1;
    const at = v ? (dir > 0 ? r.x + r.w : r.x) : (dir > 0 ? r.y + r.h : r.y);
    const lo = v ? r.y : r.x, hi = v ? r.y + r.h : r.x + r.w;
    let best = null;
    for (const o of work) if (o !== r) for (const l of wallLines([o])) {
      const d = l.at - at, ov = Math.min(hi, l.hi) - Math.max(lo, l.lo);
      if (l.o !== (v ? "v" : "h") || Math.abs(d) < 1e-4 || Math.abs(d) >= thr || ov < 0.2) continue;
      const m = (Math.max(lo, l.lo) + Math.min(hi, l.hi)) / 2, beyond = l.at + dir * 0.01;  // o lies past its wall?
      if (!inRoom(o, v ? { x: beyond, y: m } : { x: m, y: beyond })) continue;
      if (!best || Math.abs(d) < Math.abs(best)) best = d;
    }
    if (best == null) continue;
    const g = { ...r };
    if (side === "e") g.w += best; if (side === "s") g.h += best;
    if (side === "w") { g.x += best; g.w -= best; } if (side === "n") { g.y += best; g.h -= best; }
    if (g.w < MIN_ROOM || g.h < MIN_ROOM) continue;
    if (overlapDepth(g, work.filter((o) => o !== r)) > OVERLAP_OK) continue;
    for (const k of ["x", "y", "w", "h"]) r[k] = r3(g[k]);
  }
  const out = [];
  for (const r of work) {
    const o = orig.get(r);
    if (["x", "y", "w", "h"].some((k) => Math.abs(r[k] - o[k]) > 1e-4))
      out.push({ id: r.id, x: r.x, y: r.y, w: r.w, h: r.h, ...(moved.get(r) || { dx: 0, dy: 0 }) });
  }
  return out;
}

// ---------- edit-mode hooks ----------
function snapThr() {
  let k = 0;
  try { k = 1 / svg.getScreenCTM().a; } catch {}
  return Math.min(SNAP_MAX, SNAP_PX * (k > 0 && isFinite(k) ? k : mpp()));
}
const linesOf = (r) => { const ls = wallLines([r]); return { fx: ls.filter((l) => l.o === "v"), fy: ls.filter((l) => l.o === "h") }; };
function setGuides(res) { st.snapGuides = [res.x && { o: "v", at: res.x.at }, res.y && { o: "h", at: res.y.at }].filter(Boolean); }

// Moving a room (d.obj, from d.ox/d.oy by dx/dy). Sets its x/y; the caller moves what it carries.
function snapMove(d, dx, dy) {
  const r = d.obj, rx = d.ox + dx, ry = d.oy + dy;
  st.snapGuides = null;
  if (d.alt) { r.x = r3(snap(rx)); r.y = r3(snap(ry)); return; }
  const others = st.draft.rooms.filter((o) => o !== r);
  const res = solveSnap({ ...linesOf({ ...r, x: rx, y: ry }), targets: others, thr: snapThr(),
    build: (a, b) => ({ ...r, x: a ? rx + a.d : snap(rx), y: b ? ry + b.d : snap(ry) }) });
  r.x = r3(res.g.x); r.y = r3(res.g.y); setGuides(res);
}
// Resizing with a handle; resizeRoom() has already applied the grid-snapped size.
function snapResize(d, dx, dy) {
  st.snapGuides = null;
  if (d.alt) return;
  const r = d.obj, o = d.orig, dir = d.resize, fx = [], fy = [];
  const E = o.x + o.w + dx, W = o.x + dx, S = o.y + o.h + dy, N = o.y + dy;
  if (dir.includes("e")) fx.push({ at: E, lo: r.y, hi: r.y + r.h });
  if (dir.includes("w")) fx.push({ at: W, lo: r.y, hi: r.y + r.h });
  if (dir.includes("s")) fy.push({ at: S, lo: r.x, hi: r.x + r.w });
  if (dir.includes("n")) fy.push({ at: N, lo: r.x, hi: r.x + r.w });
  const build = (a, b) => {
    const g = { ...r, cut: r.cut && { ...r.cut } };
    if (a && dir.includes("e")) g.w = Math.max(MIN_ROOM, E + a.d - g.x);
    if (a && dir.includes("w")) { const x = Math.min(W + a.d, o.x + o.w - MIN_ROOM); g.w = o.x + o.w - x; g.x = x; }
    if (b && dir.includes("s")) g.h = Math.max(MIN_ROOM, S + b.d - g.y);
    if (b && dir.includes("n")) { const y = Math.min(N + b.d, o.y + o.h - MIN_ROOM); g.h = o.y + o.h - y; g.y = y; }
    clampCut(g);
    return g;
  };
  const res = solveSnap({ fx, fy, targets: st.draft.rooms.filter((x) => x !== r), thr: snapThr(), build });
  for (const k of ["x", "y", "w", "h"]) r[k] = r3(res.g[k]);
  setGuides(res);
}
// Drawing a new room: the start point, then the dragged corner.
function snapDrawStart(pt, alt) {
  st.snapGuides = null;
  if (alt) return { x: snap(pt.x), y: snap(pt.y) };
  const res = solveSnap({ fx: [{ at: pt.x, lo: pt.y, hi: pt.y }], fy: [{ at: pt.y, lo: pt.x, hi: pt.x }],
    targets: st.draft.rooms, thr: snapThr(),
    build: (a, b) => ({ x: a ? pt.x + a.d : snap(pt.x), y: b ? pt.y + b.d : snap(pt.y), w: 0, h: 0 }) });
  setGuides(res);
  return { x: r3(res.g.x), y: r3(res.g.y) };
}
function snapDrawRect(p0, pt, alt) {
  const rect = (x2, y2) => ({ x: Math.min(p0.x, x2), y: Math.min(p0.y, y2), w: Math.abs(x2 - p0.x), h: Math.abs(y2 - p0.y) });
  st.snapGuides = null;
  if (alt) return rect(snap(pt.x), snap(pt.y));
  const res = solveSnap({
    fx: [{ at: pt.x, lo: Math.min(p0.y, pt.y), hi: Math.max(p0.y, pt.y) }],
    fy: [{ at: pt.y, lo: Math.min(p0.x, pt.x), hi: Math.max(p0.x, pt.x) }],
    targets: st.draft.rooms, thr: snapThr(),
    build: (a, b) => rect(a ? pt.x + a.d : snap(pt.x), b ? pt.y + b.d : snap(pt.y)) });
  setGuides(res);
  const g = res.g; return { x: r3(g.x), y: r3(g.y), w: r3(g.w), h: r3(g.h) };
}
// Dragging the L-cut inner corner to pt; floorplanPointerMove() has already applied the grid-snapped cut.
function snapCut(d, pt) {
  const r = d.cut; st.snapGuides = null;
  if (!r?.cut || d.alt) return;
  const { fx: mx, fy: my } = cutMap(r), grid = { ...r.cut };
  const ox = mx ? r.x : r.x + r.w, oy = my ? r.y + r.h : r.y;     // outer edges on the cut's side
  const build = (a, b) => {
    const g = { ...r, cut: { ...grid } };
    if (a) { const X = pt.x + a.d; g.cut.w = mx ? X - r.x : r.x + r.w - X; }
    if (b) { const Y = pt.y + b.d; g.cut.h = my ? r.y + r.h - Y : Y - r.y; }
    clampCut(g);
    return g.cut ? g : null;
  };
  const res = solveSnap({ fx: [{ at: pt.x, lo: Math.min(pt.y, oy), hi: Math.max(pt.y, oy) }],
    fy: [{ at: pt.y, lo: Math.min(pt.x, ox), hi: Math.max(pt.x, ox) }],
    targets: st.draft.rooms.filter((x) => x !== r), thr: snapThr(), build });
  r.cut = res.g.cut; r.cut.w = r3(r.cut.w); r.cut.h = r3(r.cut.h);
  setGuides(res);
}

// ---------- tidy up ----------
function applyTidy(moves) {
  // Decide what each room carries before anything moves; a device/opening goes with the first room that has it.
  // Rooms that only grow or shrink a side carry nothing.
  const taken = new Set(), plan = [];
  for (const r of st.draft.rooms) {
    const m = moves.find((x) => x.id === r.id) || { dx: 0, dy: 0 };
    const items = [...st.draft.placements.filter((p) => inRoom(r, p)), ...openingsOnRoom(r)].filter((p) => !taken.has(p));
    items.forEach((p) => taken.add(p));
    plan.push({ r, m, items });
  }
  for (const { r, m, items } of plan) {
    if (m.id) Object.assign(r, { x: m.x, y: m.y, w: m.w, h: m.h });
    if (m.dx || m.dy) for (const p of items) { p.x = r3(p.x + m.dx); p.y = r3(p.y + m.dy); }
  }
}
function tidyUp() {
  if (st.tidy) { st.tidy = null; setStatus("Tidy up kept — Save to keep it"); render(); return; }
  const moves = tidyMoves(st.draft.rooms);
  if (!moves.length) { setStatus("Nothing to tidy — no small gaps between rooms"); return; }
  const before = clone(st.draft);
  st.tidy = { before, ghosts: moves.map((m) => before.rooms.find((r) => r.id === m.id)) };
  applyTidy(moves);
  setStatus(`Tidy up moved ${moves.length} room${moves.length > 1 ? "s" : ""} (old outline dashed) — Keep or Undo`);
  render();
}
function tidyUndo() {
  if (!st.tidy) return;
  st.draft = st.tidy.before; st.tidy = null; setStatus("Tidy up undone"); render();
}

// ---------- rendering ----------
function renderSnap() {
  let g = $("walls");
  if (!g) { g = el("g", { id: "walls" }); $("openings").before(g); }
  g.replaceChildren();
  if (!st.editing) st.tidy = null;
  const L = cur();
  for (const s of mergedWalls(L.rooms)) {
    const [x1, y1, x2, y2] = s.o === "h" ? [s.lo, s.at, s.hi, s.at] : [s.at, s.lo, s.at, s.hi];
    el("line", { class: "wall " + (s.shared ? "inner" : "outer"), x1, y1, x2, y2 }, g);
  }
  const pts = (r) => roomPoly(r).map((p) => p.join(",")).join(" ");
  for (const r of st.tidy?.ghosts || []) el("polygon", { class: "tidy-ghost", points: pts(r) }, g);
  const sel = st.editing && st.sel?.type === "room" && L.rooms.find((r) => r.id === st.sel.id);
  if (sel) el("polygon", { class: "sel-outline", points: pts(sel) }, g);
  if (st.drag && st.snapGuides) {
    const [vx, vy, vw, vh] = st.viewBox;
    for (const s of st.snapGuides) {
      const a = s.o === "v" ? { x1: s.at, y1: vy - vh, x2: s.at, y2: vy + vh * 2 } : { x1: vx - vw, y1: s.at, x2: vx + vw * 2, y2: s.at };
      el("line", { class: "snap-guide", ...a }, g);
    }
  }
  const t = $("tidyUp"), u = $("tidyUndo");
  if (t) { t.textContent = st.tidy ? "Keep" : "Tidy up"; t.classList.toggle("primary", !!st.tidy); }
  if (u) u.hidden = !st.tidy;
}

if ($("tidyUp")) $("tidyUp").onclick = tidyUp;
if ($("tidyUndo")) $("tidyUndo").onclick = tidyUndo;
