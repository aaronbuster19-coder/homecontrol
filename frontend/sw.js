"use strict";
// Bump VERSION when the shell changes shape; content updates arrive anyway (network-first).
const VERSION = "hc-v2";
const SHELL = ["/", "/index.html", "/app.js", "/floorplan.js", "/style.css", "/login.html", "/login.js", "/manifest.webmanifest",
  "/icons/icon-192.png", "/icons/icon-512.png", "/icons/icon-maskable-512.png", "/icons/apple-touch-icon.png"];
const API_CACHED = ["/api/layout", "/api/devices"];

self.addEventListener("install", (e) => {
  // Shell entries behind login ("/", app.js) fail before sign-in; cache what we can.
  e.waitUntil(caches.open(VERSION).then((c) => Promise.all(SHELL.map((u) =>
    fetch(u, { credentials: "same-origin" }).then((r) => cacheable(r) && c.put(u, r)).catch(() => {}))))
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
    const r = await fetch(req);
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
