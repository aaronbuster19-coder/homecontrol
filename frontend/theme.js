"use strict";
// ⋯ → Theme: Auto (follows the system) / Light / Dark, remembered per device. The inline script in <head> applies it
// before first paint and owns window.hcTheme; this wires up the menu and repaints what JS colours inline.
(() => {
  const sel = $("themeSel");
  if (!window.hcTheme || !sel) return;
  sel.value = hcTheme.mode;
  sel.onchange = () => hcTheme.set(sel.value);
  // Another tab of this device changed it.
  window.addEventListener("storage", (e) => { if (e.key === "hc.theme") { hcTheme.set(e.newValue || "auto"); sel.value = hcTheme.mode; } });
  // Room temperature tints are computed colours (modes.js tempColor): draw them again.
  window.addEventListener("hc-theme", () => { if (typeof render === "function") render(); });
})();
