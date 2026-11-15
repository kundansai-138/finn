// FINN service worker: installable, fast to open, readable offline, and the receiver for push alerts.
//
//   app shell (html, js, css, icons)  cache-first, refreshed in the background
//   /api/radar, /api/brief, /api/portfolio, /api/network, /api/people/todo  network-first; offline → last good copy, flagged as offline
//   /api/company/*                    stale-while-revalidate (dossiers change slowly)
//   everything else (chat stream, writes)  network only
const VERSION = "finn-v3";
const SHELL = ["/", "/static/app.js", "/static/styles.css", "/static/vendor/preact-htm.module.js",
  "/manifest.webmanifest", "/static/icons/icon-192.png", "/static/icons/icon-512.png", "/static/icons/apple-touch-icon.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

const stamp = async (resp) => {
  // Mark a cached API response so the UI can say "offline · as of 08:42".
  const body = await resp.clone().text();
  const h = new Headers(resp.headers);
  h.set("X-FINN-Offline", "1");
  return new Response(body, { status: 200, headers: h });
};

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  const path = url.pathname;

  if (path.startsWith("/api/radar") || path.startsWith("/api/brief") || path.startsWith("/api/portfolio") || path.startsWith("/api/network") || path.startsWith("/api/people/todo")) {
    e.respondWith(fetch(e.request).then((r) => {
      if (r.ok) caches.open(VERSION).then((c) => c.put(e.request, r.clone()));
      return r;
    }).catch(async () => {
      const hit = await caches.match(e.request);
      return hit ? stamp(hit) : new Response(JSON.stringify({ detail: "offline" }), { status: 503, headers: { "Content-Type": "application/json" } });
    }));
    return;
  }
  if (path.startsWith("/api/company/")) {
    e.respondWith(caches.open(VERSION).then(async (c) => {
      const hit = await c.match(e.request);
      const net = fetch(e.request).then((r) => { if (r.ok) c.put(e.request, r.clone()); return r; }).catch(() => null);
      return hit ? (net, hit) : (await net) || new Response(JSON.stringify({ detail: "offline" }), { status: 503 });
    }));
    return;
  }
  if (path.startsWith("/api/") || path === "/sw.js") return;   // live only
  // App shell: cache first, refresh in the background.
  e.respondWith(caches.open(VERSION).then(async (c) => {
    const key = path === "/" || !path.includes(".") ? "/" : e.request;
    const hit = await c.match(key);
    const net = fetch(e.request).then((r) => { if (r.ok && r.type === "basic") c.put(key, r.clone()); return r; }).catch(() => hit);
    return hit || net;
  }));
});

// ---- Push: alerts arrive here even when the app is closed -------------------------
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { title: "FINN", body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(d.title || "FINN", {
    body: d.body || "", tag: d.tag || undefined, renotify: !!d.tag, data: { url: d.url || "/" },
    icon: "/static/icons/icon-192.png", badge: "/static/icons/icon-192.png",
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const target = new URL(e.notification.data?.url || "/", location.origin).href;
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
    const w = wins.find((x) => x.url.startsWith(location.origin));
    if (w) { w.navigate(target); return w.focus(); }
    return self.clients.openWindow(target);
  }));
});
