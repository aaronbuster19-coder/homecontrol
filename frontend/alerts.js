"use strict";
// Door-open push alerts: per-device subscription + shared settings (sheet opened from the header bell).
(() => {
  const el = (id) => document.getElementById(id);
  const sheet = el("alertSheet");
  const isIOS = /iP(hone|ad|od)/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  const supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

  async function call(path, body, method = "POST") {
    const r = await fetch(path, { method, headers: { "Content-Type": "application/json" }, body: body && JSON.stringify(body) });
    if (r.status === 401) { location.replace("/login.html"); throw new Error("signed out"); }
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || r.statusText);
    return d;
  }
  const msg = (text, warn = false) => { el("alertMsg").textContent = text; el("alertMsg").classList.toggle("warn", warn); };
  const b64ToBytes = (s) => Uint8Array.from(atob((s + "=".repeat(-s.length & 3)).replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
  const currentSub = async () => supported ? (await navigator.serviceWorker.ready).pushManager.getSubscription() : null;

  async function refresh() {
    let sub = null;
    el("alertOn").disabled = el("alertOff").disabled = true;
    if (!supported) {
      msg(isIOS && !standalone
        ? "On iPhone/iPad, alerts need the app on your Home Screen: tap Share → Add to Home Screen, open it from there, then come back here."
        : "This browser doesn't support push notifications.", true);
    } else if (Notification.permission === "denied") {
      msg("Notifications are blocked for this site. Re-allow them in the browser's site settings (padlock icon → Notifications), then reopen this.", true);
    } else {
      try { sub = await currentSub(); } catch {}
      msg(sub ? "This device gets alerts." : "This device doesn't get alerts yet.");
      el("alertOn").disabled = !!sub; el("alertOff").disabled = !sub;
    }
    try {
      const s = await call("/api/alerts/settings", undefined, "GET");
      el("alertEnabled").checked = s.enabled; el("alertMins").value = s.door_open_minutes; el("alertOnClose").checked = s.notify_on_close;
    } catch (e) { msg(`Settings: ${e.message}`, true); }
  }

  async function enable() {
    try {
      if (await Notification.requestPermission() !== "granted") return refresh();
      const { publicKey } = await call("/api/push/key", undefined, "GET");
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(publicKey) });
      await call("/api/push/subscribe", sub.toJSON());
      await refresh();
    } catch (e) { msg(`Couldn't enable alerts: ${e.message}`, true); }
  }

  async function disable() {
    try {
      const sub = await currentSub();
      if (sub) { await call("/api/push/unsubscribe", { endpoint: sub.endpoint }); await sub.unsubscribe(); }
      await refresh();
    } catch (e) { msg(`Couldn't disable alerts: ${e.message}`, true); }
  }

  async function save() {
    const mins = Math.round(Number(el("alertMins").value));
    if (!(mins >= 1 && mins <= 120)) return msg("Minutes must be 1–120.", true);
    try {
      await call("/api/alerts/settings", { enabled: el("alertEnabled").checked, door_open_minutes: mins,
        notify_on_close: el("alertOnClose").checked }, "PUT");
    } catch (e) { msg(`Couldn't save: ${e.message}`, true); }
  }

  async function test() {
    try {
      const r = await call("/api/push/test");
      msg(r.sent ? `Test sent to ${r.sent} device${r.sent > 1 ? "s" : ""}.` : "No device received it — enable alerts on a device first.", !r.sent);
    } catch (e) { msg(`Test failed: ${e.message}`, true); }
  }

  el("alertsBtn").onclick = () => { sheet.hidden = false; refresh(); };
  el("alertClose").onclick = () => { sheet.hidden = true; };
  sheet.addEventListener("click", (e) => { if (e.target === sheet) sheet.hidden = true; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") sheet.hidden = true; });
  el("alertOn").onclick = enable;
  el("alertOff").onclick = disable;
  el("alertTest").onclick = test;
  ["alertEnabled", "alertMins", "alertOnClose"].forEach((id) => el(id).addEventListener("change", save));
})();
