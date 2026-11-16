// Batsy's service worker: keeps the app's own files so it opens instantly (and offline), shows push notifications,
// and opens Batsy when one is tapped. Live data (/api/...) always goes to the laptop, never from a cache.
const CACHE = 'batsy-shell-v1';
const SHELL = ['/app/', '/app/app.css', '/app/app.js', '/app/manifest.webmanifest', '/app/slice.html',
  '/app/fonts/barlow-condensed-600.woff2', '/app/fonts/barlow-condensed-700.woff2',
  '/app/icons/icon-192.png', '/app/icons/apple-touch-icon.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

// the app's files: the laptop's newest when it's reachable (so changes show up), the saved copy when it isn't
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || !url.pathname.startsWith('/app/') ) return;  // /api/... goes straight through
  e.respondWith(fetch(e.request).then(r => {
    if (r.ok) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); }
    return r;
  }).catch(() => caches.match(e.request, {ignoreSearch: true}).then(r => r || caches.match('/app/'))));
});

self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = {body: e.data && e.data.text()}; }
  e.waitUntil(self.registration.showNotification(d.title || 'Batsy', {
    body: d.body || '', tag: d.tag || 'batsy', renotify: true,
    icon: '/app/icons/icon-192.png', badge: '/app/icons/icon-192.png', data: {url: d.url || '/app/'}}));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/app/';
  e.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(list => {
    const open = list.find(c => new URL(c.url).pathname.startsWith('/app/'));
    return open ? open.focus() : self.clients.openWindow(url);
  }));
});
