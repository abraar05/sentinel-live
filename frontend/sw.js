// Offline shell only. Stream traffic must never be cached - a stale CCTV
// frame is worse than no frame at all.
const CACHE = 'sentinel-shell-v1';
const SHELL = [
  '/',
  '/app/styles.css',
  '/app/app.js',
  '/app/player.js',
  '/app/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET') return;
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws/')) return;

  event.respondWith(
    caches.match(event.request).then((hit) => hit || fetch(event.request))
      .catch(() => caches.match('/'))
  );
});