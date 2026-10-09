// Browser tests for room snapping, shared walls and Tidy up (desktop mouse + 390px touch phone).
// Needs Playwright (Node) and two servers — a fake Home Assistant and the app with a throwaway database:
//   (cd tests/e2e && uvicorn fake_ha:app --port 8123) &
//   HA_URL=http://127.0.0.1:8123 HA_TOKEN=tok APP_USER=u APP_PASSWORD=p DB_PATH=/tmp/hc-e2e.db \
//     uvicorn backend.app:create_app --factory --port 8077 &
//   node tests/e2e/snap_e2e.js        # screenshots go to $SHOTS (default: the OS temp dir)
// It overwrites the layout of that app instance.
const { chromium } = require("playwright");
const BASE = "http://127.0.0.1:8077";
const OUT = process.env.SHOTS || require("os").tmpdir() + "/hc-snap-shots";
require("fs").mkdirSync(OUT, { recursive: true });
let fails = 0;
const check = (name, ok, extra = "") => { console.log(`${ok ? "PASS" : "FAIL"} ${name}${extra ? " — " + extra : ""}`); if (!ok) fails++; };
const near = (a, b, t = 1e-6) => Math.abs(a - b) <= t;

async function login(page) {
  await page.goto(BASE + "/login.html");
  await page.fill("[name=username]", "u"); await page.fill("[name=password]", "p");
  await Promise.all([page.waitForURL(BASE + "/"), page.click("button[type=submit]")]);
  await page.waitForSelector("#plan");
}
const putLayout = (page, layout) => page.evaluate(async (l) => {
  const r = await fetch("/api/layout", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(l) });
  if (!r.ok) throw new Error(await r.text());
}, layout);
const getLayout = (page) => page.evaluate(() => fetch("/api/layout").then((r) => r.json()));
const room = (id, x, y, w, h, cut) => ({ id, name: id, x, y, w, h, ...(cut ? { cut } : {}) });
const scr = (page, x, y) => page.evaluate(([x, y]) => {
  const m = document.getElementById("plan").getScreenCTM(); return [m.a * x + m.e, m.d * y + m.f];
}, [x, y]);
const roomOf = (L, id) => L.rooms.find((r) => r.id === id);
const draftRoom = (page, id) => page.evaluate((id) => st.draft.rooms.find((r) => r.id === id), id);
const guides = (page) => page.locator("#walls .snap-guide").count();

async function freshEdit(page, layout) {
  await putLayout(page, layout);
  await page.goto(BASE + "/"); await page.waitForSelector("#walls line", { state: "attached" });
  await page.click("#editToggle"); await page.waitForSelector("#editbar:not([hidden])");
}
// mouse drag in plan coordinates; optional check while still pressed
async function mdrag(page, from, to, during) {
  const [x0, y0] = await scr(page, ...from);
  await page.mouse.move(x0, y0); await page.mouse.down();
  const [x1, y1] = await scr(page, ...to); // viewBox is frozen while dragging
  await page.mouse.move((x0 + x1) / 2, (y0 + y1) / 2, { steps: 4 });
  await page.mouse.move(x1, y1, { steps: 6 });
  const r = during ? await during() : null;
  await page.mouse.up();
  return r;
}
// touch drag via CDP
async function tdrag(page, cdp, from, to, during) {
  const [x0, y0] = await scr(page, ...from);
  await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: x0, y: y0 }] });
  const [x1, y1] = await scr(page, ...to);
  for (let i = 1; i <= 10; i++) {
    await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: x0 + (x1 - x0) * i / 10, y: y0 + (y1 - y0) * i / 10 }] });
  }
  const r = during ? await during() : null;
  await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  await page.waitForTimeout(50);
  return r;
}
async function tap(page, cdp, pt) {
  const [x, y] = await scr(page, ...pt);
  await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x, y }] });
  await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  await page.waitForTimeout(50);
}
async function save(page) { await page.click("#save"); await page.waitForSelector("#editbar[hidden]", { state: "attached" }); }
async function nameRoom(page, name) {
  await page.waitForSelector("#roomDialog[open]");
  await page.fill("#roomForm [name=name]", name);
  await page.click("#roomDialog button[value=ok]");
}

async function desktop(browser) {
  console.log("--- desktop (mouse, 1280x800)");
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const page = await ctx.newPage();
  const errors = []; page.on("pageerror", (e) => errors.push(e.message));
  await login(page);
  const thr = await page.evaluate(() => snapThr());
  console.log(`snap threshold at this zoom: ${thr.toFixed(3)} m`);

  // 1) draw a room next to an existing one -> flush
  await freshEdit(page, { unit: "m", rooms: [room("lounge", 0, 0, 4, 3)], placements: [] });
  await page.click("#addRoom");
  const g1 = await mdrag(page, [4.07, 0.06], [7.03, 2.93], () => guides(page));
  check("guide lines visible while drawing", g1 >= 1, `${g1} guides`);
  await nameRoom(page, "Kitchen");
  check("guide lines gone after the drag", (await guides(page)) === 0);
  await save(page);
  let L = await getLayout(page), k = L.rooms.find((r) => r.name === "Kitchen");
  check("drawn room snaps flush (x=4, y=0, h=3 in GET /api/layout)", k && k.x === 4 && k.y === 0 && k.h === 3 && near(k.w, 3), JSON.stringify(k));

  // 2) move a room near another -> snaps; carries its device and door
  await freshEdit(page, { unit: "m", rooms: [room("lounge", 0, 0, 4, 3), room("bed", 8, 0.5, 3, 3)],
    placements: [{ entity_id: "light.lounge", x: 9.5, y: 2 }],
    openings: [{ id: "d1", type: "door", x: 8.5, y: 3.5, len: 0.9, orient: "h" }] });
  const g2 = await mdrag(page, [9, 1.2], [9 - 3.92, 1.2 - 0.55], () => guides(page));
  check("guide lines visible while moving", g2 >= 1, `${g2} guides`);
  check("guide lines gone after the move", (await guides(page)) === 0);
  let b = await draftRoom(page, "bed");
  check("moved room snaps to neighbour wall (x=4) and top edge in line (y=0)", b.x === 4 && b.y === 0, JSON.stringify(b));
  const carried = await page.evaluate(() => [st.draft.placements[0], st.draft.openings[0]]);
  check("device and door move with the room by the same amount",
    near(carried[0].x, 5.5) && near(carried[0].y, 1.5) && near(carried[1].x, 4.5) && near(carried[1].y, 3), JSON.stringify(carried));

  // 3) Alt disables snapping
  await page.click("#cancelEdit");
  await freshEdit(page, { unit: "m", rooms: [room("lounge", 0, 0, 4, 3), room("bed", 8, 0.5, 3, 3)], placements: [] });
  await page.keyboard.down("Alt");
  const g3 = await mdrag(page, [9, 1.2], [9 - 3.92, 1.2 - 0.55], () => guides(page));
  await page.keyboard.up("Alt");
  b = await draftRoom(page, "bed");
  check("Alt: no snap, grid only (x=4.1, y=-0.05)", near(b.x, 4.1) && near(b.y, -0.05) && g3 === 0, JSON.stringify(b) + ` guides=${g3}`);
  await page.click("#cancelEdit");

  // 4) resize handle snaps to a neighbour's edge
  await freshEdit(page, { unit: "m", rooms: [room("lounge", 0, 0, 4, 3), room("bath", 0, 4, 2.5, 2), room("hall", 4, 3, 3, 3)], placements: [] });
  await page.click(`[data-room=bath]`, { position: { x: 20, y: 30 } }); // select
  const nh = await page.locator(".handle.h-n rect.hit").boundingBox();
  const [, ty] = await scr(page, 0, 3.06);
  await page.mouse.move(nh.x + nh.width / 2, nh.y + nh.height / 2); await page.mouse.down();
  await page.mouse.move(nh.x + nh.width / 2, ty, { steps: 6 });
  const g4 = await guides(page); await page.mouse.up();
  let bath = await draftRoom(page, "bath");
  check("n handle snaps to lounge's bottom wall (y=3, h=3)", bath.y === 3 && bath.h === 3 && g4 >= 1, JSON.stringify(bath));
  const eh = await page.locator(".handle.h-e rect.hit").boundingBox();
  const [tx] = await scr(page, 3.93, 0);
  await page.mouse.move(eh.x + eh.width / 2, eh.y + eh.height / 2); await page.mouse.down();
  await page.mouse.move(tx, eh.y + eh.height / 2, { steps: 6 }); await page.mouse.up();
  bath = await draftRoom(page, "bath");
  check("e handle snaps to the hall's west wall (w=4)", bath.w === 4 && bath.x === 0, JSON.stringify(bath));
  await page.click("#cancelEdit");

  // 5) L-cut inner corner snaps to a neighbour's walls
  await freshEdit(page, { unit: "m", rooms: [room("living", 0, 6, 6, 4, { corner: "ne", w: 1.5, h: 1.5 }), room("store", 4, 5, 3, 2)], placements: [] });
  await page.click(`[data-room=living]`, { position: { x: 30, y: 120 } });
  const ch = await page.locator(".handle.h-cut rect.hit").boundingBox();
  const [cx, cy] = await scr(page, 4.07, 7.08);
  await page.mouse.move(ch.x + ch.width / 2, ch.y + ch.height / 2); await page.mouse.down();
  await page.mouse.move(cx, cy, { steps: 6 });
  const g5 = await guides(page); await page.mouse.up();
  const lv = await draftRoom(page, "living");
  check("L-cut corner snaps onto the store's walls (cut 2 x 1)", lv.cut.w === 2 && lv.cut.h === 1 && g5 === 2, JSON.stringify(lv.cut) + ` guides=${g5}`);
  await page.click("#cancelEdit");

  // 6) shared wall drawn once; doors/windows on it render and select
  await freshEdit(page, { unit: "m", rooms: [room("lounge", 0, 0, 4, 3), room("kitchen", 4, 0, 3, 3)], placements: [],
    openings: [{ id: "d1", type: "door", x: 4, y: 1, len: 0.9, orient: "v" }] });
  const walls = await page.evaluate(() => [...document.querySelectorAll("#walls line.wall")].map((l) => ({
    x1: +l.getAttribute("x1"), y1: +l.getAttribute("y1"), x2: +l.getAttribute("x2"), y2: +l.getAttribute("y2"), cls: l.getAttribute("class") })));
  const at4 = walls.filter((w) => w.x1 === 4 && w.x2 === 4);
  check("shared wall x=4 drawn once (1 line, inner)", at4.length === 1 && at4[0].cls.includes("inner") && at4[0].y1 === 0 && at4[0].y2 === 3, JSON.stringify(at4));
  check("5 wall lines for 2 side-by-side rooms (was 8 edges)", walls.length === 5, `${walls.length}`);
  check("outer walls heavier than inner", await page.evaluate(() =>
    parseFloat(getComputedStyle(document.querySelector(".wall.outer")).strokeWidth) > parseFloat(getComputedStyle(document.querySelector(".wall.inner")).strokeWidth)));
  await page.click("#addWindow");
  const [wx, wy] = await scr(page, 4.03, 2.3); await page.mouse.click(wx, wy);
  const ops = await page.evaluate(() => st.draft.openings.map((o) => ({ ...o })));
  check("window added on the shared wall", ops.length === 2 && ops[1].orient === "v" && ops[1].x === 4, JSON.stringify(ops[1]));
  check("door renders gap + swing, window two panes",
    (await page.locator(".opening.door .gap").count()) === 1 && (await page.locator(".opening.door .swing").count()) === 1 && (await page.locator(".opening.window .pane").count()) === 2);
  await page.mouse.click(10, 10); // deselect (off the plan)
  const [dx, dy] = await scr(page, 4, 1.45); await page.mouse.click(dx, dy);
  check("door on shared wall is selectable", await page.evaluate(() => st.sel?.type === "open" && st.sel.id === "d1"));
  check("Link sensor shows for the selected door", await page.isVisible("#linkSensor"));
  await save(page);
  check("view mode: door and window still drawn on shared wall", (await page.locator(".opening.door .swing").count()) === 1 && (await page.locator(".opening.window .pane").count()) === 2);

  // 7) Tidy up closes a 0.15 m gap; Undo restores; Keep + Save persists
  await freshEdit(page, { unit: "m", rooms: [room("lounge", 0, 0, 5, 4), room("kitchen", 5.15, 0, 3, 4)],
    placements: [{ entity_id: "light.kitchen", x: 6.5, y: 2 }] });
  await page.click("#tidyUp");
  check("tidy preview: ghost outline + Keep/Undo", (await page.locator(".tidy-ghost").count()) === 1 && await page.isVisible("#tidyUndo") && (await page.textContent("#tidyUp")) === "Keep");
  check("tidy moved kitchen flush in the draft", (await draftRoom(page, "kitchen")).x === 5);
  await page.click("#tidyUndo");
  check("Undo puts it back", (await draftRoom(page, "kitchen")).x === 5.15 && !(await page.isVisible("#tidyUndo")));
  await page.click("#tidyUp"); await page.click("#tidyUp"); // tidy, keep
  check("Keep clears the preview", (await page.locator(".tidy-ghost").count()) === 0);
  L = await getLayout(page);
  check("not saved until Save", roomOf(L, "kitchen").x === 5.15);
  await save(page);
  L = await getLayout(page);
  check("after Save: gap closed, device carried", roomOf(L, "kitchen").x === 5 && near(L.placements[0].x, 6.35), JSON.stringify(roomOf(L, "kitchen")) + JSON.stringify(L.placements[0]));
  await page.click("#editToggle"); await page.click("#tidyUp");
  check("nothing left to tidy", (await page.textContent("#status")).includes("Nothing to tidy"));
  await page.click("#cancelEdit");
  // Cancel discards a tidy
  await freshEdit(page, { unit: "m", rooms: [room("lounge", 0, 0, 5, 4), room("kitchen", 5.15, 0, 3, 4)], placements: [] });
  await page.click("#tidyUp"); await page.click("#cancelEdit");
  check("Cancel discards a tidy", roomOf(await getLayout(page), "kitchen").x === 5.15 && (await page.evaluate(() => st.layout.rooms[1].x)) === 5.15);

  // 8) screenshots of an L-shaped flat before/after tidy
  const flat = { unit: "m", rooms: [
    room("Living room", 0, 0, 6.2, 4.8, { corner: "se", w: 2.2, h: 1.6 }), room("Kitchen", 6.33, 0, 3.4, 3.1),
    room("Hall", 4.1, 4.95, 2.1, 3.0), room("Bathroom", 6.25, 3.2, 2.4, 2.2), room("Bedroom", 0, 4.9, 3.95, 3.5),
    room("Study", 8.75, 3.0, 2.7, 3.2, { corner: "sw", w: 0.9, h: 1.0 }) ].map((r) => ({ ...r, id: r.id.toLowerCase().replace(" ", "") })),
    placements: [{ entity_id: "light.lounge", x: 2, y: 2 }, { entity_id: "light.kitchen", x: 8, y: 1.5 }, { entity_id: "switch.tv", x: 4.5, y: 0.8 },
      { entity_id: "climate.lounge_valve", x: 0.6, y: 3.8 }, { entity_id: "binary_sensor.contact_sensor_door", x: 5.1, y: 7.5 }],
    openings: [{ id: "o1", type: "door", x: 4.6, y: 7.95, len: 0.9, orient: "h" }, { id: "o2", type: "window", x: 1, y: 0, len: 1.6, orient: "h" },
      { id: "o3", type: "window", x: 7.2, y: 0, len: 1.2, orient: "h" }, { id: "o4", type: "door", x: 6.2, y: 1.2, len: 0.8, orient: "v" },
      { id: "o5", type: "door", x: 1.5, y: 4.8, len: 0.8, orient: "h" }, { id: "o6", type: "window", x: 0, y: 5.8, len: 1.4, orient: "v" }] };
  await putLayout(page, flat);
  await page.goto(BASE + "/"); await page.waitForSelector("#walls line", { state: "attached" }); await page.waitForTimeout(300);
  await page.screenshot({ path: `${OUT}/flat-before.png` });
  await page.click("#editToggle"); await page.click("#tidyUp"); await page.waitForTimeout(100);
  await page.screenshot({ path: `${OUT}/flat-tidy-preview.png` });
  const moved = await page.textContent("#status");
  await page.click("#tidyUp"); await save(page); await page.waitForTimeout(300);
  await page.screenshot({ path: `${OUT}/flat-after.png` });
  L = await getLayout(page);
  const kitchen = roomOf(L, "kitchen"), hall = roomOf(L, "hall"), bed = roomOf(L, "bedroom");
  check("flat tidied: kitchen x=6.2, bedroom y=4.8, hall flush", kitchen.x === 6.2 && bed.y === 4.8 && hall.x === bed.x + bed.w, moved);
  check("no page errors", errors.length === 0, errors.join("; "));
  await ctx.close();
}

async function phone(browser) {
  console.log("--- phone (touch, 390x844)");
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  const errors = []; page.on("pageerror", (e) => errors.push(e.message));
  const cdp = await ctx.newCDPSession(page);
  await login(page);
  await freshEdit(page, { unit: "m", rooms: [room("lounge", 0, 0, 4, 3), room("bed", 8, 0.5, 3, 3)], placements: [] });
  console.log(`snap threshold at this zoom: ${(await page.evaluate(() => snapThr())).toFixed(3)} m`);
  const wrap = await page.evaluate(() => { const b = document.getElementById("editbar"); return { h: b.offsetHeight, sw: b.scrollWidth, cw: b.clientWidth }; });
  check("edit toolbar wraps without horizontal overflow", wrap.sw <= wrap.cw, JSON.stringify(wrap));
  await page.screenshot({ path: `${OUT}/phone-edit.png` });
  // move
  const g = await tdrag(page, cdp, [9, 1.5], [9 - 3.8, 1.5 - 0.4], () => guides(page));
  const b = await draftRoom(page, "bed");
  check("touch: moved room snaps flush (x=4, y=0)", b.x === 4 && b.y === 0 && g >= 1, JSON.stringify(b) + ` guides=${g}`);
  check("touch: guides gone after release", (await guides(page)) === 0);
  // draw below the lounge
  await page.click("#addRoom");
  await tdrag(page, cdp, [0.1, 3.15], [3.8, 5.5]);
  await nameRoom(page, "Bath");
  await save(page);
  const L = await getLayout(page), bath = L.rooms.find((r) => r.name === "Bath");
  check("touch: drawn room snaps (x=0, y=3, right edge 4)", bath && bath.x === 0 && bath.y === 3 && near(bath.x + bath.w, 4), JSON.stringify(bath));
  // tidy on the phone
  await freshEdit(page, { unit: "m", rooms: [room("lounge", 0, 0, 4, 3), room("bed", 4.15, 0, 3, 3)], placements: [] });
  await page.tap("#tidyUp"); await page.tap("#tidyUp"); await save(page);
  check("touch: tidy up closes 0.15 m gap", roomOf(await getLayout(page), "bed").x === 4);
  await page.screenshot({ path: `${OUT}/phone-view.png` });
  check("phone: no page errors", errors.length === 0, errors.join("; "));
  await ctx.close();
}

(async () => {
  const browser = await chromium.launch();
  try { await desktop(browser); await phone(browser); }
  catch (e) { console.log("ERROR", e); fails++; }
  finally { await browser.close(); }
  console.log(fails ? `${fails} FAILED` : "ALL PASSED");
  process.exit(fails ? 1 : 0);
})();
