"use strict";
// Furniture on the plan: catalogue + top-down drawings, edit-mode add/move/rotate/resize/duplicate, view-mode toggle.
// Data: layout.furniture = [{id, type, x, y, w, h, rot, label?, plug?, hide_marker?, thresholds?, remind?, auto_off?}] (plug & co: appliances.js) — x/y is the centre (m), w × h the size before
// rotating, rot whole degrees clockwise. Drawings face "down" (+y): backs, headboards and cisterns are at the top.
// Hooks called from app.js: renderFurniture(markersG), furniturePointerDown(e, pt), furniturePointerMove(d, pt, dx, dy),
// furnitureInRoom(r). Uses snap.js (solveSnap, linesOf, snapThr, setGuides, r3) and floorplan.js (inRoom, mpp).
const FUR_MIN = 0.2, FUR_MAX = 10, FUR_ROT_STEP = 15, FUR_LABEL_MAX = 30, FUR_KEY = "hc.showFurniture";

// ---------- drawings ----------
// Each draws into g in local coordinates centred on 0,0: x from -w/2 to w/2, y from -h/2 to h/2.
const fR = (g, x, y, w, h, c, rx = 0) => el("rect", { x, y, width: Math.max(w, 0.001), height: Math.max(h, 0.001), rx: Math.min(rx, w / 2, h / 2), class: c }, g);
const fC = (g, cx, cy, r, c) => el("circle", { cx, cy, r: Math.max(r, 0.001), class: c }, g);
const fE = (g, cx, cy, rx, ry, c) => el("ellipse", { cx, cy, rx: Math.max(rx, 0.001), ry: Math.max(ry, 0.001), class: c }, g);
const fL = (g, x1, y1, x2, y2, c = "fu-l") => el("line", { x1, y1, x2, y2, class: c }, g);
const fP = (g, d, c) => el("path", { d, class: c }, g);

function drawBed(g, w, h) {
  const x0 = -w / 2, y0 = -h / 2, hb = Math.min(0.08, h * 0.06);
  fR(g, x0, y0, w, h, "fu-b", 0.04);
  fR(g, x0, y0, w, hb, "fu-d", 0.02);
  const n = w >= 1.15 ? 2 : 1, pad = Math.min(0.08, w * 0.06), pw = (w - pad * (n + 1)) / n, ph = Math.min(0.3, h * 0.16), py = y0 + hb + 0.05;
  for (let i = 0; i < n; i++) fR(g, x0 + pad + i * (pw + pad), py, pw, ph, "fu-s", 0.06);
  const dy = py + ph + 0.07;
  fR(g, x0 + 0.03, dy, w - 0.06, y0 + h - 0.03 - dy, "fu-t", 0.05);
  const fold = Math.min(0.25, (y0 + h - dy) * 0.25);
  fL(g, x0 + 0.03, dy + fold, x0 + w - 0.03, dy + fold);
}
function drawBedside(g, w, h) {
  const m = Math.min(w, h);
  fR(g, -w / 2, -h / 2, w, h, "fu-b", 0.03);
  fC(g, 0, 0, m * 0.27, "fu-s");
  fC(g, 0, 0, m * 0.09, "fu-d");
}
function drawWardrobe(g, w, h) {
  const x0 = -w / 2, y0 = -h / 2;
  fR(g, x0, y0, w, h, "fu-b", 0.02);
  const n = Math.min(80, Math.floor((w - 0.16) / 0.09));
  for (let i = 0; i <= n; i++) { const x = x0 + 0.08 + (n ? i * (w - 0.16) / n : (w - 0.16) / 2); fL(g, x, -h * 0.28, x, h * 0.22, "fu-l fu-faint"); }
  fL(g, x0 + 0.05, -0.03, x0 + w - 0.05, -0.03, "fu-l");
  fL(g, x0 + 0.02, y0 + h - 0.04, x0 + w - 0.02, y0 + h - 0.04, "fu-l");
  fL(g, 0, y0 + h - 0.04, 0, y0 + h, "fu-l");
}
function drawSofa(n) {
  return (g, w, h) => {
    const x0 = -w / 2, y0 = -h / 2, back = Math.min(0.22, h * 0.26), arm = Math.min(0.2, w * 0.13);
    fR(g, x0, y0, w, h, "fu-b", 0.08);
    fR(g, x0, y0, w, back, "fu-s", 0.07);
    fR(g, x0, y0, arm, h, "fu-s", 0.07);
    fR(g, x0 + w - arm, y0, arm, h, "fu-s", 0.07);
    const inner = w - 2 * arm, k = Math.max(1, Math.min(n, Math.floor(inner / 0.3))), sw = inner / k;
    for (let i = 0; i < k; i++) fR(g, x0 + arm + i * sw + 0.015, y0 + back + 0.015, sw - 0.03, h - back - 0.045, "fu-t", 0.06);
  };
}
function drawCornerSofa(g, w, h) {
  const x0 = -w / 2, y0 = -h / 2, D = Math.min(0.9, w * 0.45, h * 0.6), b = Math.min(0.2, D * 0.25), arm = Math.min(0.18, D * 0.22);
  fP(g, `M${x0} ${y0}H${x0 + w}V${y0 + D}H${x0 + D}V${y0 + h}H${x0}Z`, "fu-b");
  fR(g, x0, y0, w, b, "fu-s", 0.07);
  fR(g, x0, y0, b, h, "fu-s", 0.07);
  fR(g, x0 + w - arm, y0, arm, D, "fu-s", 0.07);
  fR(g, x0, y0 + h - arm, D, arm, "fu-s", 0.07);
  const s = D - b, gap = 0.015;
  fR(g, x0 + b + gap, y0 + b + gap, s - 2 * gap, s - 2 * gap, "fu-t", 0.06); // corner seat
  const runX = w - D - arm, nx = Math.max(1, Math.round(runX / 0.65)), sx = runX / nx;
  for (let i = 0; i < nx; i++) fR(g, x0 + D + i * sx + gap, y0 + b + gap, sx - 2 * gap, s - 2 * gap, "fu-t", 0.06);
  const runY = h - D - arm, ny = Math.max(1, Math.round(runY / 0.65)), sy = runY / ny;
  if (runY > 0.1) for (let i = 0; i < ny; i++) fR(g, x0 + b + gap, y0 + D + i * sy + gap, s - 2 * gap, sy - 2 * gap, "fu-t", 0.06);
}
function drawArmchair(g, w, h) {
  const x0 = -w / 2, y0 = -h / 2, back = Math.min(0.2, h * 0.25), arm = Math.min(0.16, w * 0.2);
  fR(g, x0, y0, w, h, "fu-b", 0.12);
  fR(g, x0, y0, w, back, "fu-s", 0.1);
  fR(g, x0, y0, arm, h, "fu-s", 0.08);
  fR(g, x0 + w - arm, y0, arm, h, "fu-s", 0.08);
  fR(g, x0 + arm + 0.015, y0 + back + 0.015, w - 2 * arm - 0.03, h - back - 0.045, "fu-t", 0.07);
}
function drawTable(g, w, h) {
  const i = Math.min(0.06, w * 0.08, h * 0.08);
  fR(g, -w / 2, -h / 2, w, h, "fu-b", 0.05);
  fR(g, -w / 2 + i, -h / 2 + i, w - 2 * i, h - 2 * i, "fu-l fu-faint", 0.03);
}
function drawUnit(g, w, h) {
  const x0 = -w / 2, y0 = -h / 2, tw = Math.min(1.25, w * 0.7);
  fR(g, x0, y0, w, h, "fu-b", 0.02);
  const n = Math.max(1, Math.round(w / 0.55));
  for (let i = 1; i < n; i++) fL(g, x0 + (i * w) / n, y0 + h * 0.45, x0 + (i * w) / n, y0 + h, "fu-l fu-faint");
  fR(g, -tw / 2, y0 + Math.min(0.08, h * 0.2), tw, Math.min(0.06, h * 0.15), "fu-d", 0.01);
  fR(g, -Math.min(0.15, tw * 0.25) / 2, y0 + Math.min(0.14, h * 0.35), Math.min(0.15, tw * 0.25), Math.min(0.07, h * 0.15), "fu-d", 0.01);
}
function drawBookcase(g, w, h) {
  const x0 = -w / 2, y0 = -h / 2;
  fR(g, x0, y0, w, h, "fu-b", 0.02);
  let x = x0 + 0.04, i = 0;
  const steps = [0.035, 0.05, 0.03, 0.045, 0.04, 0.06, 0.03];
  while (x < x0 + w - 0.05 && i < 200) { fR(g, x, y0 + 0.04, steps[i % steps.length] - 0.008, h - 0.08 - (i % 3) * 0.03, "fu-s fu-faint", 0.005); x += steps[i % steps.length]; i++; }
}
function drawRug(g, w, h) {
  const x0 = -w / 2, y0 = -h / 2, i = Math.min(0.12, w * 0.1, h * 0.1);
  fR(g, x0, y0, w, h, "fu-rug", 0.03);
  fR(g, x0 + i, y0 + i, w - 2 * i, h - 2 * i, "fu-rugl", 0.02);
  const n = Math.min(60, Math.floor(h / 0.07));
  for (let k = 1; k < n; k++) { const y = y0 + (k * h) / n; fL(g, x0, y, x0 + 0.05, y, "fu-rugl"); fL(g, x0 + w - 0.05, y, x0 + w, y, "fu-rugl"); }
}
function drawPlant(g, w, h) {
  const r = Math.min(w, h) / 2;
  fC(g, 0, 0, r * 0.62, "fu-pot");
  for (let i = 0; i < 7; i++) {
    const a = i * (360 / 7) + 10, lf = el("ellipse", { cx: 0, cy: -r * 0.52, rx: r * 0.2, ry: r * 0.46, class: "fu-g", transform: `rotate(${a})` }, g);
    lf.setAttribute("data-leaf", i);
  }
  fC(g, 0, 0, r * 0.16, "fu-g");
}
function drawDesk(g, w, h) {
  const x0 = -w / 2, y0 = -h / 2, mw = Math.min(0.56, w * 0.45), kw = Math.min(0.44, w * 0.36);
  fR(g, x0, y0, w, h, "fu-b", 0.02);
  fR(g, -mw / 2, y0 + Math.min(0.07, h * 0.12), mw, Math.min(0.045, h * 0.08), "fu-d", 0.01);
  fR(g, -0.05, y0 + Math.min(0.115, h * 0.2), 0.1, Math.min(0.06, h * 0.1), "fu-d", 0.01);
  fR(g, -kw / 2, y0 + h * 0.55, kw, Math.min(0.13, h * 0.22), "fu-s", 0.015);
  fR(g, kw / 2 + 0.08, y0 + h * 0.58, Math.min(0.06, w * 0.05), Math.min(0.1, h * 0.17), "fu-s", 0.03);
}
// Chairs face up (-y), towards a desk or table above them; the backrest is at the bottom.
function drawDeskChair(g, w, h) {
  const x0 = -w / 2, y0 = -h / 2, a = Math.min(0.06, w * 0.12), bk = Math.min(0.1, h * 0.2);
  fR(g, x0 + a, y0, w - 2 * a, h - bk * 0.6, "fu-t", 0.1);
  fR(g, x0, y0 + h * 0.2, a, h * 0.5, "fu-s", 0.03);
  fR(g, x0 + w - a, y0 + h * 0.2, a, h * 0.5, "fu-s", 0.03);
  fR(g, x0 + a * 0.6, y0 + h - bk, w - a * 1.2, bk, "fu-s", bk / 2);
}
function chairAt(g, cx, cy, w, h, flip) {
  const s = flip ? -1 : 1, bk = Math.min(0.08, h * 0.2);
  fR(g, cx - w / 2, cy - h / 2, w, h, "fu-t", 0.05);
  fR(g, cx - w / 2, s > 0 ? cy + h / 2 - bk : cy - h / 2, w, bk, "fu-s", 0.03);
}
function drawChair(g, w, h) { chairAt(g, 0, 0, w, h, false); }
function drawDiningSet(g, w, h) {
  const tw = w * 0.74, th = h * 0.5, cw = Math.min(0.44, tw / 2.6), ch = Math.min(0.42, (h - th) / 2 + 0.06);
  for (const s of [-1, 1]) for (const f of [-0.25, 0.25]) chairAt(g, f * tw, s * (th / 2 + ch / 2 - 0.06), cw, ch, s < 0);
  fR(g, -tw / 2, -th / 2, tw, th, "fu-b", 0.05);
  const i = Math.min(0.06, th * 0.1);
  fR(g, -tw / 2 + i, -th / 2 + i, tw - 2 * i, th - 2 * i, "fu-l fu-faint", 0.03);
}
function drawCounter(g, w, h) {
  const x0 = -w / 2, y0 = -h / 2;
  fR(g, x0, y0, w, h, "fu-b", 0.01);
  fL(g, x0, y0 + h - 0.05, x0 + w, y0 + h - 0.05, "fu-l fu-faint");
  const n = Math.max(1, Math.round(w / 0.6));
  for (let i = 1; i < n; i++) fL(g, x0 + (i * w) / n, y0 + h - 0.05, x0 + (i * w) / n, y0 + h, "fu-l fu-faint");
}
function drawKitchenSink(g, w, h) {
  drawCounter(g, w, h);
  const bw = Math.min(0.5, w * 0.6), bh = Math.min(0.4, h * 0.65);
  fR(g, -bw / 2, -h / 2 + Math.max(0.06, (h - bh) / 2 - 0.03), bw, bh, "fu-w", 0.05);
  fC(g, 0, -h / 2 + Math.max(0.06, (h - bh) / 2 - 0.03) + bh / 2, Math.min(0.025, bw * 0.08), "fu-l");
  fC(g, 0, -h / 2 + 0.035, 0.022, "fu-d");
}
function drawHob(g, w, h) {
  fR(g, -w / 2, -h / 2, w, h, "fu-b", 0.01);
  const m = Math.min(w, h), ix = Math.min(w * 0.22, 0.15), iy = Math.min(h * 0.22, 0.13);
  fR(g, -w / 2 + 0.03, -h / 2 + 0.03, w - 0.06, h - 0.11, "fu-d", 0.02);
  for (const [x, y, r] of [[-ix, -iy, 0.1], [ix, -iy, 0.075], [-ix, iy - 0.03, 0.075], [ix, iy - 0.03, 0.1]]) {
    const rr = Math.min(r, m * 0.17);
    fC(g, x, y, rr, "fu-ring"); fC(g, x, y, rr * 0.5, "fu-ring");
  }
  for (let i = -2; i <= 2; i++) fC(g, i * Math.min(0.08, w * 0.12), h / 2 - 0.045, 0.014, "fu-l");
}
function drawFridge(g, w, h) {
  const x0 = -w / 2, y0 = -h / 2;
  fR(g, x0, y0, w, h, "fu-b", 0.03);
  fR(g, x0 + 0.04, y0 + 0.04, w - 0.08, h - 0.14, "fu-l fu-faint", 0.02);
  fL(g, x0, y0 + h - 0.06, x0 + w, y0 + h - 0.06, "fu-l");
  fR(g, x0 + w * 0.62, y0 + h - 0.06, w * 0.28, 0.035, "fu-d", 0.015);
}
function drawWasher(g, w, h) {
  const x0 = -w / 2, y0 = -h / 2, r = Math.min(w, h) * 0.32;
  fR(g, x0, y0, w, h, "fu-b", 0.03);
  fR(g, x0 + 0.03, y0 + 0.03, w - 0.06, Math.min(0.09, h * 0.15), "fu-d", 0.015);
  fC(g, 0, 0.05, r, "fu-w");
  fC(g, 0, 0.05, r * 0.62, "fu-l");
}
function drawBathtub(g, w, h) {
  const x0 = -w / 2, y0 = -h / 2, i = Math.min(0.07, w * 0.08, h * 0.1);
  fR(g, x0, y0, w, h, "fu-b", 0.05);
  fR(g, x0 + i, y0 + i, w - 2 * i, h - 2 * i, "fu-w", Math.min(w, h) * 0.32);
  const along = w >= h; // the drain and tap go at one end of the long side
  if (along) { fC(g, x0 + i + 0.17, 0, 0.03, "fu-l"); fC(g, x0 + i * 0.5, 0, 0.022, "fu-d"); }
  else { fC(g, 0, y0 + i + 0.17, 0.03, "fu-l"); fC(g, 0, y0 + i * 0.5, 0.022, "fu-d"); }
}
function drawShower(g, w, h) {
  const x0 = -w / 2, y0 = -h / 2, i = Math.min(0.05, w * 0.08);
  fR(g, x0, y0, w, h, "fu-w", 0.03);
  fR(g, x0 + i, y0 + i, w - 2 * i, h - 2 * i, "fu-l fu-faint", 0.02);
  const c = 0.05;
  fL(g, x0 + i, y0 + i, -c, -c, "fu-l fu-faint"); fL(g, x0 + w - i, y0 + i, c, -c, "fu-l fu-faint");
  fL(g, x0 + i, y0 + h - i, -c, c, "fu-l fu-faint"); fL(g, x0 + w - i, y0 + h - i, c, c, "fu-l fu-faint");
  fC(g, 0, 0, 0.05, "fu-l");
  fC(g, x0 + w / 2, y0 + 0.06, 0.035, "fu-d");
}
function drawToilet(g, w, h) {
  const x0 = -w / 2, y0 = -h / 2, ch = h * 0.27;
  fR(g, x0, y0, w, ch, "fu-b", 0.03);
  fE(g, 0, y0 + ch + (h - ch) / 2, w * 0.43, (h - ch) / 2, "fu-w");
  fE(g, 0, y0 + ch + (h - ch) / 2 + 0.015, w * 0.27, (h - ch) * 0.33, "fu-l");
}
function drawSink(g, w, h) {
  const x0 = -w / 2, y0 = -h / 2;
  fR(g, x0, y0, w, h, "fu-b", 0.04);
  fE(g, 0, 0.03, w * 0.37, h * 0.32, "fu-w");
  fC(g, 0, 0.03, 0.018, "fu-l");
  fC(g, 0, y0 + Math.min(0.07, h * 0.16), 0.022, "fu-d");
}

// ---------- appliances (can be linked to a plug: appliances.js) ----------
function drawFan(g, w, h) { // pedestal / desk fan from the front: guard, blades (spin while on), hub
  const r = Math.min(w, h) / 2;
  fC(g, 0, 0, r * 0.96, "fu-b");
  const bl = el("g", { class: "fu-blades" }, g);
  el("circle", { cx: 0, cy: 0, r: r * 0.86, class: "fu-none" }, bl); // keeps the spinning group's box still
  for (let i = 0; i < 3; i++) el("ellipse", { cx: 0, cy: -r * 0.44, rx: r * 0.2, ry: r * 0.4, class: "fu-s", transform: `rotate(${i * 120 + 15})` }, bl);
  for (let i = 0; i < 12; i++) { const a = i * Math.PI / 6; fL(bl, Math.cos(a) * r * 0.18, Math.sin(a) * r * 0.18, Math.cos(a) * r * 0.86, Math.sin(a) * r * 0.86, "fu-l fu-faint fu-guard"); }
  fC(g, 0, 0, r * 0.86, "fu-l fu-faint");
  fC(g, 0, 0, r * 0.18, "fu-d");
}
function drawFloorLamp(g, w, h) {
  const r = Math.min(w, h) / 2;
  fC(g, 0, 0, r * 0.95, "fu-t fu-shade");
  fC(g, 0, 0, r * 0.62, "fu-l fu-faint");
  fC(g, 0, 0, r * 0.2, "fu-d fu-bulb");
}
function drawTv(g, w, h) { // flat screen from above: a thin panel at the back, its stand in front
  const x0 = -w / 2, y0 = -h / 2, sh = Math.max(0.03, Math.min(0.07, h * 0.3)), fw = Math.min(w * 0.4, 0.45);
  fP(g, `M${-fw * 0.3} ${y0 + sh}L${fw * 0.3} ${y0 + sh}L${fw / 2} ${y0 + h}L${-fw / 2} ${y0 + h}Z`, "fu-s");
  fR(g, x0, y0, w, sh, "fu-d fu-screen", 0.01);
  fL(g, x0 + 0.03, y0 + sh, x0 + w - 0.03, y0 + sh, "fu-l fu-faint");
}
function drawHeater(g, w, h) { // oil-filled radiator: a row of fins on two feet
  const x0 = -w / 2, y0 = -h / 2;
  fR(g, x0, y0 + h * 0.15, w, h * 0.7, "fu-b", 0.03);
  const n = Math.max(3, Math.min(30, Math.round(w / 0.055)));
  for (let i = 1; i < n; i++) fL(g, x0 + (i * w) / n, y0 + h * 0.15, x0 + (i * w) / n, y0 + h * 0.85, "fu-l fu-faint");
  fR(g, x0 + w * 0.08, y0, w * 0.12, h, "fu-d", 0.02);
  fR(g, x0 + w * 0.8, y0, w * 0.12, h, "fu-d", 0.02);
  fC(g, x0 + w * 0.94, y0 + h * 0.5, Math.min(0.025, h * 0.15), "fu-s");
}
function drawKettle(g, w, h) { // jug kettle on its base: handle at the back, spout at the front
  const r = Math.min(w, h) * 0.38;
  fR(g, -w / 2, -h / 2, w, h, "fu-d", Math.min(w, h) * 0.18);
  fR(g, -r * 0.45, -h / 2 + 0.005, r * 0.9, h * 0.22, "fu-s", 0.015);
  fP(g, `M${-r * 0.35} ${r * 0.7}L0 ${r * 1.35}L${r * 0.35} ${r * 0.7}Z`, "fu-b");
  fC(g, 0, 0, r, "fu-b");
  fC(g, 0, 0, r * 0.62, "fu-s");
  fC(g, 0, -r * 0.2, r * 0.13, "fu-d");
}
function drawMicrowave(g, w, h) {
  const x0 = -w / 2, y0 = -h / 2, pw = Math.min(0.12, w * 0.25);
  fR(g, x0, y0, w, h, "fu-b", 0.02);
  fR(g, x0 + 0.03, y0 + 0.03, w - pw - 0.07, h - 0.06, "fu-w fu-window", 0.02);
  fC(g, x0 + (w - pw - 0.04) / 2, 0, Math.min(w - pw, h) * 0.22, "fu-l fu-faint");
  fR(g, x0 + w - pw - 0.02, y0 + 0.03, pw, h - 0.06, "fu-d", 0.01);
  for (let i = 0; i < 3; i++) fC(g, x0 + w - 0.02 - pw / 2, y0 + 0.07 + i * Math.min(0.05, (h - 0.1) / 3), Math.min(0.012, pw * 0.15), "fu-l");
}
function drawCoffee(g, w, h) { // water tank at the back, cup on the drip tray in front
  const x0 = -w / 2, y0 = -h / 2;
  fR(g, x0, y0, w, h, "fu-b", 0.03);
  fR(g, x0 + 0.03, y0 + 0.03, w - 0.06, h * 0.3, "fu-w", 0.02);
  fR(g, x0 + w * 0.15, y0 + h * 0.52, w * 0.7, h * 0.42, "fu-d", 0.02);
  fC(g, 0, y0 + h * 0.73, Math.min(w, h) * 0.13, "fu-s");
  fC(g, 0, y0 + h * 0.73, Math.min(w, h) * 0.07, "fu-l");
}
function drawToaster(g, w, h) {
  const x0 = -w / 2, y0 = -h / 2, sw = w * 0.62, sh = Math.min(0.035, h * 0.2);
  fR(g, x0, y0, w, h, "fu-b", Math.min(w, h) * 0.3);
  fR(g, -sw / 2, -h * 0.22 - sh / 2, sw, sh, "fu-d fu-slot", sh / 2);
  fR(g, -sw / 2, h * 0.22 - sh / 2, sw, sh, "fu-d fu-slot", sh / 2);
  fR(g, x0 + w - 0.012, -0.02, 0.03, 0.04, "fu-s", 0.01);
}
function drawDishwasher(g, w, h) { // rack and spray arm, control strip at the front
  const x0 = -w / 2, y0 = -h / 2, m = Math.min(w, h);
  fR(g, x0, y0, w, h, "fu-b", 0.03);
  fR(g, x0 + 0.04, y0 + 0.04, w - 0.08, h - 0.15, "fu-t", 0.02);
  for (let i = 1; i < 6; i++) { const x = x0 + 0.04 + (i * (w - 0.08)) / 6; fL(g, x, y0 + 0.05, x, y0 + h - 0.12, "fu-l fu-faint"); }
  el("rect", { x: -m * 0.34, y: -0.025 - 0.04, width: m * 0.68, height: 0.05, rx: 0.025, class: "fu-s", transform: "rotate(-20 0 -0.04)" }, g);
  fC(g, 0, -0.04, 0.03, "fu-d");
  fR(g, x0 + 0.03, y0 + h - 0.08, w - 0.06, 0.05, "fu-d", 0.015);
  fC(g, x0 + w - 0.08, y0 + h - 0.055, 0.012, "fu-l");
}
function drawDryer(g, w, h) { // like the washer, with a vented door and the lint filter
  const x0 = -w / 2, y0 = -h / 2, r = Math.min(w, h) * 0.32;
  fR(g, x0, y0, w, h, "fu-b", 0.03);
  fR(g, x0 + 0.03, y0 + 0.03, w - 0.06, Math.min(0.09, h * 0.15), "fu-d", 0.015);
  fC(g, x0 + 0.08, y0 + 0.075, 0.018, "fu-l");
  fC(g, 0, 0.05, r, "fu-t");
  fC(g, 0, 0.05, r * 0.72, "fu-l");
  for (let i = -2; i <= 2; i++) fL(g, -r * 0.4, 0.05 + i * r * 0.16, r * 0.4, 0.05 + i * r * 0.16, "fu-l fu-faint");
}
function drawFreezer(g, w, h) {
  drawFridge(g, w, h);
  const s = Math.min(w, h) * 0.18;
  for (let i = 0; i < 3; i++) { const a = i * Math.PI / 3; fL(g, -Math.cos(a) * s, -0.04 - Math.sin(a) * s, Math.cos(a) * s, -0.04 + Math.sin(a) * s, "fu-l fu-snow"); }
}
function drawIron(g, w, h) { // sole plate pointing forward (down), handle and temperature dial on top
  const x0 = -w / 2, y0 = -h / 2;
  fP(g, `M${x0} ${y0 + h * 0.12}Q${x0} ${y0} ${x0 + w * 0.15} ${y0}H${x0 + w * 0.85}Q${x0 + w} ${y0} ${x0 + w} ${y0 + h * 0.12}`
    + `V${y0 + h * 0.45}Q${x0 + w} ${y0 + h * 0.8} 0 ${y0 + h}Q${x0} ${y0 + h * 0.8} ${x0} ${y0 + h * 0.45}Z`, "fu-b");
  fR(g, -w * 0.18, y0 + h * 0.1, w * 0.36, h * 0.5, "fu-s", w * 0.15);
  fC(g, 0, y0 + h * 0.72, Math.min(w, h) * 0.12, "fu-d fu-dial");
}
function drawStraightener(g, w, h) { // two arms side by side: hinge and cable at the left, heated plates at the right
  const x0 = -w / 2, y0 = -h / 2, aw = h * 0.46;
  fR(g, x0, y0, w, aw, "fu-b", aw / 2);
  fR(g, x0, y0 + h - aw, w, aw, "fu-b", aw / 2);
  fR(g, x0 + w * 0.5, y0 + aw * 0.25, w * 0.45, aw * 0.5, "fu-d fu-plate", aw * 0.2);
  fR(g, x0 + w * 0.5, y0 + h - aw * 0.75, w * 0.45, aw * 0.5, "fu-d fu-plate", aw * 0.2);
  fC(g, x0 + h * 0.5, 0, h * 0.3, "fu-s");
}

function drawHoover(g, w, h) { // cordless stick vacuum standing in its dock: wall bracket at the back, floor head in front
  const x0 = -w / 2, y0 = -h / 2, m = Math.min(w, h), r = m * 0.2, cy = y0 + h * 0.32;
  fR(g, -w * 0.24, y0, w * 0.48, h * 0.14, "fu-d", 0.012);                  // wall bracket with the charging contacts
  fR(g, x0 + w * 0.04, y0 + h * 0.5, w * 0.92, h * 0.5, "fu-t", 0.03);       // the dock's floor plate
  fR(g, -m * 0.035, cy, m * 0.07, y0 + h * 0.62 - cy, "fu-s", m * 0.03);     // the wand, seen end-on as it rises
  fR(g, -w * 0.36, y0 + h * 0.6, w * 0.72, h * 0.25, "fu-b", 0.03);          // floor head
  fL(g, -w * 0.3, y0 + h * 0.77, w * 0.3, y0 + h * 0.77, "fu-l fu-faint");   // brush roll
  for (const s of [-1, 1]) fC(g, s * w * 0.27, y0 + h * 0.81, m * 0.022, "fu-d fu-led"); // headlights
  fR(g, -m * 0.09, y0 + h * 0.02, m * 0.18, h * 0.16, "fu-s", m * 0.06);    // handle, against the bracket
  fC(g, 0, cy, r, "fu-b");                                                   // motor and clear dust bin from above
  fC(g, 0, cy, r * 0.72, "fu-w");
  const b = r * 0.5;
  fP(g, `M${b * 0.15} ${cy - b}L${-b * 0.55} ${cy + b * 0.12}H${-b * 0.02}L${-b * 0.15} ${cy + b}L${b * 0.55} ${cy - b * 0.12}H${b * 0.02}Z`, "fu-bolt");
}
function drawDesktopPc(g, w, h) { // tower from above: exhaust fan in the top panel, front bezel with the power button
  const x0 = -w / 2, y0 = -h / 2, r = Math.min(w * 0.36, h * 0.2), cy = y0 + h * 0.36, bz = Math.min(0.05, h * 0.12);
  fR(g, x0, y0, w, h, "fu-b", 0.015);
  for (let i = 0; i < 3; i++) { const y = y0 + h * 0.05 + i * Math.min(0.02, h * 0.04); fL(g, x0 + w * 0.2, y, x0 + w * 0.8, y, "fu-l fu-faint"); }
  fC(g, 0, cy, r, "fu-l");
  fC(g, 0, cy, r * 0.62, "fu-l fu-faint");
  for (let i = 0; i < 4; i++) { const a = i * Math.PI / 4; fL(g, Math.cos(a) * r * 0.9, cy + Math.sin(a) * r * 0.9, -Math.cos(a) * r * 0.9, cy - Math.sin(a) * r * 0.9, "fu-l fu-faint"); }
  fC(g, 0, cy, r * 0.22, "fu-d");
  fR(g, x0 + w * 0.12, cy + r + h * 0.08, w * 0.76, h * 0.12, "fu-s", 0.008); // drive / I/O panel
  fR(g, x0, y0 + h - bz, w, bz, "fu-d", 0.01);                                // front bezel
  fC(g, x0 + w * 0.72, y0 + h - bz / 2, Math.min(bz * 0.32, w * 0.08), "fu-s fu-led"); // power button
}
function drawHomeServer(g, w, h) { // small NAS / mini server: vented lid, a row of drive bays across the front
  const x0 = -w / 2, y0 = -h / 2, n = Math.max(2, Math.min(8, Math.round(w / 0.09))), fy = y0 + h * 0.6, fh = h * 0.4 - 0.025;
  fR(g, x0, y0, w, h, "fu-b", 0.025);
  fR(g, x0 + w * 0.08, y0 + h * 0.07, w * 0.84, h * 0.44, "fu-l fu-faint", 0.015);
  for (let i = 1; i < 6; i++) { const y = y0 + h * 0.07 + (i * h * 0.44) / 6; fL(g, x0 + w * 0.16, y, x0 + w * 0.84, y, "fu-l fu-faint"); }
  fR(g, x0 + 0.02, fy, w - 0.04, fh, "fu-d", 0.012);
  const gap = Math.min(0.012, w * 0.03), bw = (w - 0.04 - gap * (n + 1)) / n;
  for (let i = 0; i < n; i++) {
    const bx = x0 + 0.02 + gap + i * (bw + gap);
    fR(g, bx, fy + gap, bw, fh - 2 * gap, "fu-s", 0.006);                             // drive bay
    fL(g, bx + bw * 0.25, fy + fh * 0.3, bx + bw * 0.75, fy + fh * 0.3, "fu-l fu-faint"); // its pull tab
    fC(g, bx + bw / 2, fy + fh - gap - Math.min(0.018, fh * 0.15), Math.min(0.009, bw * 0.18), "fu-d fu-led");
  }
}

const FURNITURE = {
  bed: { name: "Double bed", group: "Bedroom", w: 1.4, h: 2.0, draw: drawBed },
  bed_single: { name: "Single bed", group: "Bedroom", w: 0.9, h: 1.9, draw: drawBed },
  bedside: { name: "Bedside table", group: "Bedroom", w: 0.45, h: 0.4, draw: drawBedside },
  wardrobe: { name: "Wardrobe", group: "Bedroom", w: 1.0, h: 0.6, draw: drawWardrobe },
  sofa: { name: "Sofa", group: "Living", w: 2.0, h: 0.9, draw: drawSofa(2) },
  sofa3: { name: "3-seat sofa", group: "Living", w: 2.3, h: 0.95, draw: drawSofa(3) },
  sofa_corner: { name: "Corner sofa", group: "Living", w: 2.5, h: 1.6, draw: drawCornerSofa },
  armchair: { name: "Armchair", group: "Living", w: 0.85, h: 0.85, draw: drawArmchair },
  coffee_table: { name: "Coffee table", group: "Living", w: 1.0, h: 0.55, draw: drawTable },
  unit: { name: "TV unit / sideboard", group: "Living", w: 1.6, h: 0.45, draw: drawUnit },
  bookcase: { name: "Bookcase", group: "Living", w: 0.8, h: 0.3, draw: drawBookcase },
  rug: { name: "Rug", group: "Living", w: 2.0, h: 1.4, draw: drawRug },
  plant: { name: "Plant", group: "Living", w: 0.45, h: 0.45, draw: drawPlant },
  desk: { name: "Desk", group: "Work & dining", w: 1.2, h: 0.6, draw: drawDesk },
  desk_chair: { name: "Desk chair", group: "Work & dining", w: 0.6, h: 0.6, draw: drawDeskChair },
  dining_table: { name: "Dining table", group: "Work & dining", w: 1.4, h: 0.8, draw: drawTable },
  dining_set: { name: "Table + 4 chairs", group: "Work & dining", w: 1.6, h: 1.6, draw: drawDiningSet },
  chair: { name: "Chair", group: "Work & dining", w: 0.45, h: 0.5, draw: drawChair },
  desktop_pc: { name: "Desktop PC", group: "Work & dining", w: 0.2, h: 0.45, draw: drawDesktopPc },
  home_server: { name: "Home server", group: "Work & dining", w: 0.4, h: 0.4, draw: drawHomeServer },
  counter: { name: "Kitchen counter", group: "Kitchen", w: 1.2, h: 0.6, draw: drawCounter },
  kitchen_sink: { name: "Kitchen sink", group: "Kitchen", w: 0.8, h: 0.6, draw: drawKitchenSink },
  hob: { name: "Hob / cooker", group: "Kitchen", w: 0.6, h: 0.6, draw: drawHob },
  fridge: { name: "Fridge", group: "Kitchen", w: 0.6, h: 0.65, draw: drawFridge },
  washer: { name: "Washing machine", group: "Kitchen", w: 0.6, h: 0.6, draw: drawWasher },
  bathtub: { name: "Bathtub", group: "Bathroom", w: 1.7, h: 0.75, draw: drawBathtub },
  shower: { name: "Shower", group: "Bathroom", w: 0.9, h: 0.9, draw: drawShower },
  toilet: { name: "Toilet", group: "Bathroom", w: 0.4, h: 0.65, draw: drawToilet },
  sink: { name: "Basin", group: "Bathroom", w: 0.6, h: 0.45, draw: drawSink },
  fan: { name: "Fan", group: "Appliances", w: 0.45, h: 0.45, draw: drawFan },
  floor_lamp: { name: "Floor lamp", group: "Appliances", w: 0.4, h: 0.4, draw: drawFloorLamp },
  tv: { name: "TV", group: "Appliances", w: 1.25, h: 0.25, draw: drawTv },
  heater: { name: "Heater", group: "Appliances", w: 0.6, h: 0.25, draw: drawHeater },
  kettle: { name: "Kettle", group: "Appliances", w: 0.25, h: 0.25, draw: drawKettle },
  microwave: { name: "Microwave", group: "Appliances", w: 0.5, h: 0.4, draw: drawMicrowave },
  coffee_machine: { name: "Coffee machine", group: "Appliances", w: 0.3, h: 0.4, draw: drawCoffee },
  toaster: { name: "Toaster", group: "Appliances", w: 0.3, h: 0.2, draw: drawToaster },
  dishwasher: { name: "Dishwasher", group: "Appliances", w: 0.6, h: 0.6, draw: drawDishwasher },
  dryer: { name: "Tumble dryer", group: "Appliances", w: 0.6, h: 0.6, draw: drawDryer },
  freezer: { name: "Freezer", group: "Appliances", w: 0.6, h: 0.65, draw: drawFreezer },
  iron: { name: "Iron", group: "Appliances", w: 0.14, h: 0.27, draw: drawIron },
  hair_straightener: { name: "Hair straightener", group: "Appliances", w: 0.3, h: 0.08, draw: drawStraightener },
  hoover: { name: "Hoover", group: "Appliances", w: 0.3, h: 0.25, draw: drawHoover },
};

// ---------- geometry ----------
const furList = () => cur().furniture || [];
const furById = (id) => (st.draft?.furniture || []).find((f) => f.id === id);
const furSel = () => st.editing && st.sel?.type === "fur" ? furById(st.sel.id) : null;
const furId = () => "f" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
// Axis-aligned box around the (rotated) piece, room-shaped {x, y, w, h} so snap.js can use it.
function furBox(f) {
  const t = (f.rot || 0) * Math.PI / 180, c = Math.abs(Math.cos(t)), s = Math.abs(Math.sin(t));
  const w = f.w * c + f.h * s, h = f.w * s + f.h * c;
  return { id: f.id, x: f.x - w / 2, y: f.y - h / 2, w, h };
}
// Local (u, v) of piece f -> plan coordinates.
function furToPlan(f, u, v) {
  const t = (f.rot || 0) * Math.PI / 180, c = Math.cos(t), s = Math.sin(t);
  return [f.x + u * c - v * s, f.y + u * s + v * c];
}
function furnitureInRoom(r) { return (st.draft?.furniture || []).filter((f) => inRoom(r, f)); }

// ---------- rendering ----------
let showFurniture = true;
try { showFurniture = localStorage.getItem(FUR_KEY) !== "0"; } catch {}
const FUR_CORNERS = { nw: [-1, -1], ne: [1, -1], se: [1, 1], sw: [-1, 1] };

function renderFurniture(handlesParent) {
  const g = $("furniture");
  if (g) {
    g.replaceChildren();
    if (st.editing || showFurniture) for (const f of furList()) {
      const def = FURNITURE[f.type]; if (!def) continue;
      if (!st.editing && typeof applianceLinked === "function" && applianceLinked(f)) continue; // live: appliances.js
      if (!st.editing && typeof tvLinked === "function" && tvLinked(f)) continue; // live: tv.js
      const sel = st.editing && st.sel?.type === "fur" && st.sel.id === f.id;
      const fg = el("g", { class: `fur fu-${f.type}` + (sel ? " sel" : ""), "data-fur": f.id,
        transform: `translate(${f.x} ${f.y}) rotate(${f.rot || 0})` }, g);
      el("rect", { class: "fu-hit", x: -f.w / 2, y: -f.h / 2, width: f.w, height: f.h }, fg);
      def.draw(fg, f.w, f.h);
      if (f.plug && st.editing) { // linked to a plug: a small badge in the corner
        const b = Math.min(0.2, f.w * 0.5, f.h * 0.5);
        el("circle", { class: "fu-linkdot", cx: f.w / 2 - b * 0.55, cy: -f.h / 2 + b * 0.55, r: b / 2 }, fg);
        el("use", { href: "#ic-plug", class: "fu-linkic", x: f.w / 2 - b * 0.9, y: -f.h / 2 + b * 0.2, width: b * 0.7, height: b * 0.7 }, fg);
      }
      const t = el("title", {}, fg); t.textContent = f.label ? `${f.label} (${def.name})` : def.name;
      if (f.label) { const lt = el("text", { class: "fu-label", x: f.x, y: f.y }, g); lt.textContent = f.label; }
    }
    if (g.childElementCount) furRaiseNames(g);
  }
  if (typeof renderAppliances === "function") renderAppliances();
  if (typeof renderTvs === "function") renderTvs();
  const f = furSel();
  for (const id of ["furRot", "furDup", "furEdit"]) if ($(id)) $(id).hidden = !f;
  if ($("furLink")) $("furLink").hidden = !f || typeof APPLIANCE === "undefined" || !APPLIANCE[f.type];
  if (!f || !handlesParent) return;
  const k = mpp(), hs = 5 * k, hit = 18 * k;
  const hg = el("g", { class: "fur-handles", transform: `translate(${f.x} ${f.y}) rotate(${f.rot || 0})` }, handlesParent);
  el("rect", { class: "fur-outline", x: -f.w / 2, y: -f.h / 2, width: f.w, height: f.h }, hg);
  for (const [dir, [sx, sy]] of Object.entries(FUR_CORNERS)) {
    const cx = sx * f.w / 2, cy = sy * f.h / 2;
    const hh = el("g", { class: `handle fh fh-${dir}`, "data-h": `f-${dir}` }, hg);
    // The touch target sits mostly outside the piece, so a small piece can still be dragged by its middle.
    el("rect", { class: "hit", x: cx + sx * hit * 0.5 - hit, y: cy + sy * hit * 0.5 - hit, width: hit * 2, height: hit * 2 }, hh);
    el("rect", { x: cx - hs, y: cy - hs, width: hs * 2, height: hs * 2 }, hh);
  }
  const ry = -f.h / 2 - 30 * k;
  el("line", { class: "fur-stem", x1: 0, y1: -f.h / 2, x2: 0, y2: ry }, hg);
  const rh = el("g", { class: "handle fh fh-rot", "data-h": "f-rot" }, hg);
  el("circle", { class: "hit", cx: 0, cy: ry, r: hit }, rh);
  el("circle", { cx: 0, cy: ry, r: hs * 1.1 }, rh);
  el("path", { class: "fur-rot-ic", d: `M${-hs * 0.5} ${ry - hs * 0.2}A${hs * 0.55} ${hs * 0.55} 0 1 1 ${hs * 0.15} ${ry + hs * 0.55}` }, rh);
}

// Room names (and their temperature / size lines) under a piece of furniture are drawn again on top of it.
const FUR_TEXT_STYLE = ["fill", "fill-opacity", "font-size", "font-weight", "text-anchor", "dominant-baseline"];
function furRaiseNames(g) {
  const boxes = furList().map(furBox), out = el("g", { class: "fur-names", "aria-hidden": "true" }, g);
  for (const t of document.querySelectorAll("#rooms .room > text")) {
    let b; try { b = t.getBBox(); } catch { continue; }
    if (!b.width || !boxes.some((f) => b.x < f.x + f.w && b.x + b.width > f.x && b.y < f.y + f.h && b.y + b.height > f.y)) continue;
    const c = el("text", { x: t.getAttribute("x"), y: t.getAttribute("y") }, out), cs = getComputedStyle(t);
    c.textContent = t.textContent;
    c.setAttribute("style", FUR_TEXT_STYLE.map((p) => `${p}:${cs.getPropertyValue(p)}`).join(";")); // + a halo (CSS)
  }
}

// ---------- editing ----------
function furStart(e, pt, d) {
  st.drag = { start: pt, moved: false, pid: e.pointerId, ...d };
  svg.setPointerCapture(e.pointerId); render(); return true;
}
function furniturePointerDown(e, pt) {
  const hd = e.target.closest(".handle"), f0 = furSel();
  if (hd && f0 && hd.dataset.h?.startsWith("f-")) {
    const mode = hd.dataset.h.slice(2);
    return furStart(e, pt, { fur: mode === "rot" ? "rot" : "size", corner: mode, obj: f0, orig: { ...f0 } });
  }
  const fg = e.target.closest(".fur"); if (!fg) return false;
  const f = furById(fg.dataset.fur); if (!f) return false;
  st.sel = { type: "fur", id: f.id };
  const b = furBox(f);
  return furStart(e, pt, { fur: "move", obj: f, ox: b.x, oy: b.y, bw: b.w, bh: b.h });
}
function furniturePointerMove(d, pt, dx, dy) {
  const f = d.obj; st.snapGuides = null;
  if (d.fur === "rot") {
    const a = Math.atan2(pt.y - f.y, pt.x - f.x) * 180 / Math.PI + 90, step = d.alt ? 1 : FUR_ROT_STEP;
    f.rot = ((Math.round(a / step) * step) % 360 + 360) % 360;
    return;
  }
  if (d.fur === "size") { // the opposite corner stays put
    const o = d.orig, [sx, sy] = FUR_CORNERS[d.corner], t = (o.rot || 0) * Math.PI / 180, c = Math.cos(t), s = Math.sin(t);
    const [FX, FY] = furToPlan(o, -sx * o.w / 2, -sy * o.h / 2), [MX, MY] = furToPlan(o, sx * o.w / 2, sy * o.h / 2);
    const px = MX + dx - FX, py = MY + dy - FY, lu = px * c + py * s, lv = -px * s + py * c;
    const w = Math.min(FUR_MAX, Math.max(FUR_MIN, snap(lu * sx))), h = Math.min(FUR_MAX, Math.max(FUR_MIN, snap(lv * sy)));
    const cu = sx * w / 2, cv = sy * h / 2;
    Object.assign(f, { w: r3(w), h: r3(h), x: r3(FX + cu * c - cv * s), y: r3(FY + cu * s + cv * c) });
    return;
  }
  // Move: the box's corner goes on the 5 cm grid, or its edges flush with a wall or another piece (Alt: grid only).
  const rx = d.ox + dx, ry = d.oy + dy, box = { id: f.id, x: rx, y: ry, w: d.bw, h: d.bh };
  let g = { x: snap(rx), y: snap(ry) };
  if (!d.alt) {
    const targets = [...st.draft.rooms, ...st.draft.furniture.filter((o) => o !== f).map(furBox)];
    const res = solveSnap({ ...linesOf(box), targets, others: [], thr: snapThr(),
      build: (a, b) => ({ ...box, x: a ? rx + a.d : snap(rx), y: b ? ry + b.d : snap(ry) }) });
    g = res.g; setGuides(res);
  }
  f.x = r3(g.x + d.bw / 2); f.y = r3(g.y + d.bh / 2);
}
// Somewhere visible: the middle of the part of the plan on screen, nudged off any piece already there.
function furVisibleCentre() {
  const r = svg.getBoundingClientRect();
  const x0 = Math.max(r.left, 0), x1 = Math.min(r.right, innerWidth), y0 = Math.max(r.top, 0), y1 = Math.min(r.bottom, innerHeight);
  return svgPoint((x0 + x1) / 2, (y0 + y1) / 2);
}
function addFurniture(type, pt = furVisibleCentre()) {
  const def = FURNITURE[type]; if (!def || !st.editing) return;
  const L = st.draft; L.furniture ||= [];
  if (L.furniture.length >= 200) { setStatus("That's the most furniture a plan can hold (200)", true); return; }
  let x = r3(snap(pt.x - def.w / 2) + def.w / 2), y = r3(snap(pt.y - def.h / 2) + def.h / 2);
  for (let i = 0; i < 20 && L.furniture.some((o) => Math.abs(o.x - x) < 0.01 && Math.abs(o.y - y) < 0.01); i++) { x = r3(x + 0.25); y = r3(y + 0.25); }
  const f = { id: furId(), type, x, y, w: def.w, h: def.h, rot: 0 };
  L.furniture.push(f);
  st.sel = { type: "fur", id: f.id }; st.picked = null;
  setStatus(`${def.name} added — drag to move`);
  render();
  return f;
}
function rotateFurniture(by = 90) { const f = furSel(); if (!f) return; f.rot = (((f.rot || 0) + by) % 360 + 360) % 360; render(); }
function duplicateFurniture() {
  const f = furSel(); if (!f) return;
  if (st.draft.furniture.length >= 200) { setStatus("That's the most furniture a plan can hold (200)", true); return; }
  const c = { ...f, id: furId(), x: r3(f.x + 0.3), y: r3(f.y + 0.3) };
  delete c.plug; delete c.hide_marker; delete c.thresholds; delete c.auto_off; delete c.media; // a plug / TV links to one piece only
  st.draft.furniture.push(c); st.sel = { type: "fur", id: c.id }; render();
}
function deleteFurniture(id) { st.draft.furniture = (st.draft.furniture || []).filter((f) => f.id !== id); }

// ---------- toolbar, catalogue sheet, details dialog, menu toggle ----------
(() => {
  const h = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstChild; };
  const bar = $("editbar"), del = $("deleteSel");
  const add = h(`<button id="addFurniture" title="Add furniture: a bed, sofa, desk…" aria-label="Add furniture">+ <svg class="fur-ic" width="18" height="14" viewBox="0 0 18 14" aria-hidden="true"><path d="M3 5V3a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v2M1.5 6.5A1.5 1.5 0 0 1 4.5 6.5V9h9V6.5a1.5 1.5 0 0 1 3 0V12h-15zM3 12v1.5M15 12v1.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/></svg><span class="lbl"> Furniture</span></button>`);
  ($("addWindow") || del).after(add);
  for (const [id, text, title] of [["furRot", "↻ 90°", "Rotate 90° clockwise"], ["furDup", "Copy", "Duplicate"], ["furEdit", "Size…", "Size and label"]]) {
    const b = h(`<button id="${id}" hidden></button>`); b.textContent = text; b.title = b.ariaLabel = title; bar.insertBefore(b, del);
  }
  const sheet = h(`<div id="furSheet" class="sheet fur-sheet" hidden><div class="sheet-body">
    <button class="close" id="furClose" aria-label="Close">×</button>
    <h3>Add furniture</h3><div class="sub">Tap to put it in the middle of the plan, or drag it onto the plan.</div>
    <div id="furGrid"></div></div></div>`);
  const dlg = h(`<dialog id="furDialog"><form method="dialog" id="furForm">
    <h3 id="furDialogTitle">Furniture</h3>
    <label>Label (optional) <input name="label" maxlength="${FUR_LABEL_MAX}" autocomplete="off" placeholder="e.g. Grandma's chair"></label>
    <div class="row">
      <label>Width (<span class="u"></span>) <input name="w" type="number" step="0.05" min="0.2" max="10" required></label>
      <label>Depth (<span class="u"></span>) <input name="h" type="number" step="0.05" min="0.2" max="10" required></label>
    </div>
    <menu><button value="cancel" formnovalidate>Cancel</button><button value="ok" class="primary">OK</button></menu>
  </form></dialog>`);
  document.body.append(sheet, dlg);

  // Catalogue: one thumbnail per type, grouped.
  const grid = $("furGrid");
  let group = null, list = null;
  for (const [type, def] of Object.entries(FURNITURE)) {
    if (def.group !== group) {
      group = def.group;
      const hd = document.createElement("h4"); hd.textContent = group; grid.appendChild(hd);
      list = document.createElement("div"); list.className = "fur-grid"; grid.appendChild(list);
    }
    const b = document.createElement("button"); b.type = "button"; b.className = "fur-item"; b.dataset.type = type;
    const pad = 0.12, s = document.createElementNS(NS, "svg");
    s.setAttribute("viewBox", `${-def.w / 2 - pad} ${-def.h / 2 - pad} ${def.w + 2 * pad} ${def.h + 2 * pad}`);
    s.setAttribute("aria-hidden", "true");
    def.draw(el("g", { class: `fur fu-${type}` }, s), def.w, def.h);
    const n = document.createElement("span"); n.className = "n"; n.textContent = def.name;
    const z = document.createElement("span"); z.className = "z"; z.dataset.w = def.w; z.dataset.h = def.h;
    b.append(s, n, z); list.appendChild(b);
  }
  const open = (on) => {
    sheet.hidden = !on;
    if (on) for (const z of grid.querySelectorAll(".z")) z.textContent = `${toDisp(+z.dataset.w).toFixed(unit() === "ft" ? 1 : 2)} × ${toDisp(+z.dataset.h).toFixed(unit() === "ft" ? 1 : 2)} ${unit()}`;
  };
  add.onclick = () => { if (st.drawing) setDrawing(false); if (st.adding) setAdding(null); open(true); };
  $("furClose").onclick = () => open(false);
  sheet.addEventListener("click", (e) => { if (e.target === sheet) open(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !sheet.hidden) open(false); });

  // Tap a thumbnail: add in the middle. Drag one out: the sheet gets out of the way and it lands where it's dropped.
  let fd = null, dragged = false;
  grid.addEventListener("pointerdown", (e) => {
    const b = e.target.closest(".fur-item"); if (!b) return;
    fd = { type: b.dataset.type, x: e.clientX, y: e.clientY, ghost: null };
  });
  document.addEventListener("pointermove", (e) => {
    if (!fd) return;
    if (!fd.ghost && Math.hypot(e.clientX - fd.x, e.clientY - fd.y) > 8) {
      fd.ghost = document.createElement("div"); fd.ghost.className = "ghost fur-ghost"; document.body.appendChild(fd.ghost);
      open(false);
    }
    if (fd.ghost) { fd.ghost.style.left = e.clientX + "px"; fd.ghost.style.top = e.clientY + "px"; }
  });
  document.addEventListener("pointerup", (e) => {
    if (!fd) return;
    const d = fd; fd = null;
    if (!d.ghost) return; // a tap: the click handler adds it
    d.ghost.remove(); dragged = true; setTimeout(() => { dragged = false; }, 0);
    if (overSvg(e.clientX, e.clientY)) addFurniture(d.type, svgPoint(e.clientX, e.clientY));
  });
  document.addEventListener("pointercancel", () => { if (fd?.ghost) fd.ghost.remove(); fd = null; });
  grid.addEventListener("click", (e) => {
    const b = e.target.closest(".fur-item"); if (!b || dragged) return;
    open(false); addFurniture(b.dataset.type);
  });

  $("furRot").onclick = () => rotateFurniture(90);
  $("furDup").onclick = duplicateFurniture;
  const details = () => {
    const f = furSel(); if (!f) return;
    const form = $("furForm");
    $("furDialogTitle").textContent = FURNITURE[f.type]?.name || "Furniture";
    dlg.querySelectorAll(".u").forEach((s) => (s.textContent = unit()));
    form.label.value = f.label || "";
    form.w.value = +toDisp(f.w).toFixed(2); form.h.value = +toDisp(f.h).toFixed(2);
    dlg.onclose = () => {
      if (dlg.returnValue !== "ok") return;
      const label = form.label.value.split(/\s+/).filter(Boolean).join(" ").slice(0, FUR_LABEL_MAX);
      const w = fromDisp(+form.w.value), hh = fromDisp(+form.h.value);
      if (label) f.label = label; else delete f.label;
      if (w > 0) f.w = r3(Math.min(FUR_MAX, Math.max(FUR_MIN, snap(w))));
      if (hh > 0) f.h = r3(Math.min(FUR_MAX, Math.max(FUR_MIN, snap(hh))));
      render();
    };
    dlg.returnValue = ""; dlg.showModal(); form.label.focus();
  };
  $("furEdit").onclick = details;
  svg.addEventListener("dblclick", (e) => { if (st.editing && e.target.closest(".fur")) details(); });

  // ⋯ menu: show furniture (this device only).
  const row = h(`<label class="more-row" role="menuitem"><input type="checkbox" id="furToggle"> Show furniture</label>`);
  const anchor = $("tempToggle")?.closest("label");
  if (anchor) anchor.after(row); else $("moreMenu").prepend(row);
  const tg = $("furToggle"); tg.checked = showFurniture;
  tg.onchange = () => { showFurniture = tg.checked; try { localStorage.setItem(FUR_KEY, showFurniture ? "1" : "0"); } catch {} render(); };
})();
