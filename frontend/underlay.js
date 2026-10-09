"use strict";
// Floor-plan photo underlay (backend/underlay.py): a photo or scan drawn under the rooms so walls can be traced over
// it. Edit mode → Photo: upload, then move / scale / rotate it (drag, pinch, scroll, or the panel) and set its
// opacity. It is saved as you go, apart from the layout (Save / Cancel don't touch it). Shown in edit mode unless
// "Show while editing" is off (this device); outside edit mode only with "Also show outside edit mode" (everyone).
const UL_MAX_BYTES = 10 * 1024 * 1024, UL_MAX_EDGE = 4000, UL_WIDTH = [0.5, 200], UL_HIDE_KEY = "hc.underlayHidden";
const ul = { data: null, aspect: null, href: null, adjust: false, hidden: false, pending: {}, timer: null, ptrs: new Map(), g0: null };
try { ul.hidden = localStorage.getItem(UL_HIDE_KEY) === "1"; } catch {}

const ulHas = () => !!ul.data?.image;
const ulVisible = () => ulHas() && (st.editing ? !ul.hidden : !!ul.data.show_view);
const ulAspect = () => ul.aspect || ul.data.image.h / ul.data.image.w;
const ulR3 = (v) => Math.round(v * 1000) / 1000;
const ulRot = (r) => { r = ((r % 360) + 360) % 360; return Math.round((r > 180 ? r - 360 : r) * 10) / 10; };

// ---------- drawing (called from render() in app.js) ----------
function renderUnderlay() {
  let g = $("underlay");
  if (!g) {
    g = el("g", { id: "underlay" }); svg.insertBefore(g, $("rooms"));
    el("image", { preserveAspectRatio: "none" }, g);
    el("rect", { class: "ul-outline" }, g);
  }
  if (!st.editing && (ul.adjust || !$("ulPanel")?.hidden)) ulClose(); // left edit mode (Save / Cancel)
  const on = ulVisible();
  svg.classList.toggle("uly-on", on);
  g.style.display = on ? "" : "none";
  if (!on) return;
  const u = ul.data, w = u.width, h = u.width * ulAspect();
  const href = `/api/underlay/image?v=${encodeURIComponent(u.image.version)}`;
  const img = g.querySelector("image");
  if (ul.href !== href) {
    ul.href = href; ul.aspect = null; img.setAttribute("href", href);
    // The browser's decoded size (EXIF rotation applied) decides the shape; the server's numbers are the fallback.
    const probe = new Image();
    probe.onload = () => { if (ul.href === href && probe.naturalWidth) { ul.aspect = probe.naturalHeight / probe.naturalWidth; renderUnderlay(); } };
    probe.src = href;
  }
  for (const n of [img, g.querySelector(".ul-outline")]) {
    n.setAttribute("x", -w / 2); n.setAttribute("y", -h / 2); n.setAttribute("width", w); n.setAttribute("height", h);
  }
  g.setAttribute("transform", `translate(${u.x} ${u.y}) rotate(${u.rot})`);
  img.style.opacity = u.opacity;
  g.classList.toggle("inv", !!u.invert_dark);
  g.classList.toggle("adjusting", ul.adjust);
}
function ulClose() {
  ul.adjust = false; ul.ptrs.clear(); ul.g0 = null; document.body.classList.remove("ul-adjust");
  const p = $("ulPanel"); if (p) { p.hidden = true; $("ulBtn").classList.remove("primary"); $("ulBtn").setAttribute("aria-expanded", "false"); }
}
function ulRender() { if (typeof render === "function") render(); else renderUnderlay(); ulSync(); }

// ---------- server ----------
async function ulLoad() {
  try { ul.data = await api("/api/underlay"); } catch { ul.data = null; }
  ulRender();
}
// Local first, saved after a short pause (and at once at the end of a gesture).
function ulChange(changes, now = false) {
  Object.assign(ul.data, changes); Object.assign(ul.pending, changes);
  renderUnderlay(); ulSync();
  clearTimeout(ul.timer);
  ul.timer = setTimeout(ulFlush, now ? 0 : 400);
}
async function ulFlush() {
  clearTimeout(ul.timer); ul.timer = null;
  const body = ul.pending; ul.pending = {};
  if (!Object.keys(body).length || !ulHas()) return;
  try { const d = await api("/api/underlay", { method: "PUT", body: JSON.stringify(body) }); if (!ul.timer) ul.data = { ...d, ...ul.pending }; }
  catch (e) { setStatus(`Photo: ${e.message}`, true); }
}
// Photos from phones: bake in the EXIF rotation and shrink to UL_MAX_EDGE (also keeps them well under the size limit).
async function ulPrepare(file) {
  const ok = ["image/png", "image/jpeg", "image/webp"].includes(file.type);
  let bmp = null;
  try { bmp = await createImageBitmap(file, { imageOrientation: "from-image" }); } catch {}
  if (!bmp) {
    if (ok && file.size <= UL_MAX_BYTES) return file;
    throw new Error("can't read that image — use a PNG, JPEG or WebP");
  }
  const k = Math.min(1, UL_MAX_EDGE / Math.max(bmp.width, bmp.height));
  if (ok && file.type !== "image/jpeg" && k === 1 && file.size <= UL_MAX_BYTES) { bmp.close?.(); return file; }
  const c = document.createElement("canvas");
  c.width = Math.max(1, Math.round(bmp.width * k)); c.height = Math.max(1, Math.round(bmp.height * k));
  const ctx = c.getContext("2d"); ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, c.width, c.height); // transparent PNG → white paper
  ctx.drawImage(bmp, 0, 0, c.width, c.height); bmp.close?.();
  const blob = await new Promise((res) => c.toBlob(res, "image/jpeg", 0.88));
  if (!blob) throw new Error("couldn't convert that image");
  if (blob.size > UL_MAX_BYTES) throw new Error("that image is too big (10 MB at most)");
  return blob;
}
async function ulUpload(file) {
  if (!file) return;
  setStatus("Uploading photo…");
  try {
    const body = await ulPrepare(file);
    const d = await api("/api/underlay/image", { method: "POST", body, headers: { "Content-Type": body.type } });
    ul.data = d; ul.hidden = false; try { localStorage.removeItem(UL_HIDE_KEY); } catch {}
    if (d.fresh) { ulFit(true); ulSetAdjust(true); } // a first photo: lay it over the plan, ready to line up
    ulRender(); setStatus("Photo uploaded — drag, pinch or scroll to line it up");
  } catch (e) { setStatus(`Photo: ${e.message}`, true); }
}
async function ulRemove() {
  if (!confirm("Remove the floor-plan photo?")) return;
  try { await api("/api/underlay", { method: "DELETE" }); ul.data = { image: null }; ul.pending = {}; ulSetAdjust(false); ulRender(); setStatus("Photo removed"); }
  catch (e) { setStatus(`Photo: ${e.message}`, true); }
}
// Fit inside the rooms drawn so far (or the visible plan when there are none).
function ulFit(now) {
  const rooms = cur().rooms;
  let x0, y0, x1, y1;
  if (rooms.length) {
    x0 = Math.min(...rooms.map((r) => r.x)); y0 = Math.min(...rooms.map((r) => r.y));
    x1 = Math.max(...rooms.map((r) => r.x + r.w)); y1 = Math.max(...rooms.map((r) => r.y + r.h));
  } else { const vb = st.viewBox || [0, 0, 12, 10]; [x0, y0, x1, y1] = [vb[0] + 1, vb[1] + 1, vb[0] + vb[2] - 1, vb[1] + vb[3] - 1]; }
  const a = ulAspect(), width = Math.min(UL_WIDTH[1], Math.max(UL_WIDTH[0], Math.min(x1 - x0, (y1 - y0) / a)));
  ulChange({ x: ulR3((x0 + x1) / 2), y: ulR3((y0 + y1) / 2), width: ulR3(width), rot: 0 }, now);
}

// ---------- moving it on the plan: drag; pinch or scroll to scale; two fingers (or Shift+scroll) to rotate ----------
function ulSetAdjust(on) {
  ul.adjust = !!on && ulHas() && st.editing;
  document.body.classList.toggle("ul-adjust", ul.adjust);
  ul.ptrs.clear(); ul.g0 = null;
  if (ul.adjust) { st.sel = null; st.picked = null; if (st.drawing) setDrawing(false); if (st.adding && typeof setAdding === "function") setAdding(null); }
  if (typeof render === "function") render(); else renderUnderlay();
  ulSync();
}
function ulGestureStart() {
  const pts = [...ul.ptrs.values()];
  ul.g0 = { pts: pts.map((p) => ({ ...p })), x: ul.data.x, y: ul.data.y, width: ul.data.width, rot: ul.data.rot };
}
function ulGestureMove() {
  const g = ul.g0, pts = [...ul.ptrs.values()];
  if (!g || pts.length !== g.pts.length) return;
  if (pts.length === 1) {
    ulChange({ x: ulR3(g.x + pts[0].x - g.pts[0].x), y: ulR3(g.y + pts[0].y - g.pts[0].y) });
    return;
  }
  const [a0, b0] = g.pts, [a, b] = pts;
  const c0 = { x: (a0.x + b0.x) / 2, y: (a0.y + b0.y) / 2 }, c = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
  const d0 = Math.hypot(b0.x - a0.x, b0.y - a0.y) || 1e-6, d = Math.hypot(b.x - a.x, b.y - a.y);
  const k = Math.min(UL_WIDTH[1] / g.width, Math.max(UL_WIDTH[0] / g.width, d / d0));
  const da = Math.atan2(b.y - a.y, b.x - a.x) - Math.atan2(b0.y - a0.y, b0.x - a0.x);
  const vx = g.x - c0.x, vy = g.y - c0.y, cos = Math.cos(da), sin = Math.sin(da);
  ulChange({ x: ulR3(c.x + k * (vx * cos - vy * sin)), y: ulR3(c.y + k * (vx * sin + vy * cos)),
    width: ulR3(g.width * k), rot: ulRot(g.rot + da * 180 / Math.PI) });
}
function ulScaleAt(k, p) { // scale around a plan point
  const u = ul.data, w = Math.min(UL_WIDTH[1], Math.max(UL_WIDTH[0], u.width * k)); k = w / u.width;
  ulChange({ width: ulR3(w), x: ulR3(p.x + (u.x - p.x) * k), y: ulR3(p.y + (u.y - p.y) * k) });
}
(() => {
  const wrap = $("planWrap"), active = () => ul.adjust && st.editing && ulHas();
  wrap.addEventListener("pointerdown", (e) => {
    if (!active() || e.button > 0) return;
    e.stopPropagation(); e.preventDefault(); // the room / device handlers on the plan stay out of it
    try { wrap.setPointerCapture(e.pointerId); } catch {}
    if (ul.ptrs.size >= 2) return;
    ul.ptrs.set(e.pointerId, svgPoint(e.clientX, e.clientY)); ulGestureStart();
  }, true);
  wrap.addEventListener("pointermove", (e) => {
    if (!active() || !ul.ptrs.has(e.pointerId)) return;
    e.stopPropagation();
    ul.ptrs.set(e.pointerId, svgPoint(e.clientX, e.clientY)); ulGestureMove();
  }, true);
  const up = (e) => {
    if (!ul.ptrs.has(e.pointerId)) return;
    e.stopPropagation();
    ul.ptrs.delete(e.pointerId);
    if (ul.ptrs.size) ulGestureStart(); else { ul.g0 = null; ulFlush(); }
  };
  wrap.addEventListener("pointerup", up, true);
  wrap.addEventListener("pointercancel", up, true);
  for (const t of ["click", "dblclick"]) wrap.addEventListener(t, (e) => { if (active()) e.stopPropagation(); }, true);
  wrap.addEventListener("wheel", (e) => {
    if (!active()) return;
    e.preventDefault(); e.stopPropagation();
    const step = Math.max(-1, Math.min(1, -e.deltaY / 100));
    if (e.shiftKey) ulChange({ rot: ulRot(ul.data.rot + step * 1) });
    else ulScaleAt(Math.pow(1.06, step), svgPoint(e.clientX, e.clientY));
  }, { passive: false, capture: true });
})();

// ---------- toolbar button and panel ----------
function ulPanel(on) {
  const p = $("ulPanel"); if (!p) return;
  p.hidden = !on; $("ulBtn").classList.toggle("primary", on); $("ulBtn").setAttribute("aria-expanded", on ? "true" : "false");
  if (!on && ul.adjust) ulSetAdjust(false);
  if (on) ulSync();
}
function ulSync() {
  const p = $("ulPanel"); if (!p || p.hidden) return;
  const has = ulHas(), u = ul.data || {};
  $("ulEmpty").hidden = has; $("ulCtl").hidden = !has;
  if (!has) return;
  $("ulAdjust").classList.toggle("primary", ul.adjust); $("ulAdjust").setAttribute("aria-pressed", ul.adjust ? "true" : "false");
  $("ulAdjust").textContent = ul.adjust ? "Done moving" : "Move on plan";
  $("ulHint").hidden = !ul.adjust;
  const op = Math.round(u.opacity * 100);
  if (document.activeElement !== $("ulOpacity")) $("ulOpacity").value = op;
  $("ulOpacityV").textContent = `${op} %`;
  if (document.activeElement !== $("ulRotR")) $("ulRotR").value = u.rot;
  if (document.activeElement !== $("ulRotN")) $("ulRotN").value = u.rot;
  if (document.activeElement !== $("ulWidth")) $("ulWidth").value = +toDisp(u.width).toFixed(2);
  $("ulWidthU").textContent = unit();
  $("ulShowEdit").checked = !ul.hidden;
  $("ulShowView").checked = !!u.show_view;
  $("ulInvert").checked = !!u.invert_dark;
}
(() => {
  const h = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstChild; };
  const btn = h(`<button id="ulBtn" type="button" title="Floor-plan photo to trace over" aria-label="Floor-plan photo" aria-expanded="false"><svg class="ul-ic" width="16" height="14" viewBox="0 0 16 14" aria-hidden="true"><rect x="1" y="1" width="14" height="12" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M3 11l3.5-4 2.5 3 1.5-1.5L13 11z" fill="currentColor"/><circle cx="11" cy="4.5" r="1.3" fill="currentColor"/></svg><span class="lbl"> Photo</span></button>`);
  ($("tidyUndo") || $("tidyUp") || $("deleteSel")).after(btn);
  const panel = h(`<div id="ulPanel" class="ul-panel" role="dialog" aria-label="Floor-plan photo" hidden>
    <div class="ul-head"><h3>Floor-plan photo</h3><button type="button" class="ul-close" id="ulClose" aria-label="Close">×</button></div>
    <div id="ulEmpty">
      <p class="hint">Upload a photo or scan of the floor plan, line it up, then trace the rooms over it. PNG, JPEG or WebP, up to 10 MB.</p>
      <button type="button" id="ulUpload" class="primary">Upload photo…</button>
    </div>
    <div id="ulCtl" hidden>
      <div class="ul-btns"><button type="button" id="ulAdjust" aria-pressed="false">Move on plan</button><button type="button" id="ulFit">Fit to plan</button></div>
      <p class="hint" id="ulHint" hidden>Drag to move · pinch or scroll to scale · two fingers or Shift+scroll to rotate</p>
      <div class="ul-more">
        <label class="ul-row"><span>Opacity</span><input type="range" id="ulOpacity" min="5" max="100" step="1"><output id="ulOpacityV"></output></label>
        <label class="ul-row"><span>Rotate</span><input type="range" id="ulRotR" min="-180" max="180" step="0.5"><input type="number" id="ulRotN" min="-180" max="180" step="0.5" inputmode="decimal" aria-label="Rotation in degrees"></label>
        <label class="ul-row"><span>Width</span><input type="number" id="ulWidth" step="0.1" min="0.5" max="200" inputmode="decimal"><span id="ulWidthU" class="ul-u"></span></label>
        <label class="ul-check"><input type="checkbox" id="ulShowEdit"> Show while editing <small>(this device)</small></label>
        <label class="ul-check"><input type="checkbox" id="ulShowView"> Also show outside edit mode</label>
        <label class="ul-check"><input type="checkbox" id="ulInvert"> Invert colours in the dark theme</label>
        <div class="ul-btns"><button type="button" id="ulReplace">Replace…</button><button type="button" id="ulRemove" class="ul-danger">Remove</button></div>
        <p class="hint ul-note">Saved as you go — Save and Cancel only affect the rooms.</p>
      </div>
    </div>
    <input type="file" id="ulFile" accept="image/png,image/jpeg,image/webp,image/*" hidden>
  </div>`);
  document.body.appendChild(panel);
  if (matchMedia("(pointer: coarse)").matches) $("ulHint").textContent = "Drag to move · pinch to scale and turn";

  btn.onclick = () => ulPanel(panel.hidden);
  $("ulClose").onclick = () => ulPanel(false);
  const pick = () => { $("ulFile").value = ""; $("ulFile").click(); };
  $("ulUpload").onclick = pick; $("ulReplace").onclick = pick;
  $("ulFile").onchange = (e) => ulUpload(e.target.files[0]);
  $("ulRemove").onclick = ulRemove;
  $("ulAdjust").onclick = () => ulSetAdjust(!ul.adjust);
  $("ulFit").onclick = () => ulFit(true);
  $("ulOpacity").oninput = (e) => ulChange({ opacity: Math.min(1, Math.max(0.05, +e.target.value / 100)) });
  $("ulRotR").oninput = (e) => ulChange({ rot: ulRot(+e.target.value) });
  $("ulRotN").onchange = (e) => { if (e.target.value !== "" && isFinite(+e.target.value)) ulChange({ rot: ulRot(+e.target.value) }, true); };
  $("ulWidth").onchange = (e) => {
    const w = fromDisp(+e.target.value);
    if (w > 0) ulChange({ width: ulR3(Math.min(UL_WIDTH[1], Math.max(UL_WIDTH[0], w))) }, true);
  };
  $("ulShowEdit").onchange = (e) => {
    ul.hidden = !e.target.checked;
    try { if (ul.hidden) localStorage.setItem(UL_HIDE_KEY, "1"); else localStorage.removeItem(UL_HIDE_KEY); } catch {}
    if (ul.hidden && ul.adjust) ulSetAdjust(false); else renderUnderlay();
  };
  $("ulShowView").onchange = (e) => ulChange({ show_view: e.target.checked }, true);
  $("ulInvert").onchange = (e) => ulChange({ invert_dark: e.target.checked }, true);
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || panel.hidden || document.querySelector("dialog[open]")) return;
    if (ul.adjust) ulSetAdjust(false); else ulPanel(false);
  });
  document.addEventListener("DOMContentLoaded", ulLoad);
})();
