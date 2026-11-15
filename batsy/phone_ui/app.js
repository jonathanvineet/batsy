// Batsy's phone app, StandBy style: swipe sideways between widgets / big clock / now playing; in the widgets view
// each half scrolls up and down through its own stack. Served by the laptop (batsy/phone.py) and shown full screen
// by the Android app, which passes the pairing token in the URL fragment (#t=...), so it never reaches a log.
'use strict';
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'})[c]);

// --- token, link ------------------------------------------------------------------------------------------
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
    if (r.status === 401) throw new Error('Not paired: tap the gear and pair again');
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
function confirmBox(text) {
  const d = $('#confirm');
  $('#confirm-text').textContent = text;
  return new Promise(res => { d.querySelectorAll('button').forEach(b => b.onclick = () => { d.close(); res(b.value === 'yes'); }); d.showModal(); });
}
if (window.BatsyApp) { $('#gear').hidden = false; $('#gear').addEventListener('click', () => BatsyApp.settings()); }

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
  if ((a === 'restart' || a === 'shutdown') && !await confirmBox(a === 'restart' ? 'Restart the PC now?' : 'Shut down the PC now?')) return;
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

function tintFrom(img) {  // the album's average colour, darkened: the card's background
  try {
    const c = document.createElement('canvas'); c.width = c.height = 12;
    const g = c.getContext('2d'); g.drawImage(img, 0, 0, 12, 12);
    const d = g.getImageData(0, 0, 12, 12).data;
    let r = 0, gg = 0, b = 0;
    for (let i = 0; i < d.length; i += 4) { r += d[i]; gg += d[i + 1]; b += d[i + 2]; }
    const n = d.length / 4, k = 0.55;
    mw.style.setProperty('--tint', `${Math.round(r / n * k)}, ${Math.round(gg / n * k)}, ${Math.round(b / n * k)}`);
  } catch { /* no colour: keep the last one */ }
}
function showArt(url) {
  const next = mwImgs[1 - artSlot];
  const img = new Image();
  img.crossOrigin = 'anonymous';
  img.onload = () => {
    next.src = url;
    next.classList.add('on'); mwImgs[artSlot].classList.remove('on');
    artSlot = 1 - artSlot;
    tintFrom(img);
  };
  img.src = url;
}
function drawWidget() {
  const id = song ? (song.id || song.title) : null;
  if (id !== shownId) {  // a different song (or none): text, art, and the bar jumps without gliding
    shownId = id;
    $('#mw-title').textContent = song ? song.title : 'Nothing playing';
    $('#mw-artist').textContent = song ? song.artist : 'Spotify';
    $('#mw-dev').textContent = song && song.device ? song.device : '';
    mw.classList.toggle('idle', !song);
    if (song && song.art) showArt(song.art);
    else { mwImgs.forEach(i => i.classList.remove('on')); mw.style.setProperty('--tint', '40, 42, 50'); }
    mwBar.style.transition = 'none'; tickSong(); void mwBar.offsetWidth; mwBar.style.transition = '';
  }
  if (Date.now() > holdUntil) mwPP.classList.toggle('playing', !!(song && song.playing));
}

function drawMusic(s) {
  const fresh = s && !s.error ? s : null;
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
  if (!await confirmBox(`Open “${f.name}” on the laptop?`)) return;
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
  } catch (e) {
    if (String(e.message).startsWith('Not paired')) toast(e.message);
  } finally { busy = false; }
}
refresh(); loadLight(); loadFiles();
setInterval(refresh, 3000);
setInterval(() => { if (!document.hidden) { loadLight(); loadFiles(); } }, 20000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) { refresh(); loadLight(); loadFiles(); } });
