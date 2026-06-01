// Change this version number (e.g., 'matrix-cache-v2') whenever you push major UI changes
const CACHE_NAME = 'matrix-cache-v1';

const ASSETS_TO_CACHE = [
  '/',
  '/static/manifest.json',
  '/static/Karim.png',
  '/static/offline.html' // Standard offline fallback page
];

// 1. Install Phase: Cache the essential system assets
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(ASSETS_TO_CACHE);
    })
  );
  // Force the waiting service worker to become the active service worker instantly
  self.skipWaiting();
});

// 2. Activate Phase: Drop old caches instantly when CACHE_NAME changes
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((cache) => {
          if (cache !== CACHE_NAME) {
            console.log('Busting old cache: ' + cache);
            return caches.delete(cache);
          }
        })
      );
    })
  );
  // Force active tabs to immediately come under the control of the new service worker
  return self.clients.claim();
});

// 3. Fetch Phase: Serve from cache if available, fallback to network
self.addEventListener('fetch', (event) => {
  event.respondWith(
    caches.match(event.request).then((response) => {
      return response || fetch(event.request).catch(() => {
        // Return fallback page if network completely fails
        return caches.match('/static/offline.html');
      });
    })
  );
});

// 4. Background Push Listener for live matrix updates
self.addEventListener('push', (event) => {
  const data = event.data ? event.data.text() : 'New Matrix Intelligence Update';
  event.waitUntil(
    self.registration.showNotification('Matrix Engine', {
      body: data,
      icon: '/static/Karim.png',
      badge: '/static/Karim.png'
    })
  );
});
