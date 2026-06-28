/* Aern Nexus service worker.
 * Goal: make the app feel instant — especially remote over Tailscale DERP — by caching
 * the static shell and external poster art, while keeping page DATA fresh.
 *
 * Strategy per request type:
 *   - /static/*               → stale-while-revalidate (instant, refresh in background)
 *   - TMDB / IGDB posters     → cache-first (kills the slow external-CDN dependency)
 *   - navigations (HTML)      → network-first, fall back to last-good cache when offline
 *   - /api/*  and non-GET     → untouched (always live; never cache writes/state)
 *
 * Bump VERSION to force-evict old caches on next activate. */
const VERSION = 'nexus-v1';
const STATIC_CACHE = `static-${VERSION}`;
const PAGE_CACHE = `pages-${VERSION}`;
const IMG_CACHE = `img-${VERSION}`;
const ALL = [STATIC_CACHE, PAGE_CACHE, IMG_CACHE];

const PRECACHE = [
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE)
      .then((c) => c.addAll(PRECACHE))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => !ALL.includes(k)).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;                     // never intercept writes
  const url = new URL(req.url);

  // External poster/cover art → cache-first, long-lived.
  if (url.hostname.endsWith('tmdb.org') || url.hostname.endsWith('igdb.com')) {
    event.respondWith(cacheFirst(req, IMG_CACHE));
    return;
  }

  if (url.origin !== self.location.origin) return;      // other cross-origin: passthrough
  if (url.pathname.startsWith('/api/')) return;         // app API: always live

  if (url.pathname.startsWith('/static/')) {
    event.respondWith(staleWhileRevalidate(req, STATIC_CACHE));
    return;
  }

  if (req.mode === 'navigate' || (req.headers.get('accept') || '').includes('text/html')) {
    event.respondWith(networkFirst(req, PAGE_CACHE));
    return;
  }
});

function cacheFirst(req, cacheName) {
  return caches.open(cacheName).then((c) =>
    c.match(req).then((hit) =>
      hit || fetch(req).then((res) => { if (res.ok) c.put(req, res.clone()); return res; })
    )
  );
}

function staleWhileRevalidate(req, cacheName) {
  return caches.open(cacheName).then((c) =>
    c.match(req).then((hit) => {
      const fetched = fetch(req)
        .then((res) => { if (res.ok) c.put(req, res.clone()); return res; })
        .catch(() => hit);
      return hit || fetched;
    })
  );
}

function networkFirst(req, cacheName) {
  return caches.open(cacheName).then((c) =>
    fetch(req)
      .then((res) => { if (res.ok) c.put(req, res.clone()); return res; })
      .catch(() => c.match(req).then((hit) => hit || c.match('/nexus')))
  );
}
