// Notifications for the website (reminders, research ready, "Batsy needs a yes or no").
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data.json(); } catch { d = {title: 'Batsy', body: e.data ? e.data.text() : ''}; }
  e.waitUntil(self.registration.showNotification(d.title || 'Batsy',
    {body: d.body || '', tag: d.tag || 'batsy', data: {url: d.url || '/web/'}, icon: '/app/icons/icon-192.png'}));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/web/';
  e.waitUntil(clients.matchAll({type: 'window'}).then(list => {
    for (const c of list) if (c.url.includes('/web/')) { c.navigate(url); return c.focus(); }
    return clients.openWindow(url);
  }));
});
