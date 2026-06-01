// Minimal offline support required for robust PWAs
const CACHE_NAME = 'matrix-cache-v1';
const ASSETS_TO_CACHE = [
  '/',
  '/static/css/style.css', // replace with your actual asset paths
  '/static/offline.html'   // a simple offline fallback page
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(ASSETS_TO_CACHE);
    })
  );
});

self.addEventListener('fetch', (event) => {
  event.respondWith(
    caches.match(event.request).then((response) => {
      return response || fetch(event.request).catch(() => {
        // Return fallback page if network fails
        return caches.match('/static/offline.html');
      });
    })
  );
});

// background push listener
self.addEventListener('push', (event) => {
  const data = event.data ? event.data.text() : 'New Matrix Intelligence Update';
  event.waitUntil(
    self.registration.showNotification('Matrix Engine', {
      body: data,
      icon: '/static/icon.png',
      badge: '/static/badge.png'
    })
  );
});
