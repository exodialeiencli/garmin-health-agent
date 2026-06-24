// Service Worker minimal — cache le shell de l'app pour un démarrage instantané.
// Les données (historique.json) sont TOUJOURS récupérées en réseau (jamais mises en cache).
const CACHE = "esm2028-v1";
const SHELL = ["./index.html", "./manifest.json", "./icon-192.png", "./icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = e.request.url;
  // Données dynamiques : toujours réseau d'abord
  if (url.includes("historique.json") || url.includes("api.github.com") || url.includes("raw.githubusercontent")) {
    e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
    return;
  }
  // Shell : cache d'abord
  e.respondWith(caches.match(e.request).then((r) => r || fetch(e.request)));
});
