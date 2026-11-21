// Batsy Drive: your PC drive and what you've sent Batsy. Who you are comes from your Tailscale address (no login).
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const store = {get: (k, d) => { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
  set: (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} }};

const PICS = /\.(jpe?g|png|gif|webp|bmp|heic|tiff?)$/i, VIDEO = /\.(mp4|webm|mov|m4v|mkv)$/i, AUDIO = /\.(mp3|m4a|wav|ogg|flac|aac)$/i;
const TEXT = /\.(txt|md|csv|json|py|js|ts|ino|c|cpp|h|html|css|log|yaml|yml|toml|ini|sh|gcode)$/i, PDF = /\.pdf$/i;
const S = {me: null, space: 'pc', path: '/', mode: 'folder', q: '', items: [], sel: -1, view: store.get('drive.view', 'list'),
  sort: store.get('drive.sort', {k: 'name', dir: 1}), viewing: -1};

const api = async (path, opts) => {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({error: `${r.status}`}));
  if (!r.ok || j.error) throw new Error(j.error || r.status);
  return j;
};
const q = o => new URLSearchParams(o).toString();
const fileUrl = (it, dl) => `/api/file?${q({space: S.space, path: it.path, ...(dl ? {dl: 1} : {})})}`;
const size = n => n == null ? '' : n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : n < 1073741824 ?
  `${(n / 1048576).toFixed(1)} MB` : `${(n / 1073741824).toFixed(2)} GB`;
const when = t => {
  const d = new Date(t * 1000), now = new Date(), s = (now - d) / 1000;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  return d.toLocaleDateString([], {day: '2-digit', month: 'short', year: d.getFullYear() === now.getFullYear() ? undefined : '2-digit'});
};
const ext = n => (n.match(/\.([a-z0-9]{1,5})$/i) || ['', ''])[1].toUpperCase();
function flash(t, ms = 2200) { const s = $('#status'); s.textContent = t; s.classList.add('on'); clearTimeout(flash.t); flash.t = setTimeout(() => s.classList.remove('on'), ms); }

// --- where you are (in the address, so Back works) ------------------------------------------------------------------
function go(space, path = '/', mode = 'folder') {
  location.hash = mode === 'recent' ? `${space}:recent` : `${space}:${path}`;
}
function fromHash() {
  const h = decodeURIComponent(location.hash.slice(1));
  const [space, rest] = h.includes(':') ? [h.slice(0, h.indexOf(':')), h.slice(h.indexOf(':') + 1)] : ['pc', '/'];
  S.space = ['pc', 'sent'].includes(space) ? space : 'pc';
  S.mode = rest === 'recent' ? 'recent' : 'folder';
  S.path = S.mode === 'folder' ? (rest || '/') : '/';
  S.q = ''; $('#q').value = '';
  load();
}
addEventListener('hashchange', fromHash);

// --- loading -------------------------------------------------------------------------------------------------------
let token = 0;
async function load() {
  const my = ++token;
  renderChrome();
  $('#list').innerHTML = '<div class="skeleton"></div>'.repeat(6);
  try {
    const j = S.q ? await api(`/api/search?${q({space: S.space, q: S.q})}`)
      : S.mode === 'recent' ? await api(`/api/recent?${q({space: S.space, n: 60})}`)
      : await api(`/api/list?${q({space: S.space, path: S.path})}`);
    if (my !== token) return;
    S.items = j.items || []; S.sel = -1;
    meter(j);
    render();
  } catch (e) {
    if (my !== token) return;
    S.items = [];
    $('#list').innerHTML = `<div class="empty"><b>Can't open this</b>${esc(e.message)}</div>`;
  }
}

function meter(j) {
  if (!j.total) return;
  const used = j.total - j.free;
  $('#meter .bar i').style.width = `${(100 * used / j.total).toFixed(1)}%`;
  $('#meter p').textContent = `${size(j.free)} free of ${size(j.total)}`;
}

// --- drawing -----------------------------------------------------------------------------------------------------------
function renderChrome() {
  if (!S.me) return;
  $('#spaces').innerHTML = S.me.spaces.map(s => `<button class="nav ${S.space === s.id && S.mode === 'folder' && !S.q ? 'on' : ''}" data-space="${s.id}">
    <i>${s.id === 'pc' ? '▣' : '↧'}</i><span>${esc(s.label)}<small>${esc(s.where)}</small></span></button>`).join('');
  $('#nav-recent').classList.toggle('on', S.mode === 'recent' && !S.q);
  const label = S.me.spaces.find(s => s.id === S.space)?.label || '';
  const parts = S.path.split('/').filter(Boolean);
  $('#crumbs').innerHTML = S.q ? `<li><button data-path="/">${esc(label)}</button></li><li>“${esc(S.q)}”</li>`
    : S.mode === 'recent' ? `<li><button data-path="/">${esc(label)}</button></li><li>Recent</li>`
    : `<li><button data-path="/">${esc(label)}</button></li>` + parts.map((p, i) =>
      `<li>${i === parts.length - 1 ? esc(p) : `<button data-path="/${parts.slice(0, i + 1).map(encodeURIComponent).join('/')}">${esc(p)}</button>`}</li>`).join('');
  $('#drop-to').textContent = `into ${label}${S.mode === 'folder' && !S.q ? ' / ' + (parts.join(' / ') || '') : ''}`;
}

function sorted() {
  const {k, dir} = S.sort;
  return [...S.items].sort((a, b) => (b.dir - a.dir) || (S.mode === 'recent' && !S.q ? b.t - a.t :
    (k === 'name' ? a.name.localeCompare(b.name, undefined, {numeric: true, sensitivity: 'base'}) : (a[k] || 0) - (b[k] || 0)) * dir));
}

function render() {
  S.items = sorted();
  const list = $('#list');
  list.className = 'list' + (S.view === 'grid' ? ' grid' : '');
  $('#cols').classList.toggle('hide', S.view === 'grid');
  for (const el of document.querySelectorAll('.cols span')) el.classList.toggle('on', el.dataset.sort === S.sort.k);
  $('#b-view').textContent = S.view === 'grid' ? '☰' : '▦';
  if (!S.items.length) {
    list.innerHTML = `<div class="empty"><b>${S.q ? 'Nothing found' : 'Empty'}</b>${S.q ? 'No file names match.' :
      'Drop files anywhere on this page to upload them here.'}</div>`;
    return;
  }
  const showWhere = S.q || S.mode === 'recent';
  list.innerHTML = S.items.map((it, i) => `<div class="row ${it.dir ? 'd' : ''}" data-i="${i}">
    <span class="ico ${it.dir ? 'dir' : ''}">${it.dir ? '▸' : (PICS.test(it.name) || PDF.test(it.name)) ?
      `<img loading="lazy" alt="" data-thumb="${i}">` : esc(ext(it.name) || '—')}</span>
    <span class="nm">${esc(it.name)}${showWhere ? `<span class="where">${esc(it.path.split('/').slice(0, -1).join('/') || '/')}</span>` : ''}</span>
    <span class="sz">${it.dir ? '' : size(it.size)}</span><span class="tm">${when(it.t)}</span></div>`).join('');
  thumbs();
}

const seen = new IntersectionObserver(es => es.forEach(e => {
  if (!e.isIntersecting) return;
  const img = e.target, it = S.items[+img.dataset.thumb];
  seen.unobserve(img);
  if (!it) return;
  img.src = `/api/thumb?${q({space: S.space, path: it.path, t: it.t, size: it.size})}`;
  img.onerror = () => { img.replaceWith(Object.assign(document.createElement('span'), {textContent: ext(it.name)})); };
}), {root: $('#list'), rootMargin: '200px'});
function thumbs() { document.querySelectorAll('img[data-thumb]').forEach(i => seen.observe(i)); }

function select(i, scroll = true) {
  S.sel = Math.max(-1, Math.min(S.items.length - 1, i));
  document.querySelectorAll('.row.sel').forEach(r => r.classList.remove('sel'));
  const r = document.querySelector(`.row[data-i="${S.sel}"]`);
  if (r) { r.classList.add('sel'); if (scroll) r.scrollIntoView({block: 'nearest'}); }
}
function open(i) {
  const it = S.items[i];
  if (!it) return;
  if (it.dir) { S.q = ''; go(S.space, it.path); return; }
  view(i);
}

// --- the viewer -------------------------------------------------------------------------------------------------------
function view(i) {
  const it = S.items[i];
  if (!it || it.dir) return;
  S.viewing = i;
  const v = $('#viewer'), body = $('#v-body'), url = fileUrl(it);
  $('#v-name').textContent = it.name;
  $('#v-meta').textContent = `${size(it.size)} · ${when(it.t)}`;
  $('#v-dl').href = fileUrl(it, true);
  body.innerHTML = '';
  if (PICS.test(it.name) && !/\.heic$/i.test(it.name)) body.innerHTML = `<img src="${url}" alt="">`;
  else if (PDF.test(it.name)) body.innerHTML = `<iframe src="${url}" title="${esc(it.name)}"></iframe>`;
  else if (VIDEO.test(it.name)) body.innerHTML = `<video src="${url}" controls autoplay></video>`;
  else if (AUDIO.test(it.name)) body.innerHTML = `<audio src="${url}" controls autoplay></audio>`;
  else if (TEXT.test(it.name) && it.size < 2e6) {
    body.innerHTML = '<pre>Loading…</pre>';
    fetch(url).then(r => r.text()).then(t => { if (S.viewing === i) body.firstChild.textContent = t; });
  } else body.innerHTML = `<div class="none"><b>${esc(ext(it.name) || 'File')}</b>No preview for this kind of file.<br><br>
    <a class="btn" href="${fileUrl(it, true)}">↓ Download ${size(it.size)}</a></div>`;
  v.hidden = false;
}
function closeView() { $('#viewer').hidden = true; $('#v-body').innerHTML = ''; S.viewing = -1; }
function step(d) {
  let i = S.viewing;
  do { i += d; } while (S.items[i] && S.items[i].dir);
  if (S.items[i]) { view(i); select(i); }
}
$('#v-close').onclick = closeView;
$('#v-prev').onclick = () => step(-1);
$('#v-next').onclick = () => step(1);

// --- uploads -----------------------------------------------------------------------------------------------------------
function upload(files) {
  if (!files.length) return;
  const folder = S.mode === 'folder' && !S.q ? S.path : '/';
  if (S.mode !== 'folder' || S.q) flash(`Uploading to the top of ${S.me.spaces.find(s => s.id === S.space).label}`);
  let left = files.length;
  for (const f of files) {
    const li = Object.assign(document.createElement('li'), {className: 'up'});
    li.innerHTML = `<div class="n"><span>${esc(f.name)}</span><span>0%</span></div><div class="bar"><i></i></div>`;
    $('#uploads').append(li);
    const x = new XMLHttpRequest();
    x.open('POST', `/api/upload?${q({space: S.space, path: folder, name: f.name})}`);
    x.upload.onprogress = e => { if (e.lengthComputable) { const p = Math.round(100 * e.loaded / e.total);
      li.querySelector('i').style.width = p + '%'; li.querySelector('.n span:last-child').textContent = p + '%'; } };
    const end = (ok, msg) => {
      li.classList.add(ok ? 'done' : 'fail');
      li.querySelector('.n span:last-child').textContent = ok ? 'done' : msg;
      setTimeout(() => li.remove(), ok ? 2500 : 8000);
      if (--left === 0) load();
    };
    x.onload = () => { let j = {}; try { j = JSON.parse(x.responseText); } catch {} end(x.status < 300 && !j.error, j.error || `error ${x.status}`); };
    x.onerror = () => end(false, 'failed');
    x.send(f);
  }
}
$('#pick').onchange = e => { upload([...e.target.files]); e.target.value = ''; };
let dragDepth = 0;
addEventListener('dragenter', e => { if (e.dataTransfer?.types?.includes('Files')) { dragDepth++; $('#drop').classList.add('on'); } });
addEventListener('dragleave', () => { if (--dragDepth <= 0) { dragDepth = 0; $('#drop').classList.remove('on'); } });
addEventListener('dragover', e => e.preventDefault());
addEventListener('drop', e => { e.preventDefault(); dragDepth = 0; $('#drop').classList.remove('on'); upload([...(e.dataTransfer?.files || [])]); });

async function newFolder() {
  if (S.mode !== 'folder' || S.q) return flash('Open a folder first');
  const name = prompt('New folder');
  if (!name) return;
  try { await api('/api/mkdir', {method: 'POST', body: JSON.stringify({space: S.space, path: S.path, name})}); load(); }
  catch (e) { flash(e.message); }
}

// --- input -----------------------------------------------------------------------------------------------------------------
const touch = matchMedia('(pointer: coarse)').matches;
$('#list').addEventListener('click', e => {
  const r = e.target.closest('.row'); if (!r) return;
  touch ? open(+r.dataset.i) : select(+r.dataset.i, false);
});
$('#list').addEventListener('dblclick', e => { const r = e.target.closest('.row'); if (r) open(+r.dataset.i); });
document.addEventListener('click', e => {
  const sp = e.target.closest('[data-space]');
  if (sp) { document.body.classList.remove('rail-open'); S.q = ''; go(sp.dataset.space, '/'); return; }
  const cr = e.target.closest('[data-path]');
  if (cr) { S.q = ''; go(S.space, decodeURIComponent(cr.dataset.path)); return; }
  const so = e.target.closest('[data-sort]');
  if (so) { S.sort = {k: so.dataset.sort, dir: S.sort.k === so.dataset.sort ? -S.sort.dir : 1}; store.set('drive.sort', S.sort); render(); }
});
$('#nav-recent').onclick = () => { document.body.classList.remove('rail-open'); go(S.space, '/', 'recent'); };
$('#menu').onclick = () => document.body.classList.toggle('rail-open');
$('#b-view').onclick = () => { S.view = S.view === 'grid' ? 'list' : 'grid'; store.set('drive.view', S.view); render(); };
$('#b-folder').onclick = newFolder;
let typing;
$('#q').addEventListener('input', e => {
  clearTimeout(typing);
  typing = setTimeout(() => { S.q = e.target.value.trim(); if (S.q.length === 1) return; load(); }, 280);
});
document.addEventListener('keydown', e => {
  const inField = e.target.matches('input');
  if (!$('#viewer').hidden) {
    if (e.key === 'Escape') closeView(); else if (e.key === 'ArrowRight') step(1); else if (e.key === 'ArrowLeft') step(-1);
    return;
  }
  if (inField) { if (e.key === 'Escape') { e.target.value = ''; S.q = ''; e.target.blur(); load(); } else if (e.key === 'ArrowDown') { e.target.blur(); select(0); } return; }
  if (e.key === '/') { e.preventDefault(); $('#q').focus(); }
  else if (e.key === 'ArrowDown' || e.key === 'j') { e.preventDefault(); select(S.sel + (S.view === 'grid' ? 1 : 1)); }
  else if (e.key === 'ArrowUp' || e.key === 'k') { e.preventDefault(); select(S.sel - 1); }
  else if (e.key === 'Enter' && S.sel >= 0) open(S.sel);
  else if (e.key === ' ' && S.sel >= 0) { e.preventDefault(); view(S.sel); }
  else if (e.key === 'Backspace' && S.mode === 'folder' && S.path !== '/') go(S.space, S.path.split('/').slice(0, -1).join('/') || '/');
  else if (e.key === 'u') $('#pick').click();
  else if (e.key === 'n') newFolder();
  else if (e.key === 'v') $('#b-view').click();
  else if (e.key === 'r') go(S.space, '/', 'recent');
});

// --- start -----------------------------------------------------------------------------------------------------------------
api('/api/me').then(me => { S.me = me; $('#who').textContent = me.name; fromHash(); })
  .catch(e => { $('#list').innerHTML = `<div class="empty"><b>Not for this device</b>${esc(e.message)}</div>`; });
