// Batsy's phone app, StandBy style: swipe sideways between widgets / big clock / now playing; in the widgets view
// each half scrolls up and down through its own stack. Served by the laptop (batsy/phone.py) and shown full screen
// by the Android app, which passes the pairing token in the URL fragment (#t=...), so it never reaches a log.
'use strict';
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'})[c]);

// --- token, link ------------------------------------------------------------------------------------------
// the iPhone (no Android app around it) gets the dashboard layout: cards to scroll, no clock
if (/iPhone|iPad|iPod/.test(navigator.userAgent) && !window.BatsyApp || /[?&]layout=dash/.test(location.search)) {
  document.body.classList.add('dash');
}
const store = {
  get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};
(() => {
  const h = new URLSearchParams(location.hash.slice(1));
  if (h.get('t')) store.set('token', h.get('t'));
  if (h.get('via')) store.set('via', h.get('via'));
  if (h.get('focus') === 'ask') addEventListener('load', () => openSheet());  // the home-screen widget's Ask button
  if (location.hash) history.replaceState(null, '', location.pathname);
})();

async function api(path, body, timeout = 10000) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeout);
  try {
    const r = await fetch(path, {method: body ? 'POST' : 'GET', signal: ctl.signal,
      headers: {'Authorization': 'Bearer ' + store.get('token', ''), ...(body ? {'Content-Type': 'application/json'} : {})},
      body: body ? JSON.stringify(body) : undefined});
    if (r.status === 401) { needPairing('Batsy doesn’t know this phone any more: pair again.'); throw new Error('Not paired'); }
    if (!r.ok) {  // Batsy's own words when it gave any ("The PC is off or asleep..."), else the status
      const j = await r.clone().json().catch(() => ({}));
      throw Object.assign(new Error(j.say || j.error || 'HTTP ' + r.status), {status: r.status});
    }
    setLink(true);
    return await r.json();
  } catch (e) {
    if (e.name === 'AbortError') e = new Error('Batsy took too long');
    if (!String(e.message).startsWith('Not paired') && !e.status) setLink(false);  // Batsy answered: not offline
    throw e;
  } finally { clearTimeout(timer); }
}
function setLink(ok) {
  $('#link').className = 'link ' + (ok ? 'ok' : 'bad');
  $('#link em').textContent = ok ? store.get('via', '') : 'offline';
}
let toastTimer;
function toast(text) {
  if (!text) return;
  $('#toast').textContent = text; $('#toast').classList.add('on');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').classList.remove('on'), 3000);
}
// "Are you sure?" as frosted glass over just the widget you tapped (not the whole screen), easing in and out
function confirmBox(text, from, {danger = false, yes = 'Yes', alt = ''} = {}) {
  // on the iPhone dashboard: a floating card in the middle of the screen (widgets there can be long or scrolled)
  const host = (!document.body.classList.contains('dash') && from && from.closest('.w, .panel')) || document.body;
  host.querySelectorAll(':scope > .glass-ask').forEach(x => x.remove());
  const box = document.createElement('div');
  box.className = 'glass-ask' + (host === document.body ? ' floating' : '');
  box.innerHTML = `<div class="glass-card"><p></p><div class="glass-row"><button class="g-no">Cancel</button>
    ${alt ? '<button class="g-alt"></button>' : ''}<button class="g-yes${danger ? ' danger' : ''}"></button></div></div>`;
  box.querySelector('p').textContent = text;
  box.querySelector('.g-yes').textContent = yes;
  if (alt) box.querySelector('.g-alt').textContent = alt;
  host.appendChild(box);
  requestAnimationFrame(() => requestAnimationFrame(() => box.classList.add('on')));
  return new Promise(res => {
    const done = ok => {
      box.classList.remove('on');
      box.addEventListener('transitionend', () => box.remove(), {once: true});
      setTimeout(() => box.remove(), 600);
      res(ok);
    };
    box.querySelector('.g-yes').onclick = e => { e.stopPropagation(); done(true); };
    box.querySelector('.g-no').onclick = e => { e.stopPropagation(); done(false); };
    if (alt) box.querySelector('.g-alt').onclick = e => { e.stopPropagation(); done('alt'); };
    box.onclick = e => { e.stopPropagation(); if (e.target === box) done(false); };  // tap beside the card: no
  });
}
if (window.BatsyApp) { $('#gear').hidden = false; $('#gear').addEventListener('click', () => BatsyApp.settings()); }

// --- installed as an app (iPhone home screen / browser): service worker, pairing, notifications ----------------
const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
if ('serviceWorker' in navigator && location.protocol === 'https:' || location.hostname === '127.0.0.1') {
  navigator.serviceWorker && navigator.serviceWorker.register('/app/sw.js', {scope: '/app/'}).catch(() => {});
}
function needPairing(msg = '') {
  if (window.BatsyApp) return;  // the Android app pairs over USB itself
  $('#pairsheet').hidden = false;
  $('#pairmsg').textContent = msg;
  setTimeout(() => $('#paircode').focus(), 300);
}
if (!store.get('token', '')) needPairing();
$('#pairform').addEventListener('submit', async e => {
  e.preventDefault();
  const code = $('#paircode').value.trim();
  if (!/^\d{6}$/.test(code)) { $('#pairmsg').textContent = 'The code is 6 digits.'; return; }
  try {
    const r = await fetch('/api/pair', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({code})});
    if (!r.ok) { $('#pairmsg').textContent = 'That code didn’t work. Ask Batsy for a new one.'; return; }
    store.set('token', (await r.json()).token);
    store.set('via', 'Tailscale');
    $('#pairsheet').hidden = true;
    refresh(); loadLight(); loadFiles(); loadSlice(); showBell(); loadCloudWidget();
  } catch { $('#pairmsg').textContent = 'Can’t reach the laptop. Is Tailscale on?'; }
});
// notifications: iPhone only allows them in the installed app, after a tap
async function showBell() {
  if (!('PushManager' in window) || !('serviceWorker' in navigator) || !store.get('token', '')) return;
  if (/iPhone|iPad/.test(navigator.userAgent) && !standalone) return;  // iOS: only once it's on the home screen
  const reg = await navigator.serviceWorker.ready.catch(() => null);
  if (!reg) return;
  if (Notification.permission === 'granted') {
    // already allowed: keep the laptop's copy current, quietly (a new app version, a renewed push address, a
    // subscription the laptop dropped: no tap needed, ever, unless you delete the app or turn them off)
    try { await subscribePush(reg, true); $('#bell').hidden = true; return; } catch {}
  }
  $('#bell').hidden = false;
}
async function subscribePush(reg, quiet) {
  const {key} = await api('/api/push/key');
  const pad = '='.repeat((4 - key.length % 4) % 4);
  const raw = Uint8Array.from(atob((key + pad).replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
  let sub = await reg.pushManager.getSubscription();
  if (sub && sub.options && sub.options.applicationServerKey &&
      btoa(String.fromCharCode(...new Uint8Array(sub.options.applicationServerKey))) !== btoa(String.fromCharCode(...raw))) {
    await sub.unsubscribe(); sub = null;  // made with another key: start fresh
  }
  sub = sub || await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: raw});
  return api('/api/push/subscribe', {subscription: sub.toJSON(), quiet,
    device: navigator.userAgent.match(/iPhone|iPad|Android/)?.[0] || 'browser'});
}
$('#bell').addEventListener('click', async () => {
  try {
    if (await Notification.requestPermission() !== 'granted') { toast('Notifications are off in Settings › Batsy.'); return; }
    const r = await subscribePush(await navigator.serviceWorker.ready, false);
    toast(r.say); $('#bell').hidden = true;
  } catch (e) { toast('Couldn’t turn notifications on: ' + e.message); }
});
document.addEventListener('visibilitychange', () => { if (!document.hidden) showBell(); });  // each time it's opened
showBell();

// --- views (sideways) and stacks (up/down), each remembered ---------------------------------------------
const views = $('#views');
let view = 0;
views.addEventListener('scroll', () => {
  const i = Math.round(views.scrollLeft / views.clientWidth);
  if (i === view) return;
  view = i; store.set('view', i);
  $$('#dots i').forEach((d, j) => d.classList.toggle('on', j === i));
}, {passive: true});
for (const s of $$('.stack')) {
  s.addEventListener('scroll', () => store.set('stack-' + s.id, Math.round(s.scrollTop / s.clientHeight)), {passive: true});
}
function restore() {
  views.scrollLeft = store.get('view', 0) * views.clientWidth;
  for (const s of $$('.stack')) s.scrollTop = store.get('stack-' + s.id, 0) * (s.clientHeight + parseFloat(getComputedStyle(s).rowGap || 0));
}
requestAnimationFrame(restore);
addEventListener('resize', () => requestAnimationFrame(restore));  // turned the phone: keep each place

// --- helpers --------------------------------------------------------------------------------------------
const ago = t => {
  const s = Date.now() / 1000 - t;
  if (s < 60) return 'now';
  if (s < 3600) return Math.floor(s / 60) + 'm';
  if (s < 86400) return Math.floor(s / 3600) + 'h';
  if (s < 172800) return 'yesterday';
  return new Date(t * 1000).toLocaleDateString(undefined, {day: 'numeric', month: 'short'});
};
const dur = s => { s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
  return h ? `${h}h ${m}m` : `${m}m`; };
const clockOf = ms => { const s = Math.floor(ms / 1000); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
const level = (v, warn, bad) => v >= bad ? 'bad' : v >= warn ? 'warn' : '';

// --- clock + night tint ------------------------------------------------------------------------------------
function tickClock() {
  const d = new Date();
  const time = d.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'}).replace(/\s?[AP]M$/i, '');
  const date = d.toLocaleDateString([], {weekday: 'long', day: 'numeric', month: 'long'});
  $('#w-time').textContent = $('#big-time').textContent = time;
  $('#w-date').textContent = $('#big-date').textContent = date;
  const h = d.getHours();
  // StandBy dims at night (the Android phone on its stand); the iPhone has its own brightness
  document.body.classList.toggle('night', (h >= 23 || h < 6) && !document.body.classList.contains('dash'));
}
tickClock(); setInterval(tickClock, 5000);

// --- batsy + ask sheet ----------------------------------------------------------------------------------
const STATES = {idle: ['Idle', ''], listening: ['Listening', 'listening'], thinking: ['Thinking', 'thinking'],
  speaking: ['Speaking', 'speaking'], working: ['Working', 'thinking']};
function drawBatsy(b) {
  const [label, cls] = STATES[b.state] || [b.state || 'Idle', ''];
  $('#b-state').textContent = label;
  $('#orb').className = 'orb ' + cls;
  $('#b-said').textContent = b.status || b.said || (b.asks && b.asks[0]) || 'Say “Batsy”, or tap to type.';
  const r = b.next_reminder;
  $('#w-next').textContent = r ? `${r.what} · ${new Date(r.at * 1000).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'})}` : '';
  const today = (b.today || []).map(t => typeof t === 'string' ? {text: t} : t)  // the day's to-dos, under the clock
    .map(t => `<li class="${t.done ? 'done' : ''}">${esc(t.text)}</li>`).join('');
  for (const id of ['#w-today', '#big-today']) if ($(id)) $(id).innerHTML = today;
}
// The conversation comes from the laptop (Batsy's memory): everything you said, by voice at the laptop, from this
// phone, typed or on the website, and every answer. Kept here too, so it shows at once (and offline).
let chat = store.get('chatLog', []), chatLive = null, sending = [], chatTimer = null;
const VIA = {voice: '🎙', phone: '📱', typed: '⌨', website: '🌐', announced: '🦇', routine: '⏰'};
function drawChat() {
  const box = $('#chat');
  const atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  const rows = chat.map(m => `<div class="msg ${m.who}"><span class="via">${VIA[m.via] || ''}</span>${esc(m.text)}</div>`);
  if (chatLive && !sending.some(s => s.text === chatLive.you)) {  // being answered right now (by voice, say)
    rows.push(`<div class="msg you">${esc(chatLive.you)}</div>`,
      `<div class="msg bot wait">${esc(chatLive.said || chatLive.status || 'Thinking…')}</div>`);
  }
  for (const s of sending) rows.push(`<div class="msg you">${esc(s.text)}</div>`, `<div class="msg bot wait">${esc(s.reply || 'Thinking…')}</div>`);
  box.innerHTML = rows.join('') || '<div class="msg bot">Say “Batsy”, or type here.</div>';
  if (atEnd || box.dataset.opened !== '1') { box.scrollTop = 1e9; box.dataset.opened = '1'; }
}
async function loadChat() {
  try {
    const r = await api('/api/conversation?limit=120');
    chat = r.messages; chatLive = r.live;
    store.set('chatLog', chat.slice(-120));
  } catch {}
  drawChat();
}
function openSheet() {
  $('#sheet').hidden = false; $('#chat').dataset.opened = '0';
  drawChat(); loadChat(); $('#ask-text').focus();
  clearInterval(chatTimer);
  chatTimer = setInterval(() => { if ($('#sheet').hidden) clearInterval(chatTimer); else loadChat(); }, 3000);
}
$('#batsy-w').addEventListener('click', openSheet);
$('#sheet').addEventListener('click', e => { if (e.target.id === 'sheet') $('#sheet').hidden = true; });
$('#ask').addEventListener('submit', async e => {
  e.preventDefault();
  const text = $('#ask-text').value.trim();
  if (!text) return;
  $('#ask-text').value = '';
  const s = {text};
  sending.push(s); $('#chat').dataset.opened = '0'; drawChat();
  try { await api('/api/batsy', {action: 'ask', text}, 180000); }
  catch (err) {  // no answer: Batsy may be restarting (or updating). What it said is saved and shows up once it's back.
    s.reply = 'No answer yet. If Batsy is restarting, its reply will show up here when it’s back.';
    drawChat(); await new Promise(r => setTimeout(r, 4000));
  }
  sending = sending.filter(x => x !== s);
  await loadChat(); refresh();  // the answer, as Batsy saved it
});

// --- Android StandBy: something Batsy announces takes over the whole screen for a few seconds ---------------------
const TO_ICONS = {slice: '🧊', print: '🖨', reminder: '⏰', file: '📥', music: '🎵', warn: '⚠️', ask: '❓', batsy: '🦇', open: '🔗'};
const TO_COLORS = {slice: '124, 92, 255', print: '255, 194, 14', reminder: '255, 122, 122', file: '122, 167, 255',
  music: '60, 207, 110', warn: '255, 92, 77', ask: '255, 194, 14', batsy: '255, 194, 14'};
let toQueue = [], toBusy = false;
function takeEvents(events) {
  if (!events || document.body.classList.contains('dash')) return;  // the iPhone gets push notifications instead
  const seen = store.get('evSeen', null);
  const newest = events.length ? events[events.length - 1].id : 0;
  if (seen === null) { store.set('evSeen', newest); return; }  // first run: nothing old pops up
  const fresh = events.filter(e => e.id > seen);
  if (!fresh.length) return;
  store.set('evSeen', newest);
  fresh.filter(e => e.kind === 'warn').forEach(e => toast(e.text));  // heads-ups (battery, disk): a small note
  const hot = fresh.filter(e => e.kind === 'temp').pop();
  if (hot) focusTemp(hot);  // running hot: the screen goes to that temperature and lights it up
  const big = fresh.filter(e => e.kind !== 'warn' && e.kind !== 'temp');
  if (!big.length) return;
  toQueue.push(...big.slice(-3));
  if (!toBusy) nextTakeover();
}
async function nextTakeover() {
  const e = toQueue.shift();
  if (!e) { toBusy = false; return; }
  toBusy = true;
  const to = $('#takeover');
  $('#to-icon').textContent = TO_ICONS[e.kind] || '🦇';
  $('#to-title').textContent = e.kind === 'slice' ? 'Slice done' : e.kind === 'print' && /\bdone\b/i.test(e.text) ? 'Print done' : e.title;
  to.ev = e;
  $('#to-text').textContent = e.text.replace(new RegExp('^' + e.title + ':\\s*', 'i'), '');  // not "Reminder: Reminder: …"
  $('#to-hint').textContent = e.kind === 'ask' ? 'Answer out loud, or hold the mic' : e.url ? 'Tap to open' : '';
  to.onclick = e.url ? () => { window.open(e.url, '_blank'); closeTakeover(); } : null;  // an app Batsy opened for you
  to.style.setProperty('--to', TO_COLORS[e.kind] || TO_COLORS.batsy);
  const img = $('#to-img');
  img.hidden = true;
  if (e.kind === 'slice') {  // the slice's own picture
    await loadSlice().catch(() => {});
    loadPlate();  // and "how should it sit?" on the card, if Batsy is asking
    if ($('#sw-img').src) { img.src = $('#sw-img').src; img.hidden = false; }
  }
  to.classList.toggle('long', e.text.length > 110);
  const ms = e.url ? 20000 : Math.min(14000, 5000 + e.text.length * 45);  // a link: time to tap it
  const bar = $('#to-bar');
  bar.style.transition = 'none'; bar.style.transform = 'scaleX(1)';
  to.hidden = false;
  requestAnimationFrame(() => requestAnimationFrame(() => {
    to.classList.add('on');
    bar.style.transition = `transform ${ms}ms linear`; bar.style.transform = 'scaleX(0)';
  }));
  clearTimeout(to.timer);
  to.timer = setTimeout(closeTakeover, ms);
}
function closeTakeover() {
  const to = $('#takeover');
  clearTimeout(to.timer);
  to.classList.remove('on');
  setTimeout(() => {
    to.hidden = true;
    const go = to.ev && TO_WIDGETS[to.ev.kind];
    to.ev = null;
    if (go && !toQueue.length) goToWidget($(go));  // then the widget it's about: the slice's picture, the printer
    nextTakeover();
  }, 450);
}
const TO_WIDGETS = {slice: '#sw', print: '#pw'};
// swipe to the widgets view and scroll that widget's stack to it; false when it's hidden or on the iPhone dashboard
function goToWidget(el) {
  // Exactly to that card: the page it's on, then its place in that stack. (scrollIntoView moved StandBy's whole row
  // of pages and left you on the big clock instead.)
  if (!el || !el.offsetParent || document.body.classList.contains('dash')) return false;
  const views = $('#views'), page = el.closest('.view'), stack = el.closest('.stack');
  if (views && page) views.scrollTo({left: page.offsetLeft, behavior: 'smooth'});
  if (stack) {
    const top = el.getBoundingClientRect().top - stack.getBoundingClientRect().top + stack.scrollTop;
    stack.scrollTo({top, behavior: 'smooth'});
  }
  return true;
}
let hotMark = null;  // {machine, until}: drawMachines keeps that temperature lit across refreshes
function focusTemp(e) {
  hotMark = {machine: e.machine, until: Date.now() + 20000};
  if (!goToWidget($('#machines-w'))) { hotMark = null; return toast(e.text); }  // (hidden from StandBy: just a note)
  $$('#machines-w .gauge.hot').forEach(g => g.classList.remove('hot'));
  const g = $(`#machines-w .gauge[data-temp="${e.machine}"]`);
  if (g) { g.classList.add('hot'); g.querySelector('b').textContent = e.temp + '°'; }
}
$('#takeover').addEventListener('click', closeTakeover);

// --- things to print: the cards from the last model search ("find me a phone stand to print") ------------------------
let mdItems = [];
function takeModels(m) {
  if (!m || !m.t || m.t <= store.get('mdSeen', 0)) return;
  store.set('mdSeen', m.t);
  mdItems = m.items;
  $('#md-title').textContent = `“${m.q}” · ${m.items.length} models`;
  $('#md-grid').innerHTML = m.items.map((it, i) => `<button class="ct md" data-i="${i}">
    <div class="ct-pic">${it.image ? `<img loading="lazy" alt="" src="${esc(it.image)}" data-full="${esc(it.image_full || '')}" onerror="if (this.dataset.full && this.src !== this.dataset.full) this.src = this.dataset.full">` : ''}<span class="md-n">${i + 1}</span>
      <span class="md-site ${esc(it.site)}">${esc(it.site)}</span></div>
    <b>${esc(it.name)}</b><small>${_k(it.downloads)} downloads · ${_k(it.likes)} likes${it.can_download ? '' : ' · opens the page'}</small></button>`).join('');
  const p = $('#models'); p.hidden = false; requestAnimationFrame(() => p.classList.add('on'));
}
const _k = n => n >= 1000 ? (n / 1000).toFixed(1).replace('.0', '') + 'k' : String(n || 0);
function closeModels() { const p = $('#models'); p.classList.remove('on'); setTimeout(() => { p.hidden = true; }, 280); }
$('#md-close').addEventListener('click', closeModels);
$('#md-grid').addEventListener('click', async e => {
  const t = e.target.closest('.md'); if (!t) return;
  const it = mdItems[+t.dataset.i];
  if (!await confirmBox(it.can_download ? `Get “${it.name}” from ${it.site}? You’ll see its size and shape first.` : `“${it.name}” is on MakerWorld (needs your Bambu login). Open its page on the laptop?`, t, {yes: it.can_download ? 'Get it' : 'Open'})) return;
  if (it.can_download) cardLoading(t);  // the bat fills on the card while Batsy fetches and measures it
  try {
    const r = await api('/api/models', {i: +t.dataset.i}, 20000);
    if (!it.can_download) { toast(r.say); closeModels(); }
  } catch (err) { toast(err.message); cardDone(false); }
});

// the filling bat, on the tapped card: rises while the model is fetched (no exact progress: fast, then slower)
let loadingCard = null;
function cardLoading(card) {
  cardDone(false);
  const bat = $('#viewer-load .fillbat').cloneNode(true);
  bat.querySelector('clipPath').id = 'cardclip';
  bat.querySelector('[clip-path]').setAttribute('clip-path', 'url(#cardclip)');
  const lvl = bat.querySelector('#vlevel'); lvl.removeAttribute('id');
  const box = document.createElement('div');
  box.className = 'card-load';
  box.append(bat);
  box.insertAdjacentHTML('beforeend', '<small>Getting the model…</small>');
  card.querySelector('.ct-pic').append(box);
  card.classList.add('loading');
  const fill = f => { lvl.style.transform = `translateY(${(1 - Math.min(1, f)) * 12 - 3}px)`; };
  const t0 = Date.now();
  fill(0.05);
  loadingCard = {card, box, fill, tick: setInterval(() => fill(0.9 * (1 - Math.exp(-(Date.now() - t0) / 3000))), 200)};
}
function cardDone(ok) {
  const l = loadingCard; if (!l) return;
  loadingCard = null; clearInterval(l.tick);
  if (ok) { l.fill(1); l.box.querySelector('small').textContent = 'Ready'; }
  setTimeout(() => { l.box.remove(); l.card.classList.remove('loading'); }, ok ? 500 : 0);
}

// a picked model, before slicing: its size, whether it fits the bed, and its outline
function takePreview(p) {
  if (!p || !p.t) return;
  const box = $('#mpv');
  if (p.state !== 'asking') { if (!box.hidden && box.dataset.t == p.t) closePreview(); return; }
  if (box.dataset.t == p.t) return;
  box.dataset.t = p.t;
  const i = p.info || {};
  if (loadingCard) { cardDone(true); setTimeout(() => takePreviewNow(p, i), 550); } else takePreviewNow(p, i);
}
function takePreviewNow(p, i) {
  closeModels();
  const box = $('#mpv');
  // the model itself, to turn around (with its own filling bat while it downloads)
  $('#mpv-3d').src = `/app/model.html?src=${encodeURIComponent(p.mesh || '')}&x=${i.x}&y=${i.y}&z=${i.z}`;
  $('#mpv-site').textContent = p.site;
  $('#mpv-name').textContent = p.name;
  $('#mpv-rows').innerHTML = [
    ['Size', `${i.x} × ${i.y} × ${i.z} mm`], ['Bed', i.fits ? 'fits ✓' : 'too big ✗'],
    ['Weight', i.grams ? `≈ ${Math.round(i.grams)} g` + (i.grams_how === 'approx' ? ' (rough)' : '') : '–'], ['Parts', i.parts || 1]]
    .map(([k, v]) => `<div><small>${k}</small><b class="${k === 'Bed' ? (i.fits ? 'ok' : 'bad') : ''}">${esc(String(v))}</b></div>`).join('');
  box.hidden = false; requestAnimationFrame(() => box.classList.add('on'));
}
function closePreview() {
  const b = $('#mpv'); b.classList.remove('on');
  setTimeout(() => { b.hidden = true; $('#mpv-3d').src = 'about:blank'; }, 300);  // (stops the 3D view)
}
['slice', 'skip'].forEach(act => $('#mpv-' + act).addEventListener('click', async () => {
  closePreview();
  try { toast((await api('/api/models', {action: act}, 20000)).say); } catch (err) { toast(err.message); }
}));

// --- the phone's microphone: hold to talk, or let the phone be Batsy's ears for a while ------------------------------
// Audio goes to the laptop (Whisper there turns it into words; Batsy handles it like anything you'd say); Batsy
// answers out loud from the laptop, and as text here. The phone is only the microphone.
const mic = {stream: null, rec: null, chunks: [], ears: false, ctx: null, an: null, busy: false, mute: false, timer: null};
// iOS takes the mic away when the app goes to the background or the screen locks, without saying so: a stream that
// isn't live any more is dropped and asked for again (reusing a dead one froze the button until the app was reopened)
const micLive = () => mic.stream && mic.stream.getAudioTracks().some(t => t.readyState === 'live' && t.enabled && !t.muted);
function micRelease() {
  try { if (mic.rec && mic.rec.state !== 'inactive') mic.rec.stop(); } catch {}
  mic.rec = null;
  if (mic.stream) mic.stream.getTracks().forEach(t => { try { t.stop(); } catch {} });
  mic.stream = null;
}
async function micStream() {
  if (mic.stream && !micLive()) micRelease();
  if (!mic.stream) {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) throw new Error('This phone can’t use its microphone here (it needs the secure Batsy address).');
    mic.stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: true}});
  }
  return mic.stream;
}
const recType = () => ['audio/webm;codecs=opus', 'audio/mp4', 'audio/webm', 'audio/ogg'].find(t => window.MediaRecorder && MediaRecorder.isTypeSupported(t)) || '';
function startRec() {
  const t = recType(), r = new MediaRecorder(mic.stream, t ? {mimeType: t} : {});
  mic.chunks = []; r.ondataavailable = e => { if (e.data.size) mic.chunks.push(e.data); };
  r.start(250); r.t0 = Date.now(); mic.rec = r;  // (chunks as it goes: something to send even if stop never reports)
}
function stopRec() {
  return new Promise(res => {
    const r = mic.rec; mic.rec = null;
    if (!r || r.state === 'inactive') return res(null);
    let done = false;
    const finish = () => { if (done) return; done = true;
      res(mic.chunks.length ? {blob: new Blob(mic.chunks, {type: r.mimeType || recType()}), ms: Date.now() - r.t0} : null); };
    r.onstop = finish;
    setTimeout(finish, 2500);  // iOS sometimes never fires "stop": don't wait for it forever
    try { r.stop(); } catch { finish(); }
  });
}
let unlocked = false, voicePlayer = null;
function unlockAudio() {  // iOS plays sound only after a tap: play a silent clip on the first one
  if (unlocked) return;
  unlocked = true;
  voicePlayer = new Audio(URL.createObjectURL(silentWav()));
  voicePlayer.play().catch(() => {});
}
function silentWav(ms = 60, rate = 8000) {  // a few silent samples, as a real WAV file
  const n = Math.round(rate * ms / 1000), b = new DataView(new ArrayBuffer(44 + n));
  const w = (o, t) => [...t].forEach((c, i) => b.setUint8(o + i, c.charCodeAt(0)));
  w(0, 'RIFF'); b.setUint32(4, 36 + n, true); w(8, 'WAVEfmt '); b.setUint32(16, 16, true); b.setUint16(20, 1, true);
  b.setUint16(22, 1, true); b.setUint32(24, rate, true); b.setUint32(28, rate, true); b.setUint16(32, 1, true);
  b.setUint16(34, 8, true); w(36, 'data'); b.setUint32(40, n, true);
  for (let i = 0; i < n; i++) b.setUint8(44 + i, 128);
  return new Blob([b], {type: 'audio/wav'});
}
function chime(up = true) {
  try {
    const c = mic.ctx || new (window.AudioContext || window.webkitAudioContext)();
    mic.ctx = c;
    const o = c.createOscillator(), g = c.createGain();
    o.frequency.setValueAtTime(up ? 660 : 520, c.currentTime); o.frequency.linearRampToValueAtTime(up ? 990 : 390, c.currentTime + .12);
    g.gain.setValueAtTime(.0001, c.currentTime); g.gain.exponentialRampToValueAtTime(.25, c.currentTime + .02);
    g.gain.exponentialRampToValueAtTime(.0001, c.currentTime + .22);
    o.connect(g).connect(c.destination); o.start(); o.stop(c.currentTime + .25);
  } catch {}
}
async function sendVoice(blob, mode) {
  const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), 90000);  // Batsy restarting: don't hang
  let r;
  try {
    r = await fetch('/api/voice?mode=' + mode, {method: 'POST', body: blob, signal: ctl.signal,
      headers: {Authorization: 'Bearer ' + store.get('token', ''), 'Content-Type': blob.type || 'application/octet-stream'}});
  } catch (e) {
    throw new Error(e.name === 'AbortError' ? 'Batsy took too long. Try again.' : 'Batsy isn’t reachable right now.');
  } finally { clearTimeout(timer); }
  if (!r.ok) throw new Error('Batsy didn’t get that (' + r.status + ')');
  return r.json();
}
function answered(d) {
  if (d.armed) { chime(true); $('#b-said').textContent = 'Listening…'; return; }
  if (d.ignored || !d.heard) return;
  chat.push({who: 'you', text: '🎙 ' + d.heard}, {who: 'bot', text: d.say || 'Done.'}); store.set('chat', chat.slice(-30));
  $('#b-said').textContent = d.say || d.heard;
  // Batsy answers from the laptop's speakers: don't take that (if the phone hears it) as a new request
  mic.mute = true;
  setTimeout(() => { mic.mute = false; }, Math.min(15000, 1500 + 70 * (d.say || '').length));
}
// hold to talk: the button in the Batsy widget, and the big round one that floats over every screen
function holdToTalk(btn, movable = false) {
  const status = t => { $('#b-said').textContent = t; $('#fab-said').textContent = t; };
  btn.addEventListener('click', e => e.stopPropagation());
  btn.addEventListener('contextmenu', e => e.preventDefault());
  // movable (the floating mic, like AssistiveTouch): moving your finger right away drags it; holding still talks
  let wait = null, drag = null, start = null;
  async function begin() {
    wait = null;
    if (!btn.dataset.down) return;
    try { await micStream(); } catch (err) { btn.dataset.down = ''; return toast(err.message); }
    if (!btn.dataset.down) return;  // let go while the permission prompt was up
    if (mic.ears) mic.mute = true;  // ears mode: this recording, not that one
    try { startRec(); } catch { micRelease(); try { await micStream(); startRec(); } catch (err) { btn.dataset.down = ''; return toast('The mic didn’t start. Try again.'); } }
    btn.classList.add('rec'); document.body.classList.add('ptt'); chime(true);
    status('Listening… let go when you’re done');
  }
  btn.addEventListener('pointerdown', e => {
    e.stopPropagation(); e.preventDefault();
    unlockAudio();
    btn.setPointerCapture && btn.setPointerCapture(e.pointerId);
    btn.dataset.down = '1';
    if (!movable) return begin();
    const r = btn.getBoundingClientRect();
    start = {x: e.clientX, y: e.clientY, dx: e.clientX - r.left, dy: e.clientY - r.top};
    drag = null;
    btn.classList.remove('rest');
    wait = setTimeout(begin, 220);  // still after 0.2 s: you're talking, not moving it
  });
  if (movable) btn.addEventListener('pointermove', e => {
    if (!start || btn.classList.contains('rec')) return;
    if (!drag && Math.hypot(e.clientX - start.x, e.clientY - start.y) < 10) return;
    if (wait) { clearTimeout(wait); wait = null; }
    drag = true;
    btn.classList.add('dragging');
    placeFab(e.clientX - start.dx, e.clientY - start.dy);
  });
  ['pointerup', 'pointercancel'].forEach(ev => btn.addEventListener(ev, async e => {
    e.stopPropagation();
    btn.dataset.down = '';
    start = null;
    if (drag) { drag = null; btn.classList.remove('dragging'); snapFab(); return; }  // moved it: snap to a side
    if (wait) { clearTimeout(wait); wait = null; }
    if (!mic.rec || !btn.classList.contains('rec')) { if (movable) status('Hold the mic while you talk.'); restFab(); return; }
    btn.classList.remove('rec');
    const got = await stopRec();
    if (!got || got.ms < 400) { status('Hold the mic while you talk.'); setTimeout(() => document.body.classList.remove('ptt'), 1200); restFab(); return; }
    status('Thinking…'); btn.classList.add('wait');
    try { answered(await sendVoice(got.blob, 'ptt')); } catch (err) { toast(err.message); status(err.message); }
    finally { btn.classList.remove('wait', 'rec'); }
    $('#fab-said').textContent = $('#b-said').textContent;  // the answer, under the big mic for a moment
    setTimeout(() => document.body.classList.remove('ptt'), 4000);
    restFab();
  }));
}
// Leaving the app (or locking the phone) ends any recording and gives the mic back; coming back starts clean, so the
// mic always answers the next hold without reopening the app
let earsWere = false;
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    earsWere = mic.ears;
    if (mic.ears) earsOff(true);  // (the laptop's mic takes over while the app is away)
    micRelease();
    mic.busy = false;
  } else if (earsWere) { earsWere = false; setTimeout(() => earsOn(), 400); }  // back: listening again, fresh mic
  for (const b of document.querySelectorAll('.rec, .wait')) b.classList.remove('rec', 'wait');
  document.body.classList.remove('ptt');
  for (const b of document.querySelectorAll('[data-down]')) b.dataset.down = '';
});
addEventListener('pageshow', () => { if (mic.stream && !micLive()) micRelease(); });
// --- the floating mic's place: dragged anywhere, snapped to the nearest side, remembered (like AssistiveTouch) ----
const FAB = () => $('#fab-mic');
function placeFab(x, y) {
  const b = FAB(), w = b.offsetWidth, h = b.offsetHeight;
  b.style.left = Math.max(6, Math.min(innerWidth - w - 6, x)) + 'px';
  b.style.top = Math.max(6, Math.min(innerHeight - h - 6, y)) + 'px';
  b.style.right = b.style.bottom = 'auto';
}
function snapFab() {
  const b = FAB(), r = b.getBoundingClientRect();
  const side = r.left + r.width / 2 < innerWidth / 2 ? 'left' : 'right';
  const y = Math.max(0, Math.min(1, r.top / Math.max(1, innerHeight - r.height)));
  store.set('fabPos', {side, y});
  b.classList.add('snapping');
  applyFab();
  setTimeout(() => b.classList.remove('snapping'), 260);
  restFab();
}
function applyFab() {
  const p = store.get('fabPos', null), b = FAB();
  const w = b.offsetWidth, h = b.offsetHeight;
  if (p) placeFab(p.side === 'left' ? 12 : innerWidth - w - 12, 6 + p.y * Math.max(0, innerHeight - h - 12));
  requestAnimationFrame(avoidButtons);
}
// The mic floats over everything: never let it sit on a button. If it does (landscape puts the widgets where its
// corner is), it moves to the other side, low down, where the clock leaves room; your own placement is kept otherwise.
function avoidButtons() {
  const b = FAB(), r = b.getBoundingClientRect();
  if (!r.width) return;
  const hits = el => { const q = el.getBoundingClientRect();
    return q.width && !(q.right < r.left || q.left > r.right || q.bottom < r.top || q.top > r.bottom); };
  const under = [...document.querySelectorAll('.view button, .view a, .view input')].filter(el => el !== b && el.offsetParent && hits(el));
  if (!under.length) return;
  const other = r.left + r.width / 2 < innerWidth / 2 ? innerWidth - r.width - 12 : 12;
  placeFab(other, innerHeight - r.height - 18);
}
addEventListener('resize', () => requestAnimationFrame(applyFab));
$('#views')?.addEventListener('scroll', () => { clearTimeout(avoidButtons.t); avoidButtons.t = setTimeout(avoidButtons, 250); }, {passive: true});
let fabRest = null;
function restFab() {  // after a few quiet seconds it fades back a little, out of the way
  const b = FAB();
  b.classList.remove('rest');
  clearTimeout(fabRest);
  fabRest = setTimeout(() => { if (!b.classList.contains('rec') && !b.classList.contains('wait')) b.classList.add('rest'); }, 3500);
}
addEventListener('resize', applyFab);
addEventListener('orientationchange', () => setTimeout(applyFab, 300));
holdToTalk($('#b-mic'));
holdToTalk($('#fab-mic'), true);
applyFab(); restFab();

// the phone as Batsy's ears: it listens for "Batsy" (the laptop's mic stands down meanwhile)
async function earsOn() {
  unlockAudio();
  try { await micStream(); } catch (err) { return toast(err.message); }
  const c = mic.ctx || new (window.AudioContext || window.webkitAudioContext)();
  mic.ctx = c; c.resume && c.resume();
  mic.an = c.createAnalyser(); mic.an.fftSize = 1024;
  c.createMediaStreamSource(mic.stream).connect(mic.an);
  mic.ears = true; $('#b-ears').classList.add('on'); document.body.classList.add('phone-ears');
  toast('The phone is listening: say “Batsy, …”');
  const buf = new Float32Array(mic.an.fftSize);
  let floor = 0.01, loudAt = 0, recAt = 0;
  sendVoice(new Blob([]), 'ears').catch(() => {});  // tells the laptop its mic can rest
  mic.beat = setInterval(() => { if (mic.ears) sendVoice(new Blob([]), 'ears').catch(() => {}); }, 30000);
  mic.timer = setInterval(async () => {
    if (!mic.ears) return;
    mic.an.getFloatTimeDomainData(buf);
    let sum = 0; for (const v of buf) sum += v * v;
    const level = Math.sqrt(sum / buf.length), now = Date.now();
    if (mic.mute || mic.busy) return;
    if (!mic.rec) floor = floor * 0.98 + level * 0.02;
    const loud = level > Math.max(0.012, floor * 3);
    if (loud) loudAt = now;
    if (loud && !mic.rec) { startRec(); recAt = now; $('#b-ears').classList.add('hear'); }
    if (mic.rec && (now - loudAt > 900 || now - recAt > 12000)) {  // a pause, or 12 s: that's one thing said
      $('#b-ears').classList.remove('hear');
      const got = await stopRec();
      if (!got || got.ms < 700) return;
      mic.busy = true;
      try { answered(await sendVoice(got.blob, 'ears')); } catch {}
      mic.busy = false;
    }
  }, 60);
}
function earsOff(quiet = false) {
  if (!mic.ears) return;
  mic.ears = false; clearInterval(mic.timer); clearInterval(mic.beat);
  if (mic.rec) stopRec();
  $('#b-ears').classList.remove('on', 'hear'); document.body.classList.remove('phone-ears');
  fetch('/api/voice/off', {method: 'POST', headers: {Authorization: 'Bearer ' + store.get('token', '')}}).catch(() => {});
  if (mic.stream) { mic.stream.getTracks().forEach(t => t.stop()); mic.stream = null; }
  if (!quiet) toast('The laptop’s mic is listening again.');
}
$('#b-ears').addEventListener('click', e => { e.stopPropagation(); mic.ears ? earsOff() : earsOn(); });
document.addEventListener('visibilitychange', () => { if (document.hidden) earsOff(true); });  // the phone can't listen in the background

// --- home tiles ---------------------------------------------------------------------------------------------
let lightOn = null, pc = null;
async function loadLight() {
  try { lightOn = (await api('/api/home')).on; } catch { lightOn = null; }
  $('#t-light-s').textContent = lightOn === null ? 'not answering' : lightOn ? 'on' : 'off';
  $('#t-light').classList.toggle('on', !!lightOn);
}
$$('[data-act]').forEach(b => b.addEventListener('click', async () => {
  const act = b.dataset.act;
  try {
    let r;
    if (act === 'stop') r = await api('/api/batsy', {action: 'stop'});
    else if (act === 'light') r = await api('/api/home', {action: lightOn ? 'off' : 'on'});
    else if (act === 'night') r = await api('/api/home', {action: 'night'});
    else if (act === 'pc') return openPc();
    toast(r.say);
    if (act === 'light' || act === 'night') { lightOn = r.on ?? lightOn; loadLight(); }
  } catch (e) { toast(e.message); }
}));

// --- computers ----------------------------------------------------------------------------------------------
function gauge(label, value, pct, cls, temp = '', hot = false) {  // temp: whose temperature, for the running-hot jump
  return `<div class="gauge${hot ? ' hot' : ''}"${temp ? ` data-temp="${esc(temp)}"` : ''}><span>${label}<b>${value}</b></span><div class="bar"><i class="${cls}" style="width:${Math.min(100, pct || 0)}%"></i></div></div>`;
}
function drawMachines(ms) {
  pc = ms.find(m => m.kind === 'pc') || null;
  $('#t-pc-s').textContent = pc ? (pc.online ? 'on' : pc.online === false ? 'off' : '…') : 'not set up';
  $('#t-pc').classList.toggle('on', !!(pc && pc.online));
  const box = ms.find(m => m.kind === 'ai');
  const aiHere = box && !(box.online && box.ai && box.ai.serving);  // the HP is off: the laptop runs the AI
  $('#machines-w').dataset.n = ms.length;  // three machines: side by side in landscape
  $('#machines-w').innerHTML = ms.map(m => {
    const dot = m.online === true ? 'on' : m.online === false ? 'off' : '';
    if (!m.online) return `<div class="m"><h4><i class="${dot}"></i>${esc(m.name)}</h4>
      <p class="off-note">${m.online === false ? (m.kind === 'ai' ? 'Off · the laptop runs the AI' : 'Off or asleep') : 'Checking…'}</p>${m.kind === 'pc' ? '<button class="act" data-pc="wake">Wake up</button>' : ''}</div>`;
    return `<div class="m"><h4><i class="on"></i>${esc(m.name)}</h4>${m.kind === 'ai' ? aiLine(m.ai || {}) : ''}
      ${gauge('CPU', Math.round(m.cpu || 0) + '%', m.cpu, level(m.cpu, 70, 90))}
      ${gauge('Temp', (m.temp ?? '–') + '°', m.temp, level(m.temp || 0, 80, 92),
        m.kind, hotMark && hotMark.machine === m.kind && Date.now() < hotMark.until)}
      ${gauge('Memory', m.ram_used_gb != null ? `${m.ram_used_gb}/${m.ram_total_gb} GB` : Math.round(m.ram) + '%', m.ram, level(m.ram, 75, 90))}
      ${gauge('Disk', m.disk_free_gb + ' GB free', m.disk, level(m.disk, 80, 92))}
      ${m.kind === 'ai' ? '' : m.kind === 'pc' ? '<button class="act" data-pc="menu">Controls</button>'
        : `${m.battery != null ? `<small>Battery ${m.battery}%${m.charging ? ', charging' : ''}</small>` : ''}${aiHere ? '<span class="ai-badge here">🧠 Running the AI</span>' : ''}`}</div>`;
  }).join('');
}
function aiLine(a) {  // the HP's own job: Batsy's local AI (under its name, clear of the mic button)
  if (!a.serving) return `<span class="ai-badge off">🧠 AI on standby</span>`;
  const speed = a.first_s != null ? ` · speaks in ${a.first_s}s` : '';
  return `<span class="ai-badge">🧠 ${esc((a.model || 'AI').split(':')[0])}${a.loaded ? '' : ' (loading)'}${speed}</span>`;
}
function openPc() {
  const on = pc && pc.online;
  $('#pcpanel').innerHTML = `<h3>PC · ${on ? 'on' : 'off'}</h3><div class="pcbtns">${(on
    ? [['lock', 'Lock'], ['sleep', 'Sleep'], ['restart', 'Restart'], ['shutdown', 'Shut down']] : [['wake', 'Wake up']])
    .map(([a, l]) => `<button data-pc="${a}" class="${a === 'restart' || a === 'shutdown' ? 'danger' : ''}">${l}</button>`).join('')}</div>`;
  $('#pcsheet').hidden = false;
}
$('#pcsheet').addEventListener('click', e => { if (e.target.id === 'pcsheet') $('#pcsheet').hidden = true; });
document.addEventListener('click', async e => {
  const b = e.target.closest('[data-pc]');
  if (!b) return;
  const a = b.dataset.pc;
  if (a === 'menu') return openPc();
  if ((a === 'restart' || a === 'shutdown') && !await confirmBox(a === 'restart' ? 'Restart the PC now?' : 'Shut down the PC now?',
      b, {danger: true, yes: a === 'restart' ? 'Restart' : 'Shut down'})) return;
  $('#pcsheet').hidden = true;
  try { toast((await api('/api/pc', {action: a}, 15000)).say); setTimeout(refresh, 2000); } catch (err) { toast(err.message); }
});

// --- claude ----------------------------------------------------------------------------------------------
function ring(id, pct) {
  const c = $(id);
  c.style.strokeDashoffset = 264 * (1 - Math.min(100, pct || 0) / 100);
  c.setAttribute('class', 'fill ' + level(pct, 70, 90));
}
function drawClaude(c) {
  if (!c) return;
  for (const k of ['session', 'week']) { ring('#r-' + k, (c[k] || {}).pct); $('#c-' + k).textContent = Math.round((c[k] || {}).pct || 0) + '%'; }
  const s = c.session || {};
  $('#c-reset').textContent = s.resets_at ? `Session resets in ${dur(s.resets_at - Date.now() / 1000)} · ${c.requests_week ?? 0} requests this week` : '';
  const ss = c.sessions || [], run = c.running || [];
  $('#c-sessions').innerHTML = ss.map(x => `<li><span class="live"></span><b>${esc(x.project)}</b><small>${dur(Date.now() / 1000 - x.since)}</small></li>`).join('')
    + run.map(a => `<li><span class="live"></span><b>${esc(a)}</b><small>Batsy</small></li>`).join('')
    || '<li class="empty">No sessions running</li>';
}

// --- music --------------------------------------------------------------------------------------------------
// The widget only changes what changed: a new song cross-fades its art and tint, the bar glides between updates,
// and a tap on play/pause holds that state until Spotify has caught up (no flicker back).
let song = null, songAt = 0, shownId = null, holdUntil = 0, artSlot = 0;
const mw = $('#mw'), mwImgs = $$('.mw-art img'), mwBar = $('#mw-bar'), mwPP = $('.mw-pp');

function theme(img) {
  // the album's colours: its average (darkened) tints the music card; its most vivid colour becomes the app's
  // accent, and a dark version of it the widgets' background, so the whole app wears what's playing
  try {
    const c = document.createElement('canvas'); c.width = c.height = 16;
    const g = c.getContext('2d'); g.drawImage(img, 0, 0, 16, 16);
    const d = g.getImageData(0, 0, 16, 16).data;
    let r = 0, gg = 0, b = 0, best = null, bestScore = -1;
    for (let i = 0; i < d.length; i += 4) {
      const [R, G, B] = [d[i], d[i + 1], d[i + 2]];
      r += R; gg += G; b += B;
      const mx = Math.max(R, G, B), mn = Math.min(R, G, B), sat = mx ? (mx - mn) / mx : 0, val = mx / 255;
      const score = sat * (1 - Math.abs(val - 0.75));  // vivid, and neither dark nor washed out
      if (score > bestScore) { bestScore = score; best = [R, G, B]; }
    }
    const n = d.length / 4, avg = [r / n, gg / n, b / n];
    mw.style.setProperty('--tint', avg.map(v => Math.round(v * 0.55)).join(', '));
    let acc = bestScore > 0.18 ? best : [255, 194, 14];  // a grey cover: keep Batsy's yellow
    const mx = Math.max(...acc);
    if (mx < 200) acc = acc.map(v => Math.round(v * 200 / mx));  // bright enough to read on black
    const root = document.documentElement.style;
    root.setProperty('--signal', `rgb(${acc.join(',')})`);
    const lum = (0.2126 * acc[0] + 0.7152 * acc[1] + 0.0722 * acc[2]) / 255;
    root.setProperty('--ink', lum > 0.55 ? '#14110a' : '#ffffff');
    const base = acc.map((v, i) => v * 0.6 + avg[i] * 0.4);
    root.setProperty('--w', `rgb(${base.map(v => Math.round(12 + v * 0.07)).join(',')})`);
    root.setProperty('--w2', `rgb(${base.map(v => Math.round(20 + v * 0.11)).join(',')})`);
  } catch { /* can't read the art: keep the colours we have */ }
}
function showArt(url) {
  const next = mwImgs[1 - artSlot];
  const img = new Image();
  img.crossOrigin = 'anonymous';
  img.onload = () => {
    next.src = url;
    next.classList.add('on'); mwImgs[artSlot].classList.remove('on');
    artSlot = 1 - artSlot;
    theme(img);
  };
  img.src = url;
}
function drawWidget() {
  const id = song ? (song.id || song.title) : null;
  if (id !== shownId) {  // a different song (or none): text, art, and the bar jumps without gliding
    shownId = id;
    $('#mw-title').textContent = song ? song.title : 'Nothing playing';
    $('#mw-artist').textContent = song ? song.artist : 'Spotify';
    mw.classList.toggle('idle', !song);
    if (song && song.art) showArt(song.art);
    else { mwImgs.forEach(i => i.classList.remove('on')); mw.style.setProperty('--tint', '40, 42, 50'); }
    mwBar.style.transition = 'none'; tickSong(); void mwBar.offsetWidth; mwBar.style.transition = '';
  }
  $('#mw-dev').textContent = song ? (song.held ? 'Last played' : song.device || '') : '';
  if (Date.now() > holdUntil) mwPP.classList.toggle('playing', !!(song && song.playing));
}

function drawMusic(s) {
  let fresh = s && !s.error ? s : null;
  if (fresh) store.set('lastSong', {...fresh, playing: false, savedAt: Date.now()});  // kept for when it stops
  else {  // stopped / Spotify idle: the last song stays, paused where it was
    const last = store.get('lastSong', null);
    if (last) fresh = {...last, playing: false, held: true};
  }
  if (Date.now() < holdUntil && song && fresh && (fresh.id || fresh.title) === (song.id || song.title)) {
    fresh.playing = song.playing;  // you just tapped play/pause: trust that over a slightly stale answer
  }
  song = fresh; songAt = Date.now();
  drawWidget();
  // full-screen view
  const art = song && song.art;
  const el = $('#mf-art'); if (art) { if (el.src !== art) el.src = art; el.style.visibility = ''; } else el.style.visibility = 'hidden';
  const bg = art ? `url("${art}")` : 'none'; if ($('#mf-bg').style.backgroundImage !== bg) $('#mf-bg').style.backgroundImage = bg;
  $('#mf-title').textContent = song ? song.title : (s && s.error ? 'Spotify error' : 'Nothing playing');
  $('#mf-artist').textContent = song ? song.artist : (s && s.error) || '';
  $('#mf-dev').textContent = song && song.device ? song.device : '';
  $$('.music-full [data-sp="toggle"]').forEach(b => b.classList.toggle('playing', !!(song && song.playing)));
  tickSong();
}
function tickSong() {
  const dur = song ? song.duration_ms || 1 : 1;
  const pos = song ? Math.min(dur, song.progress_ms + (song.playing ? Date.now() - songAt : 0)) : 0;
  // the widget's bar is set one second ahead and glides there (CSS transition), so it moves smoothly
  const ahead = song && song.playing ? Math.min(dur, pos + 1000) : pos;
  mwBar.style.transform = `scaleX(${song ? ahead / dur : 0})`;
  $('#mw-pos').textContent = clockOf(pos);
  $('#mw-left').textContent = song ? '-' + clockOf(dur - pos) : '0:00';
  $('#mf-bar').style.width = (song ? 100 * pos / dur : 0) + '%';
  $('#mf-pos').textContent = clockOf(pos); $('#mf-dur').textContent = clockOf(song ? dur : 0);
}
setInterval(tickSong, 1000);
$$('[data-sp]').forEach(b => b.addEventListener('click', async () => {
  const act = b.dataset.sp;
  if (song && song.held && act === 'toggle') {  // Spotify had stopped: start the kept song where it stopped
    song.playing = true; song.held = false; songAt = Date.now(); holdUntil = Date.now() + 4000; drawMusic(song);
    try {
      const r = await api('/api/spotify', {action: 'play_uri', uri: song.uri || ('spotify:track:' + song.id), position_ms: song.progress_ms});
      if (!r.ok && r.say) toast(r.say);
      setTimeout(refresh, 1500);
    } catch (e) { toast(e.message); holdUntil = 0; }
    return;
  }
  if (song && act === 'toggle') {  // flip at once, and hold it while Spotify catches up
    song.progress_ms = Math.min(song.duration_ms, song.progress_ms + (song.playing ? Date.now() - songAt : 0));
    songAt = Date.now(); song.playing = !song.playing; holdUntil = Date.now() + 3000;
    mwPP.classList.toggle('playing', song.playing); drawMusic(song);
  }
  try {  // the app knows what it shows: say pause or play outright (no round trip to ask Spotify first)
    const send = act === 'toggle' && song ? (song.playing ? 'resume' : 'pause') : act;
    const r = await api('/api/spotify', {action: send});
    if (r.say && !r.ok) toast(r.say);
    setTimeout(refresh, act === 'toggle' ? 1500 : 700);
  } catch (e) { toast(e.message); holdUntil = 0; }
}));

// --- 3d printer ------------------------------------------------------------------------------------------
// Progress straight from the printer (M27, read-only), so it's right whatever started the print; time left from
// how fast it has really been going. Temperatures come with the regular status.
let pr = {pct: null, bytes: null, total: null, state: 'offline', samples: store.get('pr-samples', []), stillSince: Date.now()};
async function loadPrinter(status) {
  // Batsy's server decides whether it's printing (it knows a print stopped on the screen, which the firmware keeps
  // calling "printing"); the printer itself gives the live percentage while it is
  const p = status && status.printer;
  if (!p || !p.connected) { pr.state = 'offline'; return drawPrinter(p); }
  if (p.uploading != null) {  // copying the G-code to the printer's card: the liquid shows how far
    pr.state = 'uploading'; pr.pct = p.uploading; return drawPrinter(p);
  }
  if (!p.printing && !p.paused && p.uploading == null) {
    pr.state = p.done ? 'done' : 'idle';  // "done" for 15 minutes after a print, then back to the controls
    pr.pct = pr.state === 'done' ? 100 : null;
    if (pr.state === 'idle') { pr.samples = []; store.set('pr-samples', []); }
    return drawPrinter(p);
  }
  if (p.streaming) {  // printing straight from the laptop: its own process has the USB port, Batsy has the numbers
    pr.pct = p.progress; pr.state = p.paused ? 'paused' : (p.hotend_target && p.hotend < p.hotend_target - 8) ? 'heating' : 'printing';
    return drawPrinter(p);
  }
  try {
    const r = await api('/api/printer', {action: 'query', gcode: 'M27'}, 12000);
    const m = (r.lines || []).join(' ').match(/printing byte (\d+)\/(\d+)/);
    if (m) {
      const [b, t] = [+m[1], +m[2]];
      if (b !== pr.bytes) pr.stillSince = Date.now();
      if (pr.total && pr.total !== t) pr.samples = [];  // a different file
      pr.bytes = b; pr.total = t; pr.pct = t ? 100 * b / t : 0;
      pr.samples = pr.samples.filter(x => Date.now() - x[0] < 20 * 60000).concat([[Date.now(), b]]);
      store.set('pr-samples', pr.samples);
    } else if (p.progress != null) pr.pct = p.progress;
  } catch { if (p.progress != null) pr.pct = p.progress; }
  const still = Date.now() - pr.stillSince > 45000 && !p.hotend_target;
  pr.state = p.paused || still ? 'paused' : (p.hotend_target && p.hotend < p.hotend_target - 8) ? 'heating' : 'printing';
  drawPrinter(p);
}
function leftText() {
  const s = pr.samples;
  if (s.length < 3 || !pr.total) return '';
  const [t0, b0] = s[0], [t1, b1] = s[s.length - 1];
  if (b1 <= b0 || t1 - t0 < 60000) return '';
  const secs = (pr.total - b1) / ((b1 - b0) / ((t1 - t0) / 1000));
  return dur(secs) + ' left';
}
function drawPrinter(p) {
  const card = $('#pw');
  card.className = 'w pw ' + pr.state;
  const shown = pr.pct == null ? null : Math.min(100, pr.pct);
  $('#pw-liquid').style.height = (shown == null ? 0 : Math.max(4, shown)) + '%';
  $('#pw-pct').textContent = shown == null ? (pr.state === 'offline' ? 'Off' : 'Idle') : (shown < 10 ? shown.toFixed(1) : Math.floor(shown)) + '%';
  $('#pw-state').textContent = {printing: 'Printing', heating: 'Heating up', paused: 'Paused', done: 'Done', idle: 'Ready',
    offline: 'Not connected', uploading: 'Copying to card'}[pr.state];
  const left = pr.state === 'printing' ? (p && p.remaining_s ? dur(p.remaining_s) + ' left' : leftText()) : pr.state === 'paused' ? 'paused' : pr.state === 'heating' ? 'heating up'
    : pr.state === 'uploading' ? 'copying · then it prints' : '';
  $('#pw-left').textContent = left || (pr.state === 'printing' ? 'working out the time…' : '');
  $('#pw-file').textContent = p && p.job && pr.state !== 'idle' ? p.job
    : pr.state === 'idle' && pr.stoppedAt ? `Last print stopped at ${pr.stoppedAt.toFixed(1)}%`
    : (pr.total && pr.state !== 'idle' ? `${(pr.total / 1e6).toFixed(1)} MB file` : '');
  // the print's details on one quiet line; when it'll be done goes next to the time left
  const info = p && p.info, busy = ['printing', 'heating', 'paused', 'uploading'].includes(pr.state);
  const meta = busy && info ? [info.metres ? `${info.metres} m of filament` : '', info.profile || '',
    info.total ? info.total.replace(/ \d+s$/, '') + ' total' : ''].filter(Boolean).join('  ·  ') : '';
  $('#pw-grams').textContent = busy && info && info.grams ? `${Math.round(info.grams)} g` : '';
  if ($('#pw-info').textContent !== meta) $('#pw-info').textContent = meta;
  $('#pw-done').textContent = pr.state === 'printing' && p && p.remaining_s
    ? 'done at ' + new Date(Date.now() + p.remaining_s * 1000).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'}) : '';
  const t = (v, g) => v == null ? '–' : `${Math.round(v)}°` + (g ? ` / ${Math.round(g)}°` : '');
  $('#pw-noz').textContent = t(p && p.hotend, p && p.hotend_target);
  $('#pw-bed').textContent = t(p && p.bed, p && p.bed_target);
  const btns = pr.state === 'printing' || pr.state === 'heating' ? [['pause', 'Pause'], ['cancel', 'Stop', 'danger']]
    : pr.state === 'paused' ? [['resume', 'Resume'], ['cancel', 'Stop', 'danger']]
    : pr.state === 'uploading' ? [['cancel', 'Stop', 'danger']] : pr.state === 'offline' ? [] : null;
  const html = btns ? btns.map(([a, l, c]) => `<button data-pr="${a}" class="${c || ''}">${l}</button>`).join('')
    : `<div class="pw-grid">${[['home', 'Home'], ['preheat', 'PLA'], ['preheat-petg', 'PETG'], ['cool', 'Cool'],
        ['bed-down', 'Bed ↓ 10'], ['bed-up', 'Bed ↑ 10'], ['motors_off', 'Motors off']]
        .map(([a, l]) => `<button data-pr="${a}">${l}</button>`).join('')}</div>`;
  if ($('#pw-btns').innerHTML !== html) $('#pw-btns').innerHTML = html;
}
$('#pw-btns').addEventListener('click', async e => {
  const b = e.target.closest('[data-pr]');
  if (!b) return;
  const a = b.dataset.pr;
  if (a === 'cancel' && !await confirmBox('Stop the print? It can\'t be resumed after this.', b, {danger: true, yes: 'Stop'})) return;
  const heat = {preheat: ['pla', 'PLA (190° / 60°)'], 'preheat-petg': ['petg', 'PETG (235° / 75°)']}[a];
  if (heat && !await confirmBox(`Heat the printer for ${heat[1]}?`, b, {yes: 'Heat'})) return;
  // an Ender-5 moves its bed: the bed going down is Z going up
  const body = heat ? {action: 'preheat', material: heat[0]} : a === 'bed-down' ? {action: 'move', axis: 'Z', mm: 10}
    : a === 'bed-up' ? {action: 'move', axis: 'Z', mm: -10} : {action: a};
  try { toast((await api('/api/printer', body, a === 'home' ? 90000 : 20000)).say); setTimeout(refresh, 1500); }
  catch (err) { toast(err.message); }
});

// --- the newest slice ---------------------------------------------------------------------------------------
let sliceT = 0;
async function loadSlice() {
  let s;
  try { s = await api('/api/slice'); } catch { return; }
  if (!s || !s.t) return;
  $('#sw-title').textContent = s.title;
  $('#sw-meta').textContent = [s.time, s.grams ? Math.round(s.grams) + '\u00a0g' : '', s.metres ? s.metres + '\u00a0m' : '']
    .filter(Boolean).join(' · ');  // (a number never splits from its unit)
  if (s.t !== sliceT) {  // a new slice: fetch its picture (with the token, so not a plain <img src>)
    sliceT = s.t;
    try {
      const r = await fetch('/api/slice/preview', {headers: {Authorization: 'Bearer ' + store.get('token', '')}});
      if (r.ok) $('#sw-img').src = URL.createObjectURL(await r.blob());
    } catch { /* keep the old one */ }
  }
}
// --- models waiting to slice: choose how each sits, right on the slice card -----------------------------------------
let plateV = -1, plateTimer = null;
async function loadPlate() {
  let p;
  try { p = await api('/api/plate'); } catch { return; }
  const box = $('#sw-plate'), a = p.asking;
  box.hidden = !a && !(p.waiting || []).length;
  box.classList.toggle('waiting', !a);  // waiting: a bar at the bottom; asking: the whole card
  clearTimeout(plateTimer);
  if (box.hidden) return;
  plateTimer = setTimeout(loadPlate, a ? 2500 : 15000);  // quicker while a question is open
  if (a) {
    $('#sp-title').textContent = `How should the ${a.model} sit?` + (a.of > 1 ? ` (${a.n} of ${a.of})` : '');
    $('#sp-btns').hidden = true;
    if (a.v === plateV) return;
    plateV = a.v;
    const tok = store.get('token', '');
    $('#sp-opts').innerHTML = a.options.map((o, k) => `<button class="sp-opt${o.best ? ' best' : ''}" data-k="${k}">
      <b>${k + 1}</b><img alt="" data-k="${k}"><small>${esc(o.how)}</small></button>`).join('');
    for (const img of $('#sp-opts').querySelectorAll('img')) {  // with the token: not a plain <img src>
      fetch(`/api/plate/img?k=${img.dataset.k}&v=${a.v}`, {headers: {Authorization: 'Bearer ' + tok}})
        .then(r => r.ok ? r.blob() : null).then(b => { if (b) img.src = URL.createObjectURL(b); }).catch(() => {});
    }
  } else {
    plateV = -1;
    const w = p.waiting;
    $('#sp-title').textContent = (w.length > 1 ? w.slice(0, -1).join(', ') + ' and ' + w[w.length - 1] : w[0]) + ' waiting to slice';
    $('#sp-opts').innerHTML = '';
    $('#sp-btns').hidden = false;
    $('#sp-btns .go').textContent = w.length > 1 ? 'Choose positions' : 'Choose position';
  }
}
let plateBusy = false;
async function plateDo(body, el) {
  if (plateBusy) return;  // one at a time: repeated taps used to start a slice each
  plateBusy = true;
  $('#sw-plate').classList.add('busy');
  try {
    if (el) el.disabled = true;
    const r = await api('/api/plate', body, 60000);
    if (r.say) toast(r.say.length > 140 ? r.say.slice(0, 137) + '…' : r.say);
  } catch (err) { toast(err.message); }
  finally {
    if (el) el.disabled = false;
    plateBusy = false; $('#sw-plate').classList.remove('busy');
    plateV = -1; loadPlate(); setTimeout(loadSlice, 4000);
  }
}
$('#sp-opts').addEventListener('click', e => { const b = e.target.closest('.sp-opt'); if (b) plateDo({pick: +b.dataset.k}, b); });
$('#sp-btns').addEventListener('click', e => { const b = e.target.closest('button[data-start]'); if (b) plateDo({start: b.dataset.start}, b); });
$('#sp-x').addEventListener('click', e => plateDo({cancel: true}, e.currentTarget));
$('#sw-pos').addEventListener('click', e => plateDo({start: 'again'}, e.currentTarget));  // choose how it sits, re-slice

$('#sw-3d').addEventListener('click', () => {
  const box = $('#slice3d');
  $('#slice3d-frame').src = '/app/slice.html';
  box.hidden = false;
  requestAnimationFrame(() => box.classList.add('on'));
});
addEventListener('message', e => {
  if (e.data && e.data.closeSlice) {
    const box = $('#slice3d');
    box.classList.remove('on');
    setTimeout(() => { box.hidden = true; $('#slice3d-frame').src = 'about:blank'; }, 280);
  }
});
$('#sw-print').addEventListener('click', async e => {
  const t = $('#sw-title').textContent, meta = $('#sw-meta').textContent;
  if (!await confirmBox(`Print ${t}? ${meta}. Is the bed clear?`, e.currentTarget, {yes: 'Print'})) return;
  try { toast((await api('/api/printer', {action: 'print_last'}, 20000)).say); } catch (err) { toast(err.message); }
});

// --- files -----------------------------------------------------------------------------------------------
const HEROES = {Jonathan: 'Batman', Karan: 'Nightwing', Rehaan: 'Superman'};
let files = [];
async function loadFiles() {
  try { files = (await api('/api/files')).files || []; } catch { return; }
  $('#f-count').textContent = files.length || '';
  $('#files').innerHTML = files.slice(0, 7).map((f, i) => `<li data-i="${i}"><span class="who ${esc(f.from)}">${esc((HEROES[f.from] || f.from || '?')[0])}</span>
    <b>${esc(f.name)}</b><small>${ago(f.t)}</small></li>`).join('') || '<li class="empty">Nothing received yet</li>';
}
$('#files').addEventListener('click', async e => {
  const li = e.target.closest('[data-i]');
  if (!li) return;
  const f = files[+li.dataset.i];
  // near the laptop (same Wi-Fi, or the USB cable): open it there; away: open it here, on the phone
  const near = await whereAmI();
  const pick = await confirmBox(`Open “${f.name}”`, li, near
    ? {yes: 'On the laptop', alt: 'On this phone'} : {yes: 'On this phone', alt: 'On the laptop'});
  if (!pick) return;
  const there = (pick === true) === near;
  try {
    if (there) await onLaptop(api('/api/files', {action: 'open', path: f.path}), f.name);
    else viewHere(await api('/api/files', {action: 'here', path: f.path}));
  } catch (err) { toast(err.message); }
});

// "Opening on the laptop": the filling bat on a glass card, rising while the laptop fetches and opens it
async function onLaptop(job, name) {
  const box = document.createElement('div');
  box.className = 'glass-ask floating busy';
  box.innerHTML = `<div class="glass-card"></div>`;
  const bat = $('#viewer-load .fillbat').cloneNode(true);
  bat.querySelector('clipPath').id = 'bbclip';
  bat.querySelector('[clip-path]').setAttribute('clip-path', 'url(#bbclip)');
  const lvl = bat.querySelector('#vlevel'); lvl.removeAttribute('id');
  const card = box.querySelector('.glass-card');
  card.append(bat);
  card.insertAdjacentHTML('beforeend', '<b>Opening on the laptop</b><small></small>');
  card.querySelector('small').textContent = name;
  document.body.appendChild(box);
  requestAnimationFrame(() => requestAnimationFrame(() => box.classList.add('on')));
  const fill = f => { lvl.style.transform = `translateY(${(1 - f) * 12 - 3}px)`; };
  fill(0.05);
  const t0 = Date.now();  // no progress from the laptop: rise quickly at first, then ever slower towards 90%
  const tick = setInterval(() => fill(0.9 * (1 - Math.exp(-(Date.now() - t0) / 2500))), 200);
  let msg;
  try { msg = (await job).say; fill(1); card.querySelector('b').textContent = 'Opened on the laptop'; }
  catch (err) { msg = err.message; card.querySelector('b').textContent = 'Couldn’t open it'; }
  clearInterval(tick);
  await new Promise(r => setTimeout(r, 700));
  box.classList.remove('on'); setTimeout(() => box.remove(), 400);
  toast(msg);
}

let whereAt = 0, wasNear = true;
async function whereAmI() {
  if (Date.now() - whereAt < 60000) return wasNear;  // the laptop caches it too: a check costs ~1 s
  try { wasNear = (await api('/api/where', null, 15000)).near; whereAt = Date.now(); } catch {}
  return wasNear;
}

// the file, inside the app: pictures, text, video and PDFs in a viewer; Share hands it to iOS (Save to Files, …)
async function viewHere(r) {
  if (!r.ok) return toast(r.say);
  const ext = (r.name.split('.').pop() || '').toLowerCase();
  const v = $('#viewer'), body = $('#viewer-body');
  $('#viewer-name').textContent = r.name;
  body.innerHTML = '';
  v.hidden = false; requestAnimationFrame(() => v.classList.add('on'));
  // the same filling bat as the 3D view, rising with the download
  const load = $('#viewer-load'), lvl = $('#vlevel'), mb = n => (n / 1e6).toFixed(1);
  const fill = f => { lvl.style.transform = `translateY(${(1 - Math.min(1, f)) * 12 - 3}px)`; };
  fill(0.04); load.hidden = false; $('#viewer-n').textContent = 'connecting…';
  let blob = r.blob;
  if (!blob) try {
    const res = await fetch(r.url);
    if (!res.ok) throw 0;
    const total = +res.headers.get('Content-Length') || 0, t0 = performance.now(), parts = [];
    let got = 0;
    for (const reader = res.body.getReader();;) {
      const {done, value} = await reader.read();
      if (done) break;
      parts.push(value); got += value.length;
      if (total) {
        fill(got / total);
        const left = (total - got) / (got / Math.max((performance.now() - t0) / 1000, 0.05));
        $('#viewer-n').textContent = `${Math.round(100 * got / total)}% · ${mb(got)} of ${mb(total)} MB` + (left > 1.5 ? ` · ${Math.ceil(left)} s left` : '');
      } else { fill(Math.min(0.9, got / 5e6)); $('#viewer-n').textContent = `${mb(got)} MB`; }
    }
    fill(1);
    blob = new Blob(parts, {type: res.headers.get('Content-Type') || ''});
    if (r.cache) keepFile(r.cache, blob);
  } catch { load.hidden = true; body.innerHTML = '<div class="viewer-wait">Couldn’t download it.</div>'; return; }
  setTimeout(() => { load.hidden = true; }, 250);  // let it show full for a moment
  const url = URL.createObjectURL(blob);
  v.dataset.url = url; v.file = new File([blob], r.name, {type: blob.type});
  if (/^(png|jpe?g|gif|webp|heic|bmp|svg)$/.test(ext)) body.innerHTML = `<img src="${url}" alt="">`;
  else if (/^(mp4|mov|m4v|webm)$/.test(ext)) body.innerHTML = `<video src="${url}" controls playsinline></video>`;
  else if (/^(mp3|m4a|wav|aac|ogg)$/.test(ext)) body.innerHTML = `<audio src="${url}" controls></audio>`;
  else if (/^(txt|md|py|js|ts|json|csv|c|cpp|h|ino|ino|html|css|toml|yaml|yml|log|gcode|sh)$/.test(ext) && blob.size < 2e6) {
    const pre = document.createElement('pre'); pre.textContent = await blob.text(); body.replaceChildren(pre);
  } else body.innerHTML = `<iframe src="${url}" title="${esc(r.name)}"></iframe>`;  // PDFs, documents: the phone's own viewer (already downloaded)
}
function closeViewer() {
  noteStack = []; $('#viewer').classList.remove('notes'); $('#viewer-search').hidden = true; $('#viewer-share').hidden = false;
  const v = $('#viewer');
  v.classList.remove('on');
  setTimeout(() => { v.hidden = true; $('#viewer-load').hidden = true; $('#viewer-body').innerHTML = ''; if (v.dataset.url) URL.revokeObjectURL(v.dataset.url); }, 280);
}
$('#viewer-close').addEventListener('click', () => {
  if (noteStack.length > 1) { noteStack.pop(); openNote(noteStack.pop()); } else closeViewer();
});
$('#viewer-share').addEventListener('click', async () => {
  const f = $('#viewer').file;
  try {
    if (f && navigator.canShare && navigator.canShare({files: [f]})) await navigator.share({files: [f], title: f.name});
    else { const a = document.createElement('a'); a.href = $('#viewer').dataset.url; a.download = f.name; a.click(); }
  } catch {}
});

// --- the Batman drive (the PC's /batman, as cloud storage) -----------------------------------------------------
// Listings are kept on the phone (they show at once, even with the PC off); thumbnails and opened files are kept
// in the phone's cache, so what you've seen once opens instantly, offline too.
const cloud = {path: '/batman', k: store.get('cloudK', ''), stack: []};
const ICONS = {pdf: 'PDF', doc: 'DOC', docx: 'DOC', xls: 'XLS', xlsx: 'XLS', ppt: 'PPT', pptx: 'PPT', zip: 'ZIP', rar: 'ZIP',
  '7z': 'ZIP', mp4: '▶', mov: '▶', mkv: '▶', mp3: '♪', m4a: '♪', wav: '♪', stl: '3D', '3mf': '3D', step: '3D', gcode: 'G',
  txt: 'TXT', md: 'TXT', py: '</>', js: '</>', ino: '</>', c: '</>', cpp: '</>', json: '{ }', csv: 'CSV'};
const extOf = n => (n.includes('.') ? n.split('.').pop() : '').toLowerCase();
const canThumb = n => /^(jpe?g|png|gif|webp|bmp|heic|tiff?|pdf)$/.test(extOf(n));
const human = b => b >= 1e9 ? (b / 1e9).toFixed(1) + ' GB' : b >= 1e6 ? (b / 1e6).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1e3)) + ' KB';
function tile(f, i) {
  const ext = extOf(f.name);
  const pic = f.dir ? '<span class="ct-ic folder"></span>'
    : canThumb(f.name) && cloud.k
      ? `<img loading="lazy" alt="" src="/api/cloud/thumb?p=${encodeURIComponent(f.path)}&v=${f.t}&s=${f.size}&k=${cloud.k}" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'ct-ic',textContent:'${esc(ICONS[ext] || ext.toUpperCase().slice(0, 4) || '•')}'}))">`
      : `<span class="ct-ic">${esc(ICONS[ext] || ext.toUpperCase().slice(0, 4) || '•')}</span>`;
  return `<button class="ct" data-i="${i}"><div class="ct-pic">${pic}</div><b>${esc(f.name)}</b>
    <small>${f.dir ? 'Folder' : human(f.size) + ' · ' + ago(f.t)}</small></button>`;
}
async function cloudGet(qs, cacheAs) {
  try {
    const r = await api('/api/cloud?' + qs, null, 35000);
    if (r.k) { cloud.k = r.k; store.set('cloudK', r.k); }
    if (!r.error) store.set(cacheAs, r);
    return r;
  } catch (e) { return {error: e.message, offline: true}; }
}
function drawSpace(r) {
  if (!r.total) return;
  $('#cw-used').style.width = Math.max(1, 100 * (1 - r.free / r.total)) + '%';
  $('#cw-space').textContent = `${human(r.total - r.free)} used · ${human(r.free)} free`;
}
let cwItems = [];
function drawCloudWidget(r, note = '') {
  cwItems = (r.items || []).slice(0, 6);
  drawSpace(r);
  $('#cw-grid').innerHTML = cwItems.map(tile).join('') ||
    `<div class="cw-empty">${esc(note || r.error || 'Nothing here yet. Open All files and tap ＋ to upload.')}</div>`;
  if (note && cwItems.length) $('#cw-space').textContent += ' · ' + note;
}
async function loadCloudWidget() {
  if ($('#cloud-w').classList.contains('w-hidden') || getComputedStyle($('#cloud-w')).display === 'none') return;
  const old = store.get('cloudRecent', null);
  if (old) drawCloudWidget(old);
  const r = await cloudGet('mode=recent', 'cloudRecent');
  if (r.error) drawCloudWidget(old || r, r.setup ? r.error : r.offline ? 'PC asleep: showing what was there' : r.error);
  else drawCloudWidget(r);
}
$('#cw-grid').addEventListener('click', e => {
  const t = e.target.closest('.ct'); if (!t) return;
  openCloud(cwItems[+t.dataset.i], t);
});
$('#cw-all').addEventListener('click', () => cloudOpenPanel());

// the full drive: its own screen
let clItems = [];
function cloudOpenPanel() {
  const c = $('#cloud');
  c.hidden = false; requestAnimationFrame(() => c.classList.add('on'));
  history.pushState({cloud: 1}, '');
  cloudGo('/batman');
}
function cloudClose() {
  const c = $('#cloud');
  c.classList.remove('on'); setTimeout(() => { c.hidden = true; }, 280);
}
window.addEventListener('popstate', () => {
  if (!$('#viewer').hidden) return closeViewer();
  if ($('#cloud').hidden) return;
  if (cloud.stack.length) { cloudGo(cloud.stack.pop(), true); history.pushState({cloud: 1}, ''); }
  else cloudClose();
});
function drawFolder(r, note = '') {
  const items = (r.items || []).slice().sort((a, b) => (b.dir - a.dir) || (r.search ? b.t - a.t : a.name.localeCompare(b.name, undefined, {numeric: true})));
  clItems = items;
  $('#cl-grid').innerHTML = items.map(tile).join('') ||
    `<div class="cw-empty">${r.error ? esc(r.error) : r.search ? 'Nothing matches.' : 'This folder is empty. Tap ＋ to upload.'}</div>`;
  $('#cl-status').textContent = note || (r.total ? `${items.length} item${items.length === 1 ? '' : 's'} · ${human(r.free)} free` : '');
}
async function cloudGo(path, back = false) {
  if (!back && path !== cloud.path && cloud.path) cloud.stack.push(cloud.path);
  if (path === '/batman') cloud.stack = [];
  cloud.path = path;
  $('#cl-search').value = '';
  $('#cl-name').textContent = path === '/batman' ? 'Batman drive' : path.split('/').pop();
  $('#cl-path').textContent = path === '/batman' ? '' : path.replace('/batman', 'Batman drive');
  $('#cl-back').textContent = path === '/batman' ? '✕' : '‹';
  const key = 'cloud:' + path, old = store.get(key, null);
  if (old) drawFolder(old); else { $('#cl-grid').innerHTML = ''; $('#cl-status').textContent = 'Loading…'; }
  const r = await cloudGet('mode=list&path=' + encodeURIComponent(path), key);
  if (cloud.path !== path) return;
  if (r.error) drawFolder(old || r, old ? (r.offline ? 'PC asleep: showing the saved copy' : r.error) : '');
  else drawFolder(r);
}
$('#cl-back').addEventListener('click', () => history.back());
$('#cl-grid').addEventListener('click', e => {
  const t = e.target.closest('.ct'); if (!t) return;
  const f = clItems[+t.dataset.i];
  if (f.dir) { cloudGo(f.path); history.pushState({cloud: 1}, ''); } else openCloud(f, t);
});
let searchT;
$('#cl-search').addEventListener('input', e => {
  clearTimeout(searchT);
  const q = e.target.value.trim();
  if (!q) return cloudGo(cloud.path, true);
  searchT = setTimeout(async () => {
    $('#cl-status').textContent = 'Searching…';
    const r = await cloudGet('mode=search&q=' + encodeURIComponent(q), 'cloudSearch');
    if ($('#cl-search').value.trim() === q) drawFolder({...r, search: true});
  }, 350);
});
$('#cl-file').addEventListener('change', async e => {
  const list = [...e.target.files]; e.target.value = '';
  for (const [n, f] of list.entries()) {
    $('#cl-status').textContent = `Uploading ${f.name} (${n + 1} of ${list.length}) · 0%`;
    try {
      await new Promise((res, rej) => {  // XHR: it reports upload progress, fetch doesn't
        const x = new XMLHttpRequest();
        x.open('POST', `/api/cloud/upload?dir=${encodeURIComponent(cloud.path)}&name=${encodeURIComponent(f.name)}`);
        x.setRequestHeader('Authorization', 'Bearer ' + store.get('token', ''));
        x.upload.onprogress = p => { if (p.lengthComputable) $('#cl-status').textContent = `Uploading ${f.name} (${n + 1} of ${list.length}) · ${Math.round(100 * p.loaded / p.total)}%`; };
        x.onload = () => x.status === 200 ? res() : rej(new Error(JSON.parse(x.responseText || '{}').say || 'Upload failed'));
        x.onerror = () => rej(new Error('Upload failed: is the PC on?'));
        x.send(f);
      });
    } catch (err) { toast(err.message); break; }
  }
  cloudGo(cloud.path, true); loadCloudWidget();
});
$('#cl-new').addEventListener('click', async () => {
  const name = prompt('New folder name');
  if (!name || !name.trim()) return;
  try { toast((await api('/api/cloud', {action: 'mkdir', dir: cloud.path, name: name.trim()})).say); } catch (err) { toast(err.message); }
  cloudGo(cloud.path, true);
});

// a file from the drive: near the laptop you can open it there; otherwise (and always on offer) here, cached
async function openCloud(f, from) {
  const cacheKey = `/cloud-cache${f.path}?v=${f.t}`;
  let cached = null;
  try { cached = await (await caches.open('batsy-files')).match(cacheKey); } catch {}
  if (!cached) {
    const near = await whereAmI();
    if (near) {
      const pick = await confirmBox(`Open “${f.name}”`, from, {yes: 'On the laptop', alt: 'On this phone'});
      if (!pick) return;
      if (pick === true) return onLaptop(api('/api/cloud', {action: 'laptop', path: f.path}, 120000), f.name);
    }
  }
  if (cached) return viewHere({ok: true, name: f.name, blob: await cached.blob()});
  try { viewHere({...(await api('/api/cloud', {action: 'open', path: f.path}, 20000)), cache: cacheKey}); }
  catch (err) {  // why, as Batsy said it: the PC is asleep, the drive isn't set up, or the file is gone
    toast(err.message.startsWith('HTTP') ? 'Couldn’t get it from the PC, and it isn’t saved on this phone yet.' : err.message);
  }
}
async function keepFile(key, blob) {
  // phone storage is limited: keep about 400 MB of opened files, dropping the longest-unopened first
  try {
    const c = await caches.open('batsy-files');
    await c.put(key, new Response(blob, {headers: {'Content-Type': blob.type}}));
    const idx = store.get('fileCache', []).filter(x => x.k !== key);
    idx.push({k: key, n: blob.size, t: Date.now()});
    let total = idx.reduce((a, x) => a + x.n, 0);
    while (total > 400e6 && idx.length > 1) { const old = idx.shift(); total -= old.n; await c.delete(old.k); }
    store.set('fileCache', idx);
  } catch {}
}

// --- iPhone dashboard: long-press a widget to move, hide and add widgets (saved on this phone) -------------------
const WIDGETS = [  // key, element, name; this order is the default
  ['printer', '#pw', '3D printer'], ['slice', '#sw', 'Sliced'], ['home', '.home-w', 'Home'], ['brain', '#brain-w', 'Brain'],
  ['drive', '#cloud-w', 'Batman drive'], ['files', '.files-w', 'Received files'], ['computers', '#machines-w', 'Laptop, PC & HP'],
  ['music', '#mw', 'Music'], ['claude', '.claude-w', 'Claude usage'], ['batsy', '#batsy-w', 'Batsy']];
const wEl = k => $(WIDGETS.find(w => w[0] === k)[1]);
let layout = store.get('dashLayout', null);
function cleanLayout(l) {
  const keys = WIDGETS.map(w => w[0]);
  const order = (l && l.order || []).filter(k => keys.includes(k));
  keys.forEach((k, i) => {  // widgets added in later versions: in their default place
    if (order.includes(k)) return;
    const before = keys.slice(0, i).reverse().find(x => order.includes(x));
    order.splice(before ? order.indexOf(before) + 1 : 0, 0, k);
  });
  return {order, hidden: (l && l.hidden || []).filter(k => keys.includes(k))};
}
function applyLayout() {
  if (!document.body.classList.contains('dash')) return;
  layout = cleanLayout(layout);
  layout.order.forEach((k, i) => {
    const el = wEl(k); if (!el) return;
    el.style.order = i + 1;
    el.classList.toggle('w-hidden', layout.hidden.includes(k));
  });
}
applyLayout();

let editing = false;
function startEdit() {
  if (editing || !document.body.classList.contains('dash')) return;
  editing = true;
  navigator.vibrate && navigator.vibrate(15);
  document.body.classList.add('editing');
  WIDGETS.forEach(([k]) => {
    const el = wEl(k); if (!el || el.querySelector(':scope > .w-ctl')) return;
    el.insertAdjacentHTML('beforeend', `<button class="w-ctl w-del" data-k="${k}" aria-label="Hide">−</button>
      <span class="w-ctl w-grab" data-k="${k}" aria-label="Move">≡</span>`);
  });
  $('#editbar').hidden = false;
}
function endEdit() {
  editing = false;
  document.body.classList.remove('editing');
  document.querySelectorAll('.w-ctl').forEach(x => x.remove());
  $('#editbar').hidden = true; $('#addsheet').hidden = true;
  store.set('dashLayout', layout);
}
$('#ed-done').addEventListener('click', endEdit);
$('#ed-reset').addEventListener('click', () => { layout = cleanLayout(null); applyLayout(); toast('Back to the original layout'); });
$('#ed-add').addEventListener('click', () => {
  const list = layout.hidden.map(k => WIDGETS.find(w => w[0] === k));
  $('#as-list').innerHTML = list.map(([k, , name]) => `<button class="as-item" data-k="${k}"><span>＋</span>${esc(name)}</button>`).join('')
    || '<p class="empty">Every widget is already on the dashboard.</p>';
  $('#addsheet').hidden = false;
});
$('#as-close').addEventListener('click', () => { $('#addsheet').hidden = true; });
$('#as-list').addEventListener('click', e => {
  const b = e.target.closest('.as-item'); if (!b) return;
  layout.hidden = layout.hidden.filter(k => k !== b.dataset.k);
  applyLayout(); b.remove();
  wEl(b.dataset.k).scrollIntoView({behavior: 'smooth', block: 'center'});
  if (!layout.hidden.length) $('#addsheet').hidden = true;
});
document.addEventListener('click', e => {
  if (!editing) return;
  const del = e.target.closest('.w-del');
  if (del) { e.stopPropagation(); e.preventDefault(); layout.hidden.push(del.dataset.k); applyLayout(); return; }
  if (e.target.closest('.w') && !e.target.closest('.w-ctl')) { e.stopPropagation(); e.preventDefault(); }  // no buttons while editing
}, true);

// long-press: 550 ms without moving (dashboard: edit in place; StandBy: the editor sheet)
let pressT = null, pressAt = null;
document.addEventListener('pointerdown', e => {
  if (editing || !$('#sbedit').hidden || !e.target.closest('#views .w')) return;
  pressAt = [e.clientX, e.clientY];
  pressT = setTimeout(() => {
    pressT = null;
    if (document.body.classList.contains('dash')) startEdit(); else sbOpen();
  }, 550);
});
document.addEventListener('pointermove', e => {
  if (pressT && Math.hypot(e.clientX - pressAt[0], e.clientY - pressAt[1]) > 10) { clearTimeout(pressT); pressT = null; }
});
['pointerup', 'pointercancel'].forEach(ev => document.addEventListener(ev, () => { clearTimeout(pressT); pressT = null; }));
document.addEventListener('contextmenu', e => { if (e.target.closest('.w')) e.preventDefault(); });

// dragging by the ≡ handle: the widget follows your finger; passing a neighbour's middle swaps them
let drag = null;
document.addEventListener('pointerdown', e => {
  const g = e.target.closest('.w-grab'); if (!g || !editing) return;
  e.preventDefault();
  const el = wEl(g.dataset.k);
  drag = {k: g.dataset.k, el, y0: e.clientY, base: 0, id: e.pointerId};
  el.classList.add('dragging');
  g.setPointerCapture && g.setPointerCapture(e.pointerId);
});
document.addEventListener('pointermove', e => {
  if (!drag || e.pointerId !== drag.id) return;
  e.preventDefault();
  const dy = e.clientY - drag.y0 - drag.base;
  drag.el.style.transform = `translateY(${dy}px) scale(1.03)`;
  const vis = layout.order.filter(k => !layout.hidden.includes(k));
  const i = vis.indexOf(drag.k);
  const r = drag.el.getBoundingClientRect(), mid = r.top + r.height / 2;
  const swap = (j) => {
    const other = wEl(vis[j]), before = drag.el.getBoundingClientRect().top;
    const a = layout.order.indexOf(drag.k), b = layout.order.indexOf(vis[j]);
    [layout.order[a], layout.order[b]] = [layout.order[b], layout.order[a]];
    applyLayout();
    drag.el.style.transform = '';
    drag.base += drag.el.getBoundingClientRect().top - before;  // the widget moved in the layout: keep it under the finger
    drag.el.style.transform = `translateY(${e.clientY - drag.y0 - drag.base}px) scale(1.03)`;
    other.animate([{transform: `translateY(${j > i ? 1 : -1}0px)`}, {transform: 'none'}], {duration: 180, easing: 'ease-out'});
  };
  if (i < vis.length - 1) { const n = wEl(vis[i + 1]).getBoundingClientRect(); if (mid > n.top + n.height / 2) swap(i + 1); }
  if (i > 0) { const p = wEl(vis[i - 1]).getBoundingClientRect(); if (mid < p.top + p.height / 2) swap(i - 1); }
  // near the top or bottom of the screen: scroll
  if (e.clientY < 90) scrollBy(0, -12); else if (e.clientY > innerHeight - 90) scrollBy(0, 12);
}, {passive: false});
['pointerup', 'pointercancel'].forEach(ev => document.addEventListener(ev, e => {
  if (!drag || e.pointerId !== drag.id) return;
  drag.el.classList.remove('dragging');
  drag.el.animate([{transform: drag.el.style.transform}, {transform: 'none'}], {duration: 160, easing: 'ease-out'});
  drag.el.style.transform = '';
  drag = null;
  store.set('dashLayout', layout);
}));

// --- Android StandBy: two stacks; long-press opens an editor to move widgets (between stacks too), hide, add ------
const SB_NAMES = {clock: 'Clock', ...Object.fromEntries(WIDGETS.map(w => [w[0], w[2]]))};
const sbEl = k => k === 'clock' ? $('.clock-w') : wEl(k);
const SB_DEFAULT = (() => {  // the stacks as the page is written; the drive starts hidden
  const keyOf = el => Object.keys(SB_NAMES).find(k => sbEl(k) === el);
  const col = id => [...$('#' + id).children].map(keyOf).filter(k => k && k !== 'drive');
  return {left: col('left'), right: col('right'), hidden: ['drive']};
})();
function sbClean(l) {
  const all = Object.keys(SB_NAMES), seen = new Set();
  const out = {};
  for (const c of ['left', 'right', 'hidden']) out[c] = ((l && l[c]) || []).filter(k => all.includes(k) && !seen.has(k) && seen.add(k));
  all.forEach(k => { if (!seen.has(k)) (SB_DEFAULT.hidden.includes(k) ? out.hidden : SB_DEFAULT.left.includes(k) ? out.left : out.right).push(k); });
  return out;
}
let sbLayout = sbClean(store.get('standbyLayout', null) || SB_DEFAULT);
function sbApply() {
  if (document.body.classList.contains('dash')) return;
  for (const c of ['left', 'right']) sbLayout[c].forEach(k => { const el = sbEl(k); el.classList.remove('w-hidden'); $('#' + c).appendChild(el); });
  sbLayout.hidden.forEach(k => sbEl(k).classList.add('w-hidden'));
  ['left', 'right'].forEach(c => { $('#' + c).scrollTop = 0; });
}
sbApply();
function sbDraw() {
  for (const c of ['left', 'right', 'hidden']) {
    $(`.sb-col[data-col="${c}"] .sb-list`).innerHTML = sbLayout[c].map(k => `<div class="sb-item" data-k="${k}">
      <button class="sb-x" aria-label="${c === 'hidden' ? 'Add' : 'Hide'}">${c === 'hidden' ? '＋' : '−'}</button>
      <b>${esc(SB_NAMES[k])}</b><span class="sb-grab">≡</span></div>`).join('') || '<div class="sb-empty">Empty</div>';
  }
}
function sbOpen() {
  navigator.vibrate && navigator.vibrate(15);
  sbDraw();
  const s = $('#sbedit'); s.hidden = false; requestAnimationFrame(() => s.classList.add('on'));
}
function sbClose() {
  const s = $('#sbedit'); s.classList.remove('on'); setTimeout(() => { s.hidden = true; }, 250);
  store.set('standbyLayout', sbLayout); sbApply(); loadCloudWidget();
}
$('#sb-done').addEventListener('click', sbClose);
$('#sb-reset').addEventListener('click', () => { sbLayout = sbClean(SB_DEFAULT); sbDraw(); });
$('#sbedit').addEventListener('click', e => {
  const x = e.target.closest('.sb-x'); if (!x) return;
  const k = x.closest('.sb-item').dataset.k;
  const from = ['left', 'right', 'hidden'].find(c => sbLayout[c].includes(k));
  sbLayout[from] = sbLayout[from].filter(y => y !== k);
  if (from === 'hidden') (sbLayout.left.length <= sbLayout.right.length ? sbLayout.left : sbLayout.right).push(k);
  else sbLayout.hidden.push(k);
  sbDraw();
});
// drag a row by ≡: it follows the finger; a gap shows where it will land, in either stack
let sbDrag = null;
$('#sbedit').addEventListener('pointerdown', e => {
  const g = e.target.closest('.sb-grab'); if (!g) return;
  e.preventDefault();
  const row = g.closest('.sb-item'), r = row.getBoundingClientRect();
  const ghost = row.cloneNode(true); ghost.classList.add('sb-ghost');
  Object.assign(ghost.style, {left: r.left + 'px', top: r.top + 'px', width: r.width + 'px'});
  document.body.appendChild(ghost);
  row.classList.add('sb-placeholder');
  sbDrag = {k: row.dataset.k, row, ghost, dy: e.clientY - r.top, dx: e.clientX - r.left, id: e.pointerId};
  g.setPointerCapture && g.setPointerCapture(e.pointerId);
});
$('#sbedit').addEventListener('pointermove', e => {
  if (!sbDrag || e.pointerId !== sbDrag.id) return;
  e.preventDefault();
  Object.assign(sbDrag.ghost.style, {left: e.clientX - sbDrag.dx + 'px', top: e.clientY - sbDrag.dy + 'px'});
  const col = [...document.querySelectorAll('.sb-col')].find(c => { const r = c.getBoundingClientRect(); return e.clientX >= r.left && e.clientX <= r.right; });
  if (!col) return;
  const list = col.querySelector('.sb-list');
  list.querySelector('.sb-empty') && list.querySelector('.sb-empty').remove();
  const after = [...list.querySelectorAll('.sb-item:not(.sb-placeholder)')].find(it => { const r = it.getBoundingClientRect(); return e.clientY < r.top + r.height / 2; });
  if (after) list.insertBefore(sbDrag.row, after); else list.appendChild(sbDrag.row);
}, {passive: false});
['pointerup', 'pointercancel'].forEach(ev => $('#sbedit').addEventListener(ev, e => {
  if (!sbDrag || e.pointerId !== sbDrag.id) return;
  sbDrag.ghost.remove(); sbDrag.row.classList.remove('sb-placeholder'); sbDrag = null;
  for (const c of ['left', 'right', 'hidden']) sbLayout[c] = [...$(`.sb-col[data-col="${c}"] .sb-list`).querySelectorAll('.sb-item')].map(it => it.dataset.k);
  sbDraw();
}));

// --- the brain (Obsidian vault): today at a glance, a quick note, and every note readable with links --------------
async function loadBrain() {
  let b;
  try { b = await api('/api/brain'); } catch { return; }
  $('#br-inbox').textContent = b.inbox ? `${b.inbox} in inbox` : '';
  const plan = b.plan.length ? `<ul class="br-plan">${b.plan.map(p => `<li class="${p.done ? 'done' : ''}">${p.done ? '☑' : '☐'} ${p.text}</li>`).join('')}</ul>` : '';
  const body = b.summary ? `<div class="br-sum">${b.summary}</div>`
    : b.count ? `<small>${b.day} · ${b.count} moment${b.count === 1 ? '' : 's'} so far</small><ul class="br-latest">${b.latest.map(x => `<li>${x}</li>`).join('')}</ul>`
    : `<small>${b.day} · nothing yet</small>`;
  $('#br-today').innerHTML = plan + body + (b.yesterday && !b.summary ? `<details class="br-y"><summary>Yesterday</summary>${b.yesterday}</details>` : '');
  $('#br-today').dataset.today = b.today;
}
$('#br-form').addEventListener('submit', async e => {
  e.preventDefault();
  const text = $('#br-in').value.trim();
  if (!text) return;
  $('#br-in').value = ''; $('#br-in').blur();
  try { toast((await api('/api/brain', {text})).say); setTimeout(loadBrain, 800); } catch (err) { toast(err.message); $('#br-in').value = text; }
});
$('#br-open').addEventListener('click', () => openNote('Home'));
$('#br-today').addEventListener('click', e => {
  const a = e.target.closest('[data-note]');
  openNote(a ? a.dataset.note : $('#br-today').dataset.today || 'Home');
});

let noteStack = [];
async function openNote(name) {
  const v = $('#viewer'), body = $('#viewer-body');
  v.classList.add('notes'); $('#viewer-share').hidden = true; $('#viewer-search').hidden = false;
  if (v.hidden) { v.hidden = false; requestAnimationFrame(() => v.classList.add('on')); }
  $('#viewer-name').textContent = name; $('#viewer-close').textContent = noteStack.length ? '‹' : '✕';
  let n;
  try { n = await api('/api/brain/note?n=' + encodeURIComponent(name)); } catch (err) { return toast(err.message); }
  if (n.error) { toast(`No note called ${name} yet.`); return; }
  noteStack.push(n.title);
  $('#viewer-name').textContent = n.title; $('#viewer-close').textContent = noteStack.length > 1 ? '‹' : '✕';
  body.innerHTML = `<article class="note">${n.html}</article>`;
  body.scrollTop = 0;
}
$('#viewer-body').addEventListener('click', e => {
  const a = e.target.closest('a[data-note]');
  if (a) { e.preventDefault(); openNote(a.dataset.note); return; }
  const hit = e.target.closest('[data-hit]');
  if (hit) openNote(hit.dataset.hit);
});
let brainT;
$('#viewer-search').addEventListener('input', e => {
  clearTimeout(brainT);
  const q = e.target.value.trim();
  if (!q) return;
  brainT = setTimeout(async () => {
    const r = await api('/api/brain/search?q=' + encodeURIComponent(q)).catch(() => ({hits: []}));
    if ($('#viewer-search').value.trim() !== q) return;
    $('#viewer-name').textContent = `Search: ${q}`;
    $('#viewer-body').innerHTML = `<div class="note hits">${r.hits.map(h => `<button class="hit" data-hit="${esc(h.path)}">
      <b>${esc(h.title)}</b><small>${esc(h.path.split('/').slice(0, -1).join(' › '))}</small><p>${esc(h.snippet)}</p></button>`).join('')
      || '<p class="empty">Nothing in your notes.</p>'}</div>`;
  }, 300);
});

// --- refresh: every 3 s while the app is open --------------------------------------------------------------
let busy = false;
async function refresh() {
  if (busy || document.hidden) return;
  busy = true;
  try {
    const s = await api('/api/status');
    drawBatsy(s.batsy || {}); drawClaude(s.claude); drawMachines(s.machines || []); drawMusic(s.spotify);
    takeEvents(s.events);
    takeModels(s.models);
    takePreview(s.model_preview);
    if (Date.now() - (window.prAt || 0) > 9000) { window.prAt = Date.now(); loadPrinter(s); } else drawPrinter(s.printer);
  } catch (e) {
    if (String(e.message).startsWith('Not paired')) toast(e.message);
  } finally { busy = false; }
}
refresh(); loadLight(); loadFiles(); loadSlice(); loadPlate(); loadCloudWidget(); loadBrain();
setInterval(() => { if (!document.hidden) { loadCloudWidget(); loadBrain(); } }, 60000);
setInterval(refresh, 3000);
setInterval(() => { if (!document.hidden) { loadLight(); loadFiles(); loadSlice(); loadPlate(); } }, 20000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) { refresh(); loadLight(); loadFiles(); loadSlice(); loadPlate(); loadCloudWidget(); loadBrain(); } });
