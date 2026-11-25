// Batsy on the web (batsy/web.py). Nobody signs in: Tailscale tells Batsy who you are. The phone app's token (same
// origin, saved by /app) also counts, so the Batsy app's browser is "you" even over the USB cable.
'use strict';
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'})[c]);
const token = (() => { try { return JSON.parse(localStorage.getItem('token')) || ''; } catch { return ''; } })();
let me = null;

async function api(path, body, timeout = 15000) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeout);
  try {
    const r = await fetch(path, {method: body ? 'POST' : 'GET', signal: ctl.signal,
      headers: {...(token ? {Authorization: 'Bearer ' + token} : {}), ...(body ? {'Content-Type': 'application/json'} : {})},
      body: body ? JSON.stringify(body) : undefined});
    const j = await r.json().catch(() => ({}));
    if (r.status === 403) throw Object.assign(new Error(j.say || j.error || 'Not allowed'), {denied: true});
    if (!r.ok) throw new Error(j.error || 'HTTP ' + r.status);
    $('#link').className = 'dot ok';
    return j;
  } catch (e) {
    if (!e.denied) $('#link').className = 'dot bad';
    throw e.name === 'AbortError' ? new Error('Batsy took too long') : e;
  } finally { clearTimeout(timer); }
}
let toastT;
function toast(t) { if (!t) return; $('#toast').textContent = t; $('#toast').classList.add('on');
  clearTimeout(toastT); toastT = setTimeout(() => $('#toast').classList.remove('on'), 3500); }
const ago = t => { const s = Date.now() / 1000 - t; return s < 60 ? 'just now' : s < 3600 ? `${Math.round(s / 60)} min ago`
  : s < 86400 ? `${Math.round(s / 3600)} h ago` : new Date(t * 1000).toLocaleDateString(undefined, {day: 'numeric', month: 'short'}); };
const dur = s => s >= 3600 ? `${Math.floor(s / 3600)} h ${Math.round(s % 3600 / 60)} min` : `${Math.round(s / 60)} min`;
const bytes = n => n == null ? '' : n > 1e6 ? (n / 1e6).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1e3)) + ' KB';

// --- tabs --------------------------------------------------------------------------------------------------
const loaders = {};
function show() {
  const [tab, arg] = (location.hash.slice(1) || 'live').split('/');
  const name = document.getElementById(tab) && (tab !== 'remote' || me?.owner) ? tab : 'live';
  document.querySelectorAll('.tab').forEach(s => s.hidden = s.id !== name);
  document.querySelectorAll('#tabs a').forEach(a => a.classList.toggle('on', a.dataset.tab === name));
  loaders[name]?.(arg);
}
addEventListener('hashchange', show);

// --- live ----------------------------------------------------------------------------------------------------
const STATE = {idle: 'Idle', listening: 'Listening', thinking: 'Thinking', speaking: 'Speaking'};
loaders.live = async () => {
  let s;
  try { s = await api('/api/web/live'); } catch (e) { $('#b-state').textContent = e.message; return; }
  const b = s.batsy || {};
  $('#b-dot').className = 'pulse ' + b.state;
  $('#b-state').textContent = STATE[b.state] || b.state;
  $('#b-status').textContent = b.status || '';
  $('#b-said').textContent = b.said || (me?.owner ? '' : 'Batsy is ' + (b.busy?.length ? 'busy.' : 'free.'));
  $('#b-you').textContent = b.you ? `You: “${b.you}”` : '';
  $('#b-busy').innerHTML = (b.busy || []).map(x => `<li>${esc(x)}</li>`).join('');
  $('#confirms').innerHTML = (s.confirm || []).map(c => `<div class="confirm"><p><b>Batsy asks:</b> ${esc(c.question)}</p>
    <button data-yes="${c.id}" class="primary">Yes</button><button data-no="${c.id}" class="danger">No</button></div>`).join('');
  printer(s.printer);
  if (me?.owner) { music(s.spotify); claude(s.claude); }
  $('#machines').innerHTML = (s.machines || []).map(machine).join('');
};
function printer(p) {
  const el = $('#p-body');
  if (!p) { el.innerHTML = '<span class="dim">No printer connected.</span>'; return; }
  if (!p.connected) { el.innerHTML = '<span class="dim">Printer off or unplugged.</span>'; return; }
  const temps = `<dl class="kv"><dt>Nozzle</dt><dd>${p.hotend != null ? Math.round(p.hotend) : '–'}°${p.hotend_target ? ' / ' + p.hotend_target + '°' : ''}</dd>
    <dt>Bed</dt><dd>${p.bed != null ? Math.round(p.bed) : '–'}°${p.bed_target ? ' / ' + p.bed_target + '°' : ''}</dd></dl>`;
  if (p.printing || p.paused) {
    const pct = Math.round(p.progress || 0);
    el.innerHTML = `<div class="big">${pct}%</div><div class="bar"><b style="width:${pct}%"></b></div>
      <div class="dim">${p.paused ? 'Paused' : p.remaining_s ? dur(p.remaining_s) + ' left' : 'Printing'}${p.job ? ' · ' + esc(p.job) : ''}</div>${temps}`;
  } else {
    el.innerHTML = `<div>${p.done ? '✓ Finished' + (p.job ? ': ' + esc(p.job) : '') : p.error ? '⚠ ' + esc(p.error) : 'Idle'}</div>${temps}`;
  }
}
function music(sp) {
  $('#c-music').hidden = false;
  $('#m-body').innerHTML = !sp || !sp.title ? '<span class="dim">Nothing playing</span>' :
    `<div class="music">${sp.art ? `<img src="${esc(sp.art)}" alt="">` : ''}<div><b>${esc(sp.title)}</b>
     <div class="dim">${esc(sp.artist)}</div><div class="dim small">${sp.playing ? 'Playing' : 'Paused'} on ${esc(sp.device)}</div></div></div>`;
}
function claude(c) {
  $('#c-claude').hidden = false;
  for (const [k, id] of [['session', 's'], ['week', 'w']]) {
    const pct = c?.[k]?.pct; const b = $('#cl-' + id);
    b.style.width = Math.min(100, pct || 0) + '%';
    b.className = pct >= 100 ? 'full' : pct >= 80 ? 'warn' : '';
    $(`#cl-${id}p`).textContent = pct == null ? '–' : Math.round(pct) + '%';
  }
  $('#cl-note').textContent = c?.running?.length ? 'Working: ' + c.running.join(', ') : '';
}
function machine(m) {
  if (!m.online) return `<article class="card"><h2>${esc(m.name)}</h2><p class="dim">${m.online === false ? 'Off or asleep' : 'Checking…'}</p></article>`;
  const meter = (label, v) => `<div class="meter"><span>${label}</span><i><b style="width:${Math.min(100, v || 0)}%" class="${v >= 90 ? 'full' : v >= 75 ? 'warn' : ''}"></b></i><em>${v == null ? '–' : Math.round(v) + '%'}</em></div>`;
  const extra = [m.temp ? m.temp + '°C' : '', m.battery != null ? m.battery + '%' + (m.charging ? ' ⚡' : ' battery') : '',
    m.gpu ? 'GPU ' + Math.round(m.gpu.util) + '%' : '', m.disk_free_gb != null ? m.disk_free_gb + ' GB free' : ''].filter(Boolean).join(' · ');
  return `<article class="card"><h2>${esc(m.name)}</h2>${meter('CPU', m.cpu)}${meter('RAM', m.ram)}<p class="dim small">${esc(extra)}</p></article>`;
}
document.addEventListener('click', async e => {
  const yes = e.target.closest('[data-yes]'), no = e.target.closest('[data-no]');
  if (!yes && !no) return;
  const r = await api('/api/web/remote', {action: 'confirm', id: +(yes || no).dataset[yes ? 'yes' : 'no'], yes: !!yes}).catch(x => ({say: x.message}));
  toast(r.say); loaders.live();
});

// --- chat ----------------------------------------------------------------------------------------------------
function bubble(role, text, cls = '') {
  const d = document.createElement('div');
  d.className = `msg ${role} ${cls}`; d.textContent = text;
  $('#msgs').appendChild(d); $('#msgs').scrollTop = 1e9; return d;
}
loaders.chat = async () => {
  const r = await api('/api/web/chat').catch(() => ({messages: []}));
  $('#msgs').innerHTML = '';
  if (!r.messages.length) bubble('batsy', me?.owner ? 'Hi. Anything you can say to me out loud works here too.'
    : `Hi ${me?.name || ''}! Ask me anything. I can search the web, start a research (“research how bats navigate”) or remind you (“remind me at 6 pm to …”).`);
  r.messages.forEach(m => bubble(m.role, m.text));
  $('#ask-text').focus();
};
$('#ask').addEventListener('submit', async e => {
  e.preventDefault();
  const text = $('#ask-text').value.trim();
  if (!text) return;
  $('#ask-text').value = ''; grow();
  bubble('you', text);
  const wait = bubble('batsy', '…', 'wait');
  try { wait.textContent = (await api('/api/web/chat', {text}, 300000)).say || 'Done.'; }
  catch (x) { wait.textContent = x.message; }
  wait.classList.remove('wait');
});
const grow = () => { const t = $('#ask-text'); t.style.height = 'auto'; t.style.height = Math.min(160, t.scrollHeight) + 'px'; };
$('#ask-text').addEventListener('input', grow);
$('#ask-text').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('#ask').requestSubmit(); } });

// --- research ------------------------------------------------------------------------------------------------
let openId = null, researchTimer = null;
loaders.research = async arg => {
  if (arg) openId = +arg;
  const r = await api('/api/web/research').catch(() => ({research: []}));
  $('#r-list').innerHTML = r.research.map(x => `<li data-id="${x.id}" class="${x.id === openId ? 'on' : ''}"><div class="grow">
    <div class="title">${esc(x.title)}</div><div class="dim small">${ago(x.t)}${x.sources ? ' · ' + x.sources + ' sources' : ''}</div></div>
    <span class="tag ${x.status}">${x.status === 'running' ? 'researching' : x.status}</span></li>`).join('')
    || '<li class="dim">Nothing yet.</li>';
  if (openId) openResearch(openId);
  clearTimeout(researchTimer);
  if (r.research.some(x => x.status === 'running')) researchTimer = setTimeout(() => !$('#research').hidden && loaders.research(), 8000);
};
$('#r-list').addEventListener('click', e => { const li = e.target.closest('li[data-id]'); if (li) location.hash = 'research/' + li.dataset.id; });
$('#r-new').addEventListener('submit', async e => {
  e.preventDefault();
  const topic = $('#r-topic').value.trim(); if (!topic) return;
  try { const r = await api('/api/web/research', {topic}); $('#r-topic').value = ''; location.hash = 'research/' + r.id; }
  catch (x) { toast(x.message); }
});
async function openResearch(id) {
  const r = await api('/api/web/research?id=' + id).catch(() => null);
  const el = $('#r-detail');
  if (!r || r.error) { el.innerHTML = '<p class="empty dim">Not found.</p>'; return; }
  const del = `<button class="danger" id="r-del">Delete</button>`;
  if (r.status !== 'done') {
    el.innerHTML = `<div class="r-head"><h1>${esc(r.topic)}</h1>${del}</div>
      ${r.status === 'running' ? `<div class="progress-line"><b></b></div><p class="dim">Searching and reading sources since ${ago(r.t)}. This page updates by itself.</p>`
        : `<p class="dim">${esc(r.note)}</p>`}`;
  } else {
    const d = r.data, src = d.sources || [];
    const cite = ids => (ids || []).filter(i => src[i]).map(i => `<sup><a href="${esc(src[i].url)}" target="_blank" rel="noopener">[${i + 1}]</a></sup>`).join('');
    el.innerHTML = `<div class="r-head"><h1>${esc(d.title || r.topic)}</h1>${del}</div>
      <p class="dim small">Researched ${ago(r.done || r.t)} · ${src.length} sources · drag the map around</p>
      <div class="map" id="map"></div>
      <div class="card summary"><h2>Summary</h2>${String(d.summary || '').split(/\n+/).map(p => `<p>${esc(p)}</p>`).join('')}</div>
      <div class="r-cols" style="margin-top:14px">
        <div class="card"><h2>Findings</h2><ol class="findings">${(d.findings || []).map(f => `<li>${esc(f.text)}${cite(f.sources)}</li>`).join('')}</ol></div>
        <div class="card"><h2>Open questions</h2><ul class="findings">${(d.open_questions || []).map(q => `<li>${esc(q)}</li>`).join('')}</ul>
          <h2 style="margin-top:16px">Next steps</h2><ul class="findings">${(d.next_steps || []).map(q => `<li>${esc(q)}</li>`).join('')}</ul></div>
      </div>
      <div class="card" style="margin-top:14px"><h2>Sources</h2><ol class="sources">${src.map((s, i) =>
        `<li>[${i + 1}] <a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title || s.url)}</a> <span class="tag">${esc(s.kind || '')}</span>
         <span class="note">${esc(s.note || '')}</span></li>`).join('')}</ol></div>`;
    drawMap($('#map'), d.map || {nodes: [], edges: []});
  }
  $('#r-del').onclick = async () => {
    if (!confirm('Delete this research?')) return;
    await api('/api/web/research', {action: 'delete', id}); openId = null; location.hash = 'research';
    $('#r-detail').innerHTML = '<p class="empty dim">Deleted.</p>';
  };
}

// The concept map: a small force layout in SVG. Core in the middle, ideas around it, drag to rearrange.
const KIND = {core: '#ffc20e', concept: '#6aa8ff', method: '#5bd38a', tool: '#c792ea', question: '#ff9a1f', source: '#8d95a5'};
function drawMap(host, map) {
  const W = host.clientWidth, H = host.clientHeight;
  const nodes = (map.nodes || []).slice(0, 60).map((n, i) => ({...n, x: W / 2 + Math.cos(i * 2.4) * (40 + i * 8),
    y: H / 2 + Math.sin(i * 2.4) * (40 + i * 6), vx: 0, vy: 0, r: n.kind === 'core' ? 16 : 6 + 3 * (n.weight || 1)}));
  const byId = Object.fromEntries(nodes.map(n => [n.id, n]));
  const edges = (map.edges || []).filter(e => byId[e.a] && byId[e.b]);
  const core = nodes.find(n => n.kind === 'core'); if (core) { core.x = W / 2; core.y = H / 2; }
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const gE = document.createElementNS(NS, 'g'), gN = document.createElementNS(NS, 'g');
  svg.append(gE, gN);
  const eEls = edges.map(e => {
    const l = document.createElementNS(NS, 'line'); l.setAttribute('class', 'edge'); gE.appendChild(l);
    const t = document.createElementNS(NS, 'text'); t.setAttribute('class', 'edge-label'); t.textContent = e.label || ''; gE.appendChild(t);
    return [l, t];
  });
  const nEls = nodes.map(n => {
    const g = document.createElementNS(NS, 'g'); g.setAttribute('class', 'node ' + (n.kind || 'concept'));
    const c = document.createElementNS(NS, 'circle'); c.setAttribute('r', n.r); c.setAttribute('fill', KIND[n.kind] || KIND.concept);
    const t = document.createElementNS(NS, 'text'); t.setAttribute('x', n.r + 5); t.setAttribute('y', 4); t.textContent = n.label;
    g.append(c, t); gN.appendChild(g);
    let drag = false;
    c.addEventListener('pointerdown', ev => { drag = true; n.fixed = true; c.setPointerCapture(ev.pointerId); kick(); });
    c.addEventListener('pointermove', ev => { if (!drag) return; const b = svg.getBoundingClientRect();
      n.x = (ev.clientX - b.left) * W / b.width; n.y = (ev.clientY - b.top) * H / b.height; kick(); });
    c.addEventListener('pointerup', () => { drag = false; });
    return g;
  });
  host.innerHTML = ''; host.appendChild(svg);
  const legend = document.createElement('div'); legend.className = 'legend';
  legend.innerHTML = Object.entries(KIND).filter(([k]) => nodes.some(n => (n.kind || 'concept') === k))
    .map(([k, c]) => `<span><i style="background:${c}"></i>${k}</span>`).join('');
  host.appendChild(legend);

  let heat = 1, raf = 0;
  function kick() { heat = Math.max(heat, .6); if (!raf) raf = requestAnimationFrame(step); }
  function step() {
    raf = 0;
    for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {  // keep apart
      const a = nodes[i], b = nodes[j]; let dx = b.x - a.x, dy = b.y - a.y, d2 = dx * dx + dy * dy + .01;
      const f = 1400 / d2, d = Math.sqrt(d2); dx /= d; dy /= d;
      a.vx -= dx * f; a.vy -= dy * f; b.vx += dx * f; b.vy += dy * f;
    }
    for (const e of edges) {  // linked ideas pull together
      const a = byId[e.a], b = byId[e.b]; const dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1;
      const f = (d - 90) * .02; a.vx += dx / d * f; a.vy += dy / d * f; b.vx -= dx / d * f; b.vy -= dy / d * f;
    }
    for (const n of nodes) {
      n.vx += (W / 2 - n.x) * .002; n.vy += (H / 2 - n.y) * .002;  // gently toward the middle
      if (n.fixed || n.kind === 'core' && !n.moved) { n.vx = n.vy = 0; if (n.kind === 'core' && !n.fixed) { n.x = W / 2; n.y = H / 2; } }
      n.x = Math.max(n.r + 4, Math.min(W - n.r - 80, n.x + n.vx * heat)); n.y = Math.max(n.r + 4, Math.min(H - n.r - 4, n.y + n.vy * heat));
      n.vx *= .6; n.vy *= .6;
    }
    edges.forEach((e, i) => { const a = byId[e.a], b = byId[e.b]; const [l, t] = eEls[i];
      l.setAttribute('x1', a.x); l.setAttribute('y1', a.y); l.setAttribute('x2', b.x); l.setAttribute('y2', b.y);
      t.setAttribute('x', (a.x + b.x) / 2); t.setAttribute('y', (a.y + b.y) / 2); });
    nodes.forEach((n, i) => nEls[i].setAttribute('transform', `translate(${n.x},${n.y})`));
    heat *= .985;
    if (heat > .02) raf = requestAnimationFrame(step);
  }
  kick();
}

// --- reminders ----------------------------------------------------------------------------------------------------
loaders.reminders = async () => {
  const r = await api('/api/web/reminders').catch(() => ({reminders: []}));
  const now = Date.now() / 1000;
  $('#rem-list').innerHTML = r.reminders.map(x => `<li><div class="grow"><div class="title">${esc(x.text)}</div>
    <div class="dim small">${x.fired || x.due < now ? 'done · ' : ''}${esc(x.when)}</div></div>
    <button class="x" data-del="${x.id}" aria-label="Delete">×</button></li>`).join('') || '<li class="dim">No reminders.</li>';
  $('#push-note').innerHTML = me?.owner ? 'Your reminders also go through Batsy as usual.'
    : 'Reminders pop up as notifications in this browser. <button id="push-on">Turn on notifications</button>';
  const b = $('#push-on'); if (b) b.onclick = enablePush;
};
$('#rem-new').addEventListener('submit', async e => {
  e.preventDefault();
  const text = $('#rem-text').value.trim(); if (!text) return;
  const r = await api('/api/web/reminders', {text}).catch(x => ({error: x.message}));
  if (r.error) return toast(r.error);
  $('#rem-text').value = ''; toast('Okay, ' + r.when); loaders.reminders();
});
$('#rem-list').addEventListener('click', async e => {
  const b = e.target.closest('[data-del]'); if (!b) return;
  await api('/api/web/reminders', {action: 'delete', id: +b.dataset.del}); loaders.reminders();
});
async function enablePush() {
  try {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) throw new Error('This browser can’t do notifications here (it needs HTTPS or the Batsy app).');
    const reg = await navigator.serviceWorker.register('/web/sw.js', {scope: '/web/'});
    if (await Notification.requestPermission() !== 'granted') throw new Error('Notifications were blocked.');
    const key = me.push_key; const raw = atob(key.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - key.length % 4) % 4));
    const sub = await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: Uint8Array.from(raw, c => c.charCodeAt(0))});
    await api('/api/web/push', {sub: sub.toJSON()}); toast('Notifications on.');
  } catch (x) { toast(x.message); }
}

// --- files ----------------------------------------------------------------------------------------------------
loaders.files = async () => {
  const r = await api('/api/web/files').catch(() => ({files: []}));
  $('#files-note').textContent = me?.owner ? 'Everything people have sent you.' : 'Files you’ve sent to Batsy.';
  $('#file-list').innerHTML = r.files.map(f => `<li><div class="grow"><div class="title">${esc(f.name)}</div>
    <div class="dim small">${ago(f.t)} · ${bytes(f.size)}${me?.owner && f.sender ? ' · from ' + esc(f.sender) : ''}${f.missing ? ' · moved' : ''}</div></div>
    ${f.missing ? '' : `<a class="btn" href="/api/web/files/raw?id=${f.id}" download>Download</a>`}</li>`).join('')
    || '<li class="dim">No files yet.</li>';
};

// --- remote (you only) --------------------------------------------------------------------------------------------------
loaders.remote = async () => {
  const s = await api('/api/web/remote').catch(x => ({error: x.message}));
  if (s.error) return toast(s.error);
  $('#rm-up').textContent = 'Running for ' + dur(s.uptime_s) + (s.pending?.length ? ` · ${s.pending.length} question(s) waiting on Live` : '');
  const rc = s.claude_rc || {};
  $('#rc-state').textContent = !s.claude ? 'Claude Code isn’t installed on the laptop.' : !rc.installed ? 'Not set up yet: press Start.'
    : `Service: ${rc.state}`;
  $('#rc-link').hidden = !rc.link; if (rc.link) $('#rc-link').href = rc.link;
  const checks = [['Tailscale SSH (a terminal from anywhere)', s.tailscale_ssh], ['Collect files sent over Tailscale', s.operator],
    ['Keeps running with nobody logged in', s.linger], ['Comes back by itself after a reboot', s.autologin],
    ['Claude Remote Control', rc.state === 'active']];
  $('#setup').innerHTML = checks.map(([t, ok]) => `<li class="${ok ? 'ok' : ''}">${t}</li>`).join('');
  if (!$('#out').textContent) runAct('log');
};
async function runAct(act, btn) {
  if (['restart', 'update'].includes(act) && !confirm(act === 'update' ? 'Pull the latest code and restart Batsy?' : 'Restart Batsy?')) return;
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/web/remote', {action: act}, 180000);
    if (r.text != null) { $('#out-title').textContent = act === 'doctor' ? 'Doctor' : 'Log'; $('#out').textContent = r.text;
      if (act === 'log') $('#out').scrollTop = 1e9; }
    toast(r.say);
    if (act.startsWith('rc_')) setTimeout(loaders.remote, 2500);
  } catch (x) { toast(x.message); } finally { if (btn) btn.disabled = false; }
}
document.querySelectorAll('#remote [data-act]').forEach(b => b.addEventListener('click', () => runAct(b.dataset.act, b)));

// --- start ------------------------------------------------------------------------------------------------------------
(async () => {
  try { me = await api('/api/web/me'); }
  catch (e) {
    document.querySelector('main').innerHTML = `<div class="card narrow" style="margin:60px auto"><h2>Batsy</h2><p>${esc(e.message)}</p></div>`;
    $('#me').textContent = ''; return;
  }
  $('#me').textContent = me.name;
  document.querySelectorAll('.owner-only').forEach(x => x.hidden = !me.owner);
  if (!me.owner) $('#chat-hint').textContent = 'Batsy searches the web for you. It can’t see anything on Jonathan’s computers.';
  show();
  setInterval(() => { if (!$('#live').hidden && !document.hidden) loaders.live(); }, 4000);
})();
