/*
 * Darslik Studiyasi - Service Worker
 *
 * Faqat sahifa qobig'ini (HTML/manifest/ikonkalar) keshlaydi - planshetda ilova
 * sifatida tezroq ochilishi va internet vaqtincha uzilganda ham sahifa umuman
 * ochilmay qolmasligi uchun. /api/ so'rovlariga HECH QACHON tegilmaydi (video
 * holati, progress va h.k. doim jonli - server bilan to'g'ridan-to'g'ri gaplashishi
 * shart), shuning uchun bu ilovaning asosiy funksiyalari internetsiz ishlamaydi.
 */
const CACHE_NAME = "darslik-studiyasi-shell-v1";
const APP_SHELL = ["/", "/manifest.json", "/icons/icon-192.png", "/icons/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  if (req.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) {
    return; // API va boshqa manba so'rovlari to'g'ridan-to'g'ri tarmoqqa boradi
  }

  event.respondWith(
    caches.match(req).then((cached) => {
      const network = fetch(req)
        .then((resp) => {
          if (resp && resp.status === 200) {
            const clone = resp.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(req, clone));
          }
          return resp;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});
