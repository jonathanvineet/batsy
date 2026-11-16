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
    if (!r.ok) throw new Error('HTTP ' + r.status);
    setLink(true);
    return await r.json();
  } catch (e) {
    if (e.name === 'AbortError') e = new Error('Batsy took too long');
    if (!String(e.message).startsWith('Not paired')) setLink(false);
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
function confirmBox(text, from, {danger = false, yes = 'Yes'} = {}) {
  const host = (from && from.closest('.w, .panel')) || document.body;
  host.querySelectorAll(':scope > .glass-ask').forEach(x => x.remove());
  const box = document.createElement('div');
  box.className = 'glass-ask' + (host === document.body ? ' floating' : '');
  box.innerHTML = `<div class="glass-card"><p></p><div class="glass-row"><button class="g-no">Cancel</button>
    <button class="g-yes${danger ? ' danger' : ''}"></button></div></div>`;
  box.querySelector('p').textContent = text;
  box.querySelector('.g-yes').textContent = yes;
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
    refresh(); loadLight(); loadFiles(); loadSlice(); showBell();
  } catch { $('#pairmsg').textContent = 'Can’t reach the laptop. Is Tailscale on?'; }
});
// notifications: iPhone only allows them in the installed app, after a tap
async function showBell() {
  if (!('PushManager' in window) || !('serviceWorker' in navigator) || !store.get('token', '')) return;
  if (/iPhone|iPad/.test(navigator.userAgent) && !standalone) return;  // iOS: only once it's on the home screen
  const reg = await navigator.serviceWorker.ready.catch(() => null);
  const sub = reg && await reg.pushManager.getSubscription();
  $('#bell').hidden = !!(sub && Notification.permission === 'granted');
}
$('#bell').addEventListener('click', async () => {
  try {
    if (await Notification.requestPermission() !== 'granted') { toast('Notifications are off in Settings › Batsy.'); return; }
    const reg = await navigator.serviceWorker.ready;
    const {key} = await api('/api/push/key');
    const pad = '='.repeat((4 - key.length % 4) % 4);
    const raw = Uint8Array.from(atob((key + pad).replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
    const sub = await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: raw});
    const r = await api('/api/push/subscribe', {subscription: sub.toJSON(), device: navigator.userAgent.match(/iPhone|iPad|Android/)?.[0] || 'browser'});
    toast(r.say); $('#bell').hidden = true;
  } catch (e) { toast('Couldn’t turn notifications on: ' + e.message); }
});
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
  document.body.classList.toggle('night', h >= 23 || h < 6);
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
}
const chat = store.get('chat', []);
function drawChat() {
  $('#chat').innerHTML = chat.slice(-10).map(m => `<div class="msg ${m.who}${m.wait ? ' wait' : ''}">${esc(m.text)}</div>`).join('');
  $('#chat').scrollTop = 1e6;
}
function openSheet() { $('#sheet').hidden = false; drawChat(); $('#ask-text').focus(); }
$('#batsy-w').addEventListener('click', openSheet);
$('#sheet').addEventListener('click', e => { if (e.target.id === 'sheet') $('#sheet').hidden = true; });
$('#ask').addEventListener('submit', async e => {
  e.preventDefault();
  const text = $('#ask-text').value.trim();
  if (!text) return;
  $('#ask-text').value = '';
  chat.push({who: 'you', text});
  const pending = {who: 'bot', text: 'Thinking…', wait: true};
  chat.push(pending); drawChat();
  try { pending.text = (await api('/api/batsy', {action: 'ask', text}, 180000)).say || 'Done.'; }
  catch (err) { pending.text = err.message; }
  delete pending.wait; store.set('chat', chat.slice(-30)); drawChat(); refresh();
});

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
function gauge(label, value, pct, cls) {
  return `<div class="gauge"><span>${label}<b>${value}</b></span><div class="bar"><i class="${cls}" style="width:${Math.min(100, pct || 0)}%"></i></div></div>`;
}
function drawMachines(ms) {
  pc = ms.find(m => m.kind === 'pc') || null;
  $('#t-pc-s').textContent = pc ? (pc.online ? 'on' : pc.online === false ? 'off' : '…') : 'not set up';
  $('#t-pc').classList.toggle('on', !!(pc && pc.online));
  $('#machines-w').innerHTML = ms.map(m => {
    const dot = m.online === true ? 'on' : m.online === false ? 'off' : '';
    if (!m.online) return `<div class="m"><h4><i class="${dot}"></i>${esc(m.name)}</h4>
      <p class="off-note">${m.online === false ? 'Off or asleep' : 'Checking…'}</p>${m.kind === 'pc' ? '<button class="act" data-pc="wake">Wake up</button>' : ''}</div>`;
    return `<div class="m"><h4><i class="on"></i>${esc(m.name)}</h4>
      ${gauge('CPU', Math.round(m.cpu || 0) + '%', m.cpu, level(m.cpu, 70, 90))}
      ${gauge('Temp', (m.temp ?? '–') + '°', m.temp, level(m.temp || 0, 80, 92))}
      ${gauge('Memory', m.ram_used_gb != null ? `${m.ram_used_gb}/${m.ram_total_gb} GB` : Math.round(m.ram) + '%', m.ram, level(m.ram, 75, 90))}
      ${gauge('Disk', m.disk_free_gb + ' GB free', m.disk, level(m.disk, 80, 92))}
      ${m.kind === 'pc' ? '<button class="act" data-pc="menu">Controls</button>'
        : m.battery != null ? `<small>Battery ${m.battery}%${m.charging ? ', charging' : ''}</small>` : ''}</div>`;
  }).join('');
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
  try {
    const r = await api('/api/spotify', {action: act});
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
  $('#sw-meta').textContent = [s.time, s.grams ? Math.round(s.grams) + ' g' : '', s.metres ? s.metres + ' m' : '']
    .filter(Boolean).join(' · ');
  if (s.t !== sliceT) {  // a new slice: fetch its picture (with the token, so not a plain <img src>)
    sliceT = s.t;
    try {
      const r = await fetch('/api/slice/preview', {headers: {Authorization: 'Bearer ' + store.get('token', '')}});
      if (r.ok) $('#sw-img').src = URL.createObjectURL(await r.blob());
    } catch { /* keep the old one */ }
  }
}
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
  if (!await confirmBox(`Open “${f.name}” on the laptop?`, li, {yes: 'Open'})) return;
  try { toast((await api('/api/files', {action: 'open', path: f.path})).say); } catch (err) { toast(err.message); }
});

// --- refresh: every 3 s while the app is open --------------------------------------------------------------
let busy = false;
async function refresh() {
  if (busy || document.hidden) return;
  busy = true;
  try {
    const s = await api('/api/status');
    drawBatsy(s.batsy || {}); drawClaude(s.claude); drawMachines(s.machines || []); drawMusic(s.spotify);
    if (Date.now() - (window.prAt || 0) > 9000) { window.prAt = Date.now(); loadPrinter(s); } else drawPrinter(s.printer);
  } catch (e) {
    if (String(e.message).startsWith('Not paired')) toast(e.message);
  } finally { busy = false; }
}
refresh(); loadLight(); loadFiles(); loadSlice();
setInterval(refresh, 3000);
setInterval(() => { if (!document.hidden) { loadLight(); loadFiles(); loadSlice(); } }, 20000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) { refresh(); loadLight(); loadFiles(); loadSlice(); } });
