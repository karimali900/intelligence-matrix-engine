const CACHE = "matrix-v1";

self.addEventListener("install", (e) => {
    e.waitUntil(caches.open(CACHE).then((c) => c.addAll(["/", "/static/manifest.json"])));
    self.skipWaiting();
});

self.addEventListener("activate", (e) => {
    e.waitUntil(clients.claim());
});

self.addEventListener("fetch", (e) => {
    e.respondWith(
        caches.match(e.request).then((cached) => cached || fetch(e.request).then((res) => {
            if (res.status === 200 && e.request.method === "GET") {
                const clone = res.clone();
                caches.open(CACHE).then((c) => c.put(e.request, clone));
            }
            return res;
        }))
    );
});
