// Batsy's phone app: five pages of widgets, swiped sideways. Served by the laptop (batsy/phone.py), shown full screen
// by the Android app, which passes the pairing token in the URL fragment (#t=...): it never reaches the server's logs.
'use strict';
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'})[c]);

// --- the token and the link --------------------------------------------------------------------------------
const store = {
  get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};
(() => {
  const h = new URLSearchParams(location.hash.slice(1));
  if (h.get('t')) store.set('token', h.get('t'));
  if (h.get('via')) store.set('via', h.get('via'));
  if (h.get('focus') === 'ask') addEventListener('load', () => $('#ask-text').focus());  // the widget's Ask button
  if (location.hash) history.replaceState(null, '', location.pathname);
})();
const token = () => store.get('token', '');

async function api(path, body, timeout = 10000) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeout);
  try {
    const r = await fetch(path, {method: body ? 'POST' : 'GET', signal: ctl.signal,
      headers: {'Authorization': 'Bearer ' + token(), ...(body ? {'Content-Type': 'application/json'} : {})},
      body: body ? JSON.stringify(body) : undefined});
    if (r.status === 401) throw new Error('Not paired: open Settings in the app and pair again');
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
  const el = $('#link');
  el.className = 'link ' + (ok ? 'ok' : 'bad');
  el.querySelector('b').textContent = ok ? (store.get('via', '') || 'Connected') : 'Offline';
}

let toastTimer;
function toast(text) {
  if (!text) return;
  const t = $('#toast');
  t.textContent = text; t.classList.add('on');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('on'), 3200);
}

function confirmBox(text) {
  const d = $('#confirm');
  $('#confirm-text').textContent = text;
  return new Promise(res => {
    d.querySelectorAll('button').forEach(b => b.onclick = () => { d.close(); res(b.value === 'yes'); });
    d.showModal();
  });
}

// inside the Android app: a gear for its Settings (pairing); in a browser there's nothing to open
if (window.BatsyApp) { $('#gear').hidden = false; $('#gear').addEventListener('click', () => BatsyApp.settings()); }

// --- pages and tabs -------------------------------------------------------------------------------------
const pages = $('#pages'), tabs = [...document.querySelectorAll('#tabs button')];
let current = 0;
tabs.forEach(b => b.addEventListener('click', () => pages.scrollTo({left: +b.dataset.go * pages.clientWidth, behavior: 'smooth'})));
pages.addEventListener('scroll', () => {
  const i = Math.round(pages.scrollLeft / pages.clientWidth);
  if (i === current) return;
  current = i;
  tabs.forEach((b, j) => b.classList.toggle('on', j === i));
  store.set('page', i);
  if (i === 4) loadFiles();
}, {passive: true});
requestAnimationFrame(() => { const p = store.get('page', 0); pages.scrollLeft = p * pages.clientWidth; });

// --- helpers --------------------------------------------------------------------------------------------
const ago = t => {
  const s = Date.now() / 1000 - t;
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + ' min ago';
  if (s < 86400) return Math.floor(s / 3600) + ' h ago';
  if (s < 172800) return 'yesterday';
  return new Date(t * 1000).toLocaleDateString(undefined, {day: 'numeric', month: 'short'});
};
const dur = s => { s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
  return h ? `${h} h ${m} min` : `${m} min`; };
const until = t => t ? 'resets in ' + dur(t - Date.now() / 1000) : '';
const clock = ms => { const s = Math.floor(ms / 1000); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
const level = (v, warn, bad) => v >= bad ? 'bad' : v >= warn ? 'warn' : '';
const speed = k => k >= 1024 ? (k / 1024).toFixed(1) + ' MB/s' : k + ' KB/s';

// --- home ---------------------------------------------------------------------------------------------------
const chat = store.get('chat', []);
function drawChat() {
  $('#b-chat').innerHTML = chat.slice(-8).map(m => `<div class="msg ${m.who}${m.wait ? ' wait' : ''}">${esc(m.text)}</div>`).join('');
  $('#b-chat').scrollTop = 1e6;
}
async function ask(text) {
  chat.push({who: 'you', text});
  const pending = {who: 'bot', text: 'Thinking…', wait: true};
  chat.push(pending); drawChat();
  try {
    const r = await api('/api/batsy', {action: 'ask', text}, 180000);
    pending.text = r.say || 'Done.';
  } catch (e) { pending.text = e.message; }
  delete pending.wait;
  store.set('chat', chat.slice(-30)); drawChat();
  refresh();
}
$('#ask').addEventListener('submit', e => {
  e.preventDefault();
  const t = $('#ask-text').value.trim();
  if (t) { $('#ask-text').value = ''; ask(t); }
});
$('#m-ask').addEventListener('submit', e => {
  e.preventDefault();
  const t = $('#m-ask-text').value.trim();
  if (!t) return;
  $('#m-ask-text').value = '';
  toast('Asking Batsy…');
  api('/api/batsy', {action: 'ask', text: /^play\b/i.test(t) ? t : 'play ' + t}, 60000)
    .then(r => { toast(r.say); setTimeout(refresh, 1500); }).catch(e => toast(e.message));
});
drawChat();

let lightOn = null, pcOn = false;
async function loadLight() {
  try { const r = await api('/api/home'); lightOn = r.on; } catch { lightOn = null; }
  $('#light-state').textContent = lightOn === null ? 'Not answering' : lightOn ? 'On · tap for off' : 'Off · tap for on';
  $('#light-tile').classList.toggle('on', !!lightOn);
}
document.querySelectorAll('[data-act]').forEach(b => b.addEventListener('click', async () => {
  const act = b.dataset.act;
  try {
    let r;
    if (act === 'stop') r = await api('/api/batsy', {action: 'stop'});
    else if (act === 'light') r = await api('/api/home', {action: lightOn ? 'off' : 'on'});
    else if (act === 'night') r = await api('/api/home', {action: 'night'});
    else if (act === 'pc') {  // on: its controls are on the Computers page; off: wake it
      if (pcOn) return pages.scrollTo({left: 2 * pages.clientWidth, behavior: 'smooth'});
      r = await api('/api/pc', {action: 'wake'});
    }
    toast(r.say);
    if (act === 'light' || act === 'night') { lightOn = r.on ?? lightOn; loadLight(); }
  } catch (e) { toast(e.message); }
}));

const STATES = {idle: ['Idle', ''], listening: ['Listening', 'listening'], thinking: ['Thinking', 'thinking'],
  speaking: ['Speaking', 'speaking'], working: ['Working', 'thinking']};
function drawBatsy(b) {
  const [label, cls] = STATES[b.state] || [b.state || 'Idle', ''];
  $('#b-state').textContent = label;
  document.querySelector('.hero .orb').className = 'orb ' + cls;
  $('#b-status').textContent = b.status || (b.said ? '“' + b.said.slice(0, 90) + '”' : 'Waiting for “Batsy”');
  const asks = b.asks || [];
  $('#asks-card').hidden = !asks.length;
  $('#asks').innerHTML = asks.map(a => `<li>${esc(a)}</li>`).join('');
}

// --- claude ---------------------------------------------------------------------------------------------
function ring(id, pct) {
  const c = $(id);
  c.style.strokeDashoffset = 264 * (1 - Math.min(100, pct || 0) / 100);
  c.setAttribute('class', 'fill ' + level(pct, 70, 90));
}
function drawClaude(c) {
  if (!c) return;
  for (const k of ['session', 'week']) {
    const v = c[k] || {};
    ring('#r-' + k, v.pct);
    $('#c-' + k).textContent = Math.round(v.pct || 0) + '%';
    $(`#c-${k}-reset`).textContent = until(v.resets_at);
  }
  $('#c-req').textContent = c.requests_week ?? '–';
  const ss = c.sessions || [];
  $('#c-count').textContent = ss.length || '';
  $('#c-sessions').innerHTML = ss.length ? ss.map(s => `<li><span class="badge">${esc(s.project.slice(0, 2).toUpperCase())}</span>
    <div class="txt"><b>${esc(s.project)}</b><small>${s.interactive ? 'In a terminal' : 'Background'} · running ${dur(Date.now() / 1000 - s.since)}</small></div>
    <span class="live"></span></li>`).join('') : '<li class="empty">No Claude Code sessions running on the laptop.</li>';
  const run = c.running || [];
  $('#c-agents').innerHTML = run.length ? run.map(a => `<li><span class="live"></span><div class="txt"><b>${esc(a)}</b></div></li>`).join('')
    : '<li class="empty">Nothing running in the background.</li>';
}

// --- computers --------------------------------------------------------------------------------------------
const history_ = {};
function spark(vals) {
  if (vals.length < 2) return '';
  const w = 100, h = 28, max = 100, step = w / (vals.length - 1);
  const pts = vals.map((v, i) => `${(i * step).toFixed(1)},${(h - (v / max) * (h - 2) - 1).toFixed(1)}`);
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><path class="area" d="M0,${h} L${pts.join(' L')} L${w},${h}Z"/><path d="M${pts.join(' L')}"/></svg>`;
}
function stat(label, value, unit, pct, cls, extra = '') {
  return `<div class="stat"><small><span>${label}</span>${extra}</small><b>${value}<em>${unit}</em></b>
    ${pct == null ? '' : `<div class="bar"><i class="${cls}" style="width:${Math.min(100, pct)}%"></i></div>`}</div>`;
}
function drawMachines(ms) {
  $('#machines').innerHTML = ms.map(m => {
    const hist = history_[m.name] = history_[m.name] || {cpu: [], temp: []};
    if (m.online) { hist.cpu.push(m.cpu || 0); if (m.temp) hist.temp.push(m.temp); }
    hist.cpu = hist.cpu.slice(-40); hist.temp = hist.temp.slice(-40);
    const state = m.online === true ? 'on' : m.online === false ? 'off' : '';
    if (!m.online) return `<div class="card"><div class="mhead"><span class="dot ${state}"></span><b>${esc(m.name)}</b>
      <small>${m.online === false ? 'Off or asleep' : 'Checking…'}</small></div>${m.kind === 'pc' ? pcButtons(false) : ''}</div>`;
    const batt = m.battery != null ? `<span>🔋 ${m.battery}%${m.charging ? ' charging' : ''}</span>` : '';
    return `<div class="card"><div class="mhead"><span class="dot on"></span><b>${esc(m.name)}</b><small>up ${dur(m.uptime_s || 0)}</small></div>
      <div class="stats">
        <div class="stat"><small><span>CPU</span><span>${m.cores || ''} threads</span></small><b>${Math.round(m.cpu || 0)}<em>%</em></b>${spark(hist.cpu)}</div>
        <div class="stat"><small><span>Temperature</span></small><b>${m.temp ?? '–'}<em>°C</em></b>${spark(hist.temp)}</div>
        ${stat('Memory', m.ram_used_gb ?? Math.round(m.ram), m.ram_used_gb != null ? ` / ${m.ram_total_gb} GB` : '%', m.ram, level(m.ram, 75, 90))}
        ${stat('Disk', m.disk_free_gb, ' GB free', m.disk, level(m.disk, 80, 92))}
      </div>
      <div class="mfoot"><span>↓ ${speed(m.net_down_kbps || 0)}</span><span>↑ ${speed(m.net_up_kbps || 0)}</span>${batt}</div>
      ${m.kind === 'pc' ? pcButtons(true) : ''}</div>`;
  }).join('');
  pcOn = !!(ms.find(m => m.kind === 'pc') || {}).online;
  $('#pc-mini').textContent = pcOn ? 'On · controls' : 'Off · tap to wake';
}
function pcButtons(on) {
  return `<div class="pcbtns">${(on ? [['lock', 'Lock'], ['sleep', 'Sleep'], ['restart', 'Restart'], ['shutdown', 'Shut down']]
    : [['wake', 'Wake up']]).map(([a, l]) => `<button class="pcbtn${a === 'shutdown' || a === 'restart' ? ' danger' : ''}" data-pc="${a}">${l}</button>`).join('')}</div>`;
}
$('#machines').addEventListener('click', async e => {
  const b = e.target.closest('[data-pc]');
  if (!b) return;
  const a = b.dataset.pc;
  if ((a === 'restart' || a === 'shutdown') && !await confirmBox(a === 'restart' ? 'Restart the PC now?' : 'Shut down the PC now?')) return;
  try { toast((await api('/api/pc', {action: a}, 15000)).say); setTimeout(refresh, 2000); } catch (err) { toast(err.message); }
});

// --- music ---------------------------------------------------------------------------------------------
let song = null, songAt = 0;
const ICON_PLAY = '<svg class="i f" viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5l11 7-11 7z"/></svg>', ICON_PAUSE = '<svg class="i f" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 5h3.5v14H7zM13.5 5H17v14h-3.5z"/></svg>';
function drawMusic(s) {
  song = s && !s.error ? s : null; songAt = Date.now();
  const playing = song && song.playing;
  for (const [img, noart] of [['#m-art', '#m-noart'], ['#mm-art', null]]) {
    const el = $(img);
    if (song && song.art) { if (el.src !== song.art) el.src = song.art; el.hidden = false; if (noart) $(noart).hidden = true; }
    else { el.removeAttribute('src'); if (noart) { el.hidden = true; $(noart).hidden = false; } }
  }
  $('#m-title').textContent = $('#mm-title').textContent = song ? song.title : (s && s.error ? 'Spotify error' : 'Nothing playing');
  $('#m-artist').textContent = $('#mm-artist').textContent = song ? song.artist : (s && s.error) || '';
  $('#m-dev').textContent = song && song.device ? 'on ' + song.device : '';
  $('#m-icon').innerHTML = $('#mm-icon').innerHTML = playing ? ICON_PAUSE : ICON_PLAY;
  tick();
}
function tick() {
  if (!song) { $('#m-bar').style.width = '0'; $('#m-pos').textContent = $('#m-dur').textContent = '0:00'; return; }
  const pos = Math.min(song.duration_ms, song.progress_ms + (song.playing ? Date.now() - songAt : 0));
  $('#m-bar').style.width = (100 * pos / (song.duration_ms || 1)) + '%';
  $('#m-pos').textContent = clock(pos); $('#m-dur').textContent = clock(song.duration_ms);
}
setInterval(tick, 1000);
document.querySelectorAll('[data-sp]').forEach(b => b.addEventListener('click', async () => {
  try {
    const r = await api('/api/spotify', {action: b.dataset.sp});
    if (r.say) toast(r.say);
    if (song && b.dataset.sp === 'toggle') { song.playing = !song.playing; drawMusic(song); }
    setTimeout(refresh, 900);
  } catch (e) { toast(e.message); }
}));

// --- files ----------------------------------------------------------------------------------------------
const HEROES = {Jonathan: 'Batman', Karan: 'Nightwing', Rehaan: 'Superman'};
let files = [], who = 'All';
async function loadFiles() {
  try { files = (await api('/api/files')).files || []; } catch (e) { toast(e.message); }
  drawFiles();
}
function drawFiles() {
  const people = ['All', ...new Set(files.map(f => f.from))];
  $('#f-filter').innerHTML = people.map(p => `<button class="chip${p === who ? ' on' : ''}" data-who="${esc(p)}">${esc(p === 'All' ? 'All' : (HEROES[p] || p))}</button>`).join('');
  const shown = files.filter(f => who === 'All' || f.from === who);
  $('#f-count').textContent = shown.length || '';
  $('#files').innerHTML = shown.length ? shown.map((f, i) => `<li data-i="${files.indexOf(f)}">
      <span class="badge ${esc(f.from)}">${esc((HEROES[f.from] || f.from || '?')[0])}</span>
      <div class="txt"><b>${esc(f.name)}</b><small>${esc(HEROES[f.from] || f.from)} · ${ago(f.t)} · ${esc(f.kind)}${f.missing ? ' · moved' : ''}</small></div>
      ${f.on_pc ? '<span class="pcmark">PC</span>' : ''}</li>`).join('') : '<li class="empty">Nothing received yet.</li>';
}
$('#f-filter').addEventListener('click', e => { const c = e.target.closest('[data-who]'); if (c) { who = c.dataset.who; drawFiles(); } });
$('#files').addEventListener('click', async e => {
  const li = e.target.closest('[data-i]');
  if (!li) return;
  const f = files[+li.dataset.i];
  if (!await confirmBox(`Open “${f.name}” on the laptop?`)) return;
  try { toast((await api('/api/files', {action: 'open', path: f.path})).say); } catch (err) { toast(err.message); }
});

// --- refresh: every 3 s while the app is open, nothing while it's in the background ---------------------
let busy = false;
async function refresh() {
  if (busy || document.hidden) return;
  busy = true;
  try {
    const s = await api('/api/status');
    drawBatsy(s.batsy || {});
    drawClaude(s.claude);
    drawMachines(s.machines || []);
    drawMusic(s.spotify);
  } catch (e) {
    if (String(e.message).startsWith('Not paired')) toast(e.message);
  } finally { busy = false; }
}
refresh(); loadLight();
setInterval(refresh, 3000);
setInterval(() => { if (!document.hidden && current === 0) loadLight(); }, 15000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) { refresh(); loadLight(); if (current === 4) loadFiles(); } });
