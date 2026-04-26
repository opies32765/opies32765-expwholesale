const CACHE_NAME = 'ew-field-v1';
const STATIC_ASSETS = [
  '/mobile',
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/icon.svg'
];

// Install — pre-cache static shell
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

// Activate — clean old caches
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Fetch — network-first for pages, skip API/POST entirely
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // Let API calls and non-GET pass through to network (no cache involvement)
  if (event.request.method !== 'GET' || url.pathname.startsWith('/api/')) {
    return;
  }

  // Partner portal + admin pages must never be intercepted or cached by
  // this service worker. The SW is installed for the field-rep /mobile
  // PWA and should not hijack dealer-facing or admin URLs.
  if (url.pathname.startsWith('/partner/')
      || url.pathname.startsWith('/admin/partner/')) {
    return;
  }

  event.respondWith(
    fetch(event.request)
      .then(response => {
        if (response.ok) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
        }
        return response;
      })
      .catch(() =>
        caches.match(event.request).then(cached => {
          if (cached) return cached;
          // Offline navigation → serve cached /mobile shell
          if (event.request.mode === 'navigate') {
            return caches.match('/mobile');
          }
          return new Response('Offline', { status: 503 });
        })
      )
  );
});

// Background sync — retry failed submissions when back online
self.addEventListener('sync', event => {
  if (event.tag === 'ew-offline-submit') {
    event.waitUntil(replayOfflineQueue());
  }
});

async function replayOfflineQueue() {
  const db = await openDB();
  const tx = db.transaction('offline_queue', 'readonly');
  const store = tx.objectStore('offline_queue');
  const all = await idbGetAll(store);

  for (const entry of all) {
    try {
      // Rebuild FormData from stored fields
      const fd = new FormData();
      for (const [key, value] of Object.entries(entry.fields || {})) {
        fd.append(key, value);
      }
      // Re-attach stored blobs
      for (const photo of (entry.photos || [])) {
        fd.append(photo.name, new Blob([photo.data], { type: photo.type }), photo.filename);
      }

      const resp = await fetch(entry.url, { method: 'POST', body: fd });
      if (resp.ok) {
        const delTx = db.transaction('offline_queue', 'readwrite');
        delTx.objectStore('offline_queue').delete(entry.id);
        // Notify the app
        const clients = await self.clients.matchAll();
        clients.forEach(client => {
          client.postMessage({ type: 'sync-success', id: entry.id, data: entry.fields });
        });
      }
    } catch (e) {
      // Still offline — will retry next sync event
    }
  }
}

// Push notifications
self.addEventListener('push', event => {
  const data = event.data ? event.data.json() : {};
  event.waitUntil(
    self.registration.showNotification(data.title || 'EW Field App', {
      body: data.body || 'You have an update',
      icon: '/static/icons/icon-192.png',
      badge: '/static/icons/icon-192.png',
      tag: data.tag || 'ew-notification',
      data: { url: data.url || '/mobile' }
    })
  );
});

// Notification click — open/focus app
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) || '/mobile';
  event.waitUntil(
    self.clients.matchAll({ type: 'window' }).then(clients => {
      for (const client of clients) {
        if (client.url.includes('/mobile') && 'focus' in client) {
          return client.focus();
        }
      }
      return self.clients.openWindow(targetUrl);
    })
  );
});

// ── IndexedDB helpers ──
function openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open('ew_offline', 1);
    req.onupgradeneeded = () => {
      if (!req.result.objectStoreNames.contains('offline_queue')) {
        req.result.createObjectStore('offline_queue', { keyPath: 'id', autoIncrement: true });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function idbGetAll(store) {
  return new Promise((resolve, reject) => {
    const req = store.getAll();
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
