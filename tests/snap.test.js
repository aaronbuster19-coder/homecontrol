// Snapping / shared-wall maths in frontend/snap.js. Run: node --test tests/   (no npm packages needed)
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// floorplan.js + snap.js with just enough of the page stubbed out.
function load() {
  const stub = () => ({ style: {}, classList: { toggle() {} }, addEventListener() {} });
  const ctx = vm.createContext({
    console, document: { querySelectorAll: () => [] },
    $: stub, el: () => stub(), svg: { clientWidth: 1000, getScreenCTM: () => ({ a: 100 }) },
    st: { draft: { rooms: [], placements: [], openings: [] }, viewBox: [0, 0, 10, 10], editing: true },
    snap: (v) => Math.round(v / 0.05) * 0.05, MIN_ROOM: 0.3, clone: (o) => JSON.parse(JSON.stringify(o)), setStatus() {}, render() {},
  });
  ctx.cur = () => ctx.st.draft;
  for (const f of ["floorplan.js", "snap.js"]) vm.runInContext(fs.readFileSync(path.join(__dirname, "../frontend", f), "utf8"), ctx, { filename: f });
  return (expr) => vm.runInContext(expr, ctx);
}
const room = (id, x, y, w, h, cut) => ({ id, name: id, x, y, w, h, ...(cut ? { cut } : {}) });

test("shared wall between two rooms is one segment", () => {
  const js = load();
  const segs = js("mergedWalls")([room("a", 0, 0, 4, 3), room("b", 4, 0, 3, 3)]);
  assert.equal(segs.length, 5); // top, bottom (each one line across both rooms), left, shared, right
  const shared = segs.filter((s) => s.shared);
  assert.equal(shared.length, 1);
  assert.deepEqual([shared[0].o, shared[0].at, shared[0].lo, shared[0].hi], ["v", 4, 0, 3]);
});

test("partly shared wall splits into shared and outer pieces; 1 cm offsets count as one wall", () => {
  const js = load();
  const segs = js("mergedWalls")([room("a", 0, 0, 4, 4), room("b", 4.01, 1, 3, 2)]);
  const at4 = segs.filter((s) => s.o === "v" && Math.abs(s.at - 4.005) < 1e-9).sort((a, b) => a.lo - b.lo);
  assert.deepEqual(JSON.parse(JSON.stringify(at4.map((s) => [s.lo, s.hi, s.shared]))), [[0, 1, false], [1, 3, true], [3, 4, false]]);
});

test("L-room inner walls take part in merging", () => {
  const js = load();
  // L with the NE corner cut 2 x 1.5: inner walls at x=3 (y 0..1.5) and y=1.5 (x 3..5)
  const segs = js("mergedWalls")([room("l", 0, 0, 5, 4, { corner: "ne", w: 2, h: 1.5 }), room("b", 3, 0, 2, 1.5)]);
  assert.equal(segs.filter((s) => s.shared).length, 2);
});

test("moving edge snaps flush to a neighbour; aligned edges too", () => {
  const js = load();
  const res = js("solveSnap")({
    fx: [{ at: 3.92, lo: 0.1, hi: 3.1 }], fy: [{ at: 0.07, lo: 1, hi: 3.92 }], targets: [room("b", 4, 0, 3, 3)], thr: 0.3,
    build: (a, b) => ({ x: 1 + (a ? a.d : 0), y: 0.07 + (b ? b.d : 0), w: 2.92, h: 3 }),
  });
  assert.ok(Math.abs(res.x.d - 0.08) < 1e-9 && res.x.at === 4);
  assert.ok(Math.abs(res.y.d + 0.07) < 1e-9 && res.y.at === 0); // top edges in line
});

test("touching edge beats a slightly closer edge that is only in line", () => {
  const js = load();
  const far = room("far", 3.95, 20, 2, 2), near = room("near", 4, 0, 3, 3);
  const c = js("axisCands")([{ at: 3.92, lo: 0, hi: 3 }], js("wallLines")([far, near]).filter((l) => l.o === "v"), 0.3, false);
  assert.equal(c[0].at, 4);
});

test("snap is skipped if it would push the room into another", () => {
  const js = load();
  // Room A (0..2) right edge at 2; B occupies 2.1..5. A's LEFT edge at 0 could snap to C's edge at 0.2 — fine;
  // but snapping A's right edge onto B's far wall (x=2.2, inside B) must be rejected.
  const B = room("b", 2.1, 0, 2.9, 3);
  const res = js("solveSnap")({
    fx: [{ at: 2.15, lo: 0, hi: 3 }], fy: [], targets: [B, room("c", 2.2, 5, 1, 1)], thr: 0.3,
    build: (a) => ({ x: 0 + (a ? a.d : 0), y: 0, w: 2.15, h: 3 }),
  });
  assert.ok(!res.x || res.x.at === 2.1, `snapped to ${res.x && res.x.at}`);
  assert.ok(js("overlapDepth")(res.g, [B]) <= 0.03);
});

test("nothing near: no snap", () => {
  const js = load();
  const res = js("solveSnap")({ fx: [{ at: 1, lo: 0, hi: 1 }], fy: [], targets: [room("b", 4, 0, 3, 3)], thr: 0.3,
    build: (a) => ({ x: a ? 1 + a.d : 1, y: 0, w: 1, h: 1 }) });
  assert.equal(res.x, null);
});

test("tidyMoves closes a 0.15 m gap and a 0.1 m overlap, keeps far rooms", () => {
  const js = load();
  const rooms = [room("big", 0, 0, 5, 4), room("gap", 5.15, 0, 3, 4), room("ovl", 0, 3.9, 5, 2), room("far", 20, 20, 2, 2)];
  const moves = js("tidyMoves")(rooms);
  const by = Object.fromEntries(moves.map((m) => [m.id, m]));
  assert.deepEqual([by.gap.dx, by.gap.dy], [-0.15, 0]);
  assert.deepEqual([by.ovl.dx, by.ovl.dy], [0, 0.1]);
  assert.equal(by.far, undefined);
  assert.equal(by.big, undefined);
});

test("roomRects of an L cover its area", () => {
  const js = load();
  const rs = js("roomRects")(room("l", 0, 0, 5, 4, { corner: "sw", w: 2, h: 1 }));
  const area = rs.reduce((s, b) => s + (b[2] - b[0]) * (b[3] - b[1]), 0);
  assert.equal(area, 20 - 2);
});

test("tidyMoves stretches a side when moving the room can't close the gap", () => {
  const js = load();
  // "short" is flush on the left (x=0) but 0.15 m short of "right" — moving it would open the left side.
  const rooms = [room("top", 0, 0, 8, 3), room("short", 0, 3, 3.85, 3), room("right", 4, 3, 4, 3)];
  const by = Object.fromEntries(js("tidyMoves")(rooms).map((m) => [m.id, m]));
  const changed = by.short || by.right;
  assert.ok(changed, "something changed");
  const s = by.short || rooms[1], r = by.right || rooms[2];
  assert.ok(Math.abs(s.x + s.w - r.x) < 1e-9, `gap closed: ${s.x + s.w} vs ${r.x}`);
  assert.equal(s.x, 0);
});
