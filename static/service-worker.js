// Bump on any SW change so the activate handler evicts old caches.
// v2 (2026-05-02): added /owner + /admin skip rules; routed
// notificationclick by URL prefix between /mobile and /owner windows.
const CACHE_NAME = 'ew-field-v2';
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

  // Partner portal, owner portal, and admin pages must never be intercepted
  // or cached by this service worker. The SW is installed for the field-rep
  // /mobile PWA and should not hijack dealer-facing, owner-facing, or admin
  // URLs (they have their own auth, their own no-cache headers, and rely on
  // fresh network responses).
  if (url.pathname.startsWith('/partner/')
      || url.pathname.startsWith('/admin/')
      || url.pathname === '/owner'
      || url.pathname.startsWith('/owner/')) {
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

// Notification click — open/focus the app the notification belongs to.
// Owner pushes carry url=/owner/bid/<id>; rep pushes carry url=/mobile/...;
// pick an existing window matching the same prefix, else open targetUrl.
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) || '/mobile';
  // Pick a focus key that distinguishes owner-portal vs field-rep windows
  // so an owner click never steals focus to an open /mobile rep window.
  let prefix = '/mobile';
  if (targetUrl.startsWith('/owner')) prefix = '/owner';
  event.waitUntil(
    self.clients.matchAll({ type: 'window' }).then(clients => {
      for (const client of clients) {
        try {
          const cu = new URL(client.url);
          if (cu.pathname.startsWith(prefix) && 'focus' in client) {
            // Navigate the existing window to the deep-link target then focus
            if ('navigate' in client && client.url !== targetUrl) {
              return client.navigate(targetUrl).then(() => client.focus());
            }
            return client.focus();
          }
        } catch (e) {}
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
