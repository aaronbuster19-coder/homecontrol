"use strict";
// Bump VERSION when the shell changes shape; content updates arrive anyway (network-first).
const VERSION = "hc-v17";
const SHELL = ["/", "/index.html", "/app.js", "/floorplan.js", "/controls.js", "/alerts.js", "/modes.js", "/history.js", "/automations.js", "/style.css", "/login.html", "/login.js", "/manifest.webmanifest", "/wall.js", "/snap.js", "/energy.js", "/schedules.js", "/quiet.js", "/dehumidifier.js", "/roomview.js", "/furniture.js", "/appliances.js", "/presence.js", "/standby.js", "/tv.js", "/tv.css",
  "/icons/icon-192.png", "/icons/icon-512.png", "/icons/icon-maskable-512.png", "/icons/apple-touch-icon.png"];
const API_CACHED = ["/api/layout", "/api/devices"];

self.addEventListener("install", (e) => {
  // Shell entries behind login ("/", app.js) fail before sign-in; cache what we can.
  e.waitUntil(caches.open(VERSION).then((c) => Promise.all(SHELL.map((u) =>
    fetch(u, { credentials: "same-origin", cache: "no-cache" }).then((r) => cacheable(r) && c.put(u, r)).catch(() => {}))))
    .then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

const cacheable = (r) => r && r.status === 200 && !r.redirected && r.type === "basic";

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  const p = url.pathname;
  if (p.startsWith("/api/") && !API_CACHED.includes(p)) return;  // never cache events/login/logout/etc.
  if (req.headers.get("accept") === "text/event-stream") return;
  const key = req.mode === "navigate" ? (p === "/index.html" ? "/" : p) : p;
  if (req.mode !== "navigate" && !API_CACHED.includes(p) && !SHELL.includes(p)) return;
  e.respondWith(networkFirst(req, key));
});

async function networkFirst(req, key) {
  const cache = await caches.open(VERSION);
  try {
    // no-cache: always revalidate with the server, never take a stale copy from the browser's HTTP cache
    const r = await fetch(req, { cache: "no-cache" });
    if (cacheable(r)) cache.put(key, r.clone());
    return r;
  } catch (err) {
    const hit = await cache.match(key);
    if (hit) {
      const h = new Headers(hit.headers);
      h.set("X-From-SW-Cache", "1");
      return new Response(hit.body, { status: hit.status, statusText: hit.statusText, headers: h });
    }
    throw err;
  }
}

// ---------- push notifications (door alerts) ----------
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(d.title || "homecontrol", {
    body: d.body || "", tag: d.tag || "homecontrol", renotify: !!d.tag,
    icon: "/icons/icon-192.png", badge: "/icons/icon-192.png", data: { url: d.url || "/", entity_id: d.entity_id || null },
    actions: Array.isArray(d.actions) ? d.actions.slice(0, 2) : [], // e.g. "Turn off" on a heater left on
  }));
});

function openApp(url) {
  return self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
    const w = wins.find((c) => new URL(c.url).origin === location.origin);
    if (w) w.postMessage({ type: "open", url }); // e.g. "/?summary" or "/?dev=switch.heater" in the running app
    return w ? w.focus() : self.clients.openWindow(url);
  });
}

// "Turn off" on a left-on reminder: only ever switches OFF, with the session cookie. Signed out (or failing): open
// the app on that device's sheet instead. Nothing is switched without this explicit tap.
function turnOff(n) {
  const eid = n.data.entity_id, url = new URL(n.data.url || "/", location.origin).href;
  return fetch(`/api/devices/${encodeURIComponent(eid)}/turn_off`, { method: "POST", credentials: "same-origin" })
    .then((r) => r.ok
      ? self.registration.showNotification(n.title.replace(/ has been on for .*$/, "") + " turned off",
        { tag: n.tag, icon: "/icons/icon-192.png", badge: "/icons/icon-192.png", data: { url } })
      : openApp(url))
    .catch(() => openApp(url));
}

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const n = e.notification, data = n.data || {};
  if (e.action === "off" && data.entity_id) { e.waitUntil(turnOff(n)); return; }
  e.waitUntil(openApp(new URL(data.url || "/", location.origin).href));
});
