// Caches only the static app shell. API calls, auth and live data always go to
// the network: only same-origin GET requests for static files are ever stored.
const CACHE = "argus-shell-v1";
const SHELL = ["/offline.html", "/site.webmanifest", "/pwa-192.png", "/pwa-512.png"];
const NEVER_CACHE = ["/api/", "/healthz", "/auth", "/login", "/oauth"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (NEVER_CACHE.some((p) => url.pathname === p.replace(/\/$/, "") || url.pathname.startsWith(p)))
    return;

  if (request.mode === "navigate") {
    // Always the network; the offline notice only when it is unreachable.
    event.respondWith(fetch(request).catch(() => caches.match("/offline.html")));
    return;
  }
  // Content-hashed build output never changes, so it is safe to cache.
  if (url.pathname.startsWith("/assets/") || SHELL.includes(url.pathname)) {
    event.respondWith(
      caches.match(request).then(
        (hit) =>
          hit ||
          fetch(request).then((response) => {
            if (response.ok) {
              const copy = response.clone();
              caches.open(CACHE).then((cache) => cache.put(request, copy));
            }
            return response;
          }),
      ),
    );
  }
});
