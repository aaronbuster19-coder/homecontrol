"use strict";
// Plan zoom on desktops and laptops: a trackpad pinch or Ctrl + mouse wheel over the plan zooms the plan around the
// pointer instead of the whole page (the browser's own page zoom). Phones and tablets keep their own pinch.
// While zoomed, a two-finger scroll (or the wheel) pans the plan; zooming back out, or the button, resets it.
// The zoom sits on top of whatever the plan shows (the whole home, edit mode, a room view) and resets when that changes.
// Hooks from app.js: planZoomBox() (computeViewBox), resetPlanZoom() (edit mode); roomview.js resets it on open/close.
(() => {
  const MAX = 8;                    // up to 8x the normal view
  const wrap = $("planWrap"), btn = $("zoomReset");
  // k = zoom factor (1 = off); u, v = the view's centre as a fraction of the unzoomed view box.
  const z = { k: 1, u: 0.5, v: 0.5, base: null };

  // app.js computeViewBox(): the unzoomed view box in, the zoomed one out.
  function planZoomBox(vb) {
    z.base = vb;
    if (z.k <= 1) return vb;
    const [X, Y, W, H] = vb, w = W / z.k, h = H / z.k;
    const x = clampNum(X + z.u * W - w / 2, X, X + W - w), y = clampNum(Y + z.v * H - h / 2, Y, Y + H - h);
    z.u = (x + w / 2 - X) / W; z.v = (y + h / 2 - Y) / H; // remember the clamped centre, so panning back is immediate
    return [x, y, w, h];
  }
  const clampNum = (n, lo, hi) => Math.min(hi, Math.max(lo, n));

  function resetPlanZoom(redraw = false) {
    const was = z.k > 1;
    z.k = 1; z.u = z.v = 0.5;
    btn.hidden = true;
    if (redraw && was) render();
  }

  // Zoom by `factor` keeping the plan point under (clientX, clientY) where it is on screen.
  function zoomAt(factor, clientX, clientY) {
    if (!z.base || st.drag) return;
    const k = clampNum(z.k * factor, 1, MAX);
    if (k === z.k) return;
    if (k <= 1.001) { resetPlanZoom(true); return; }
    const [X, Y, W, H] = z.base, vb = st.viewBox, p = svgPoint(clientX, clientY);
    // Same aspect ratio before and after, so the point keeps its place when it keeps its fraction of the view.
    const fx = (p.x - vb[0]) / vb[2], fy = (p.y - vb[1]) / vb[3], w = W / k, h = H / k;
    z.k = k; z.u = (p.x - fx * w + w / 2 - X) / W; z.v = (p.y - fy * h + h / 2 - Y) / H;
    btn.hidden = false;
    render();
  }

  function panBy(dxPx, dyPx) {
    if (!z.base) return;
    const mpp = Math.max(st.viewBox[2] / (svg.clientWidth || 800), st.viewBox[3] / (svg.clientHeight || 600));
    z.u += dxPx * mpp / z.base[2]; z.v += dyPx * mpp / z.base[3];
    render();
  }

  // Wheel: a trackpad pinch arrives as a wheel event with ctrlKey set (Chrome, Edge, Firefox), as does Ctrl + wheel.
  wrap.addEventListener("wheel", (e) => {
    if (!overSvg(e.clientX, e.clientY)) return;
    const px = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1; // lines / pages to pixels
    if (e.ctrlKey) {
      e.preventDefault(); // no page zoom
      zoomAt(Math.exp(-e.deltaY * px * 0.01), e.clientX, e.clientY);
    } else if (z.k > 1) {
      e.preventDefault(); // pan the plan, not the page
      panBy(e.deltaX * px, e.deltaY * px);
    }
  }, { passive: false });

  // Safari on a Mac sends its own gesture events for a trackpad pinch (and no ctrl-wheel). Only with a fine pointer,
  // so an iPad or iPhone pinch is left alone.
  if (matchMedia("(hover: hover) and (pointer: fine)").matches) {
    let last = 1;
    wrap.addEventListener("gesturestart", (e) => { e.preventDefault(); last = 1; });
    wrap.addEventListener("gesturechange", (e) => { e.preventDefault(); zoomAt(e.scale / last, e.clientX, e.clientY); last = e.scale; });
    wrap.addEventListener("gestureend", (e) => e.preventDefault());
  }

  btn.addEventListener("click", () => resetPlanZoom(true));

  window.planZoomBox = planZoomBox;
  window.resetPlanZoom = resetPlanZoom;
})();
