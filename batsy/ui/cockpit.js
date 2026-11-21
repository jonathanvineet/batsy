// The cockpit: everything at a glance. Data comes from batsy/cockpit.py ({cmd: "data"} every ~2 s while it's open);
// Batsy live comes as it happens (listen / think / say / heard / level / speakwave).
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const body = document.body;

// --- clock ---------------------------------------------------------------------------------------------------------
function tick() {
  const d = new Date();
  $('#hm').textContent = d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hour12: false});
  $('#date').textContent = d.toLocaleDateString([], {weekday: 'long', day: 'numeric', month: 'long'});
}
tick(); setInterval(tick, 5000);

const ago = s => s < 60 ? 'just now' : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
const dur = s => s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s` :
  `${Math.floor(s / 3600)}h ${Math.floor(s % 3600 / 60)}m`;

// --- the picture ----------------------------------------------------------------------------------------------------
function render(d) {
  // happening now
  const jobs = d.jobs || [];
  $('#now ol').innerHTML = jobs.length ? jobs.map(j => {
    const pct = j.pct == null ? null : Math.max(2, Math.min(100, j.pct));
    return `<li><b>${esc(j.label)}</b><div class="step"><span>${esc(j.step || 'Working…')}</span><span>${dur(j.secs || 0)}</span></div>
      <div class="bar ${pct == null ? 'spin' : ''}"><i style="width:${pct ?? 0}%"></i></div></li>`;
  }).join('') : '<li class="empty">Nothing running. Batsy is ready.</li>';
  // waiting for you
  const asks = d.asks || [];
  $('#asks').hidden = !asks.length;
  $('#asks ol').innerHTML = asks.map(a => `<li>${esc(a.question)}</li>`).join('');
  // just finished
  const done = d.done || [];
  $('#done ol').innerHTML = done.length ? done.map(x => `<li><b>${esc(x.label)}</b><span class="${x.ok === false ? 'fail' : 'ok'}">
    ${x.ok === false ? '✕' : '✓'}</span> ${esc((x.result || '').slice(0, 90))} · ${ago(x.ago || 0)}</li>`).join('')
    : '<li class="empty">Nothing in the last half hour.</li>';

  // machines
  const ms = d.machines || [];
  $('#machines .rows').innerHTML = ms.map(m => {
    if (!m.online) return `<div class="m"><div class="name"><i class="dot ${m.online === false ? 'off' : ''}"></i>${esc(m.name)}
      <small>${m.online === false ? 'off' : 'checking'}</small></div></div>`;
    const hot = (m.temp || 0) >= 90;
    const ai = m.ai ? (m.ai.serving ? `<div class="ai">🧠 ${esc((m.ai.model || 'AI').split(':')[0])}${m.ai.first_s ? ` · speaks in ${m.ai.first_s}s` : ''}</div>`
      : '<div class="ai" style="color:var(--muted)">AI on standby</div>') : '';
    return `<div class="m"><div class="name"><i class="dot on"></i>${esc(m.name)}<small>${m.battery != null ?
      `🔋 ${m.battery}%${m.charging ? ' ⚡' : ''}` : ''}</small></div>
      <div class="gauges">${gauge('CPU', Math.round(m.cpu || 0) + '%', m.cpu)}${gauge('Temp', (m.temp ?? '–') + '°', m.temp, hot)}
      ${gauge('RAM', m.ram_used_gb != null ? `${m.ram_used_gb}/${m.ram_total_gb}G` : Math.round(m.ram || 0) + '%', m.ram)}</div>${ai}</div>`;
  }).join('') || '<p class="empty">No machines yet.</p>';

  // connected devices (+ the printer)
  const dev = [...(d.devices || [])];
  if (d.printer) dev.unshift({name: '3D printer', online: !!d.printer.connected});
  const up = dev.filter(x => x.online).length;
  $('#devices ul').innerHTML = dev.map(x => `<li class="${x.online ? '' : 'down'}"><i class="dot ${x.online ? 'on' : 'off'}"></i>
    <span>${esc(x.name)}</span></li>`).join('');
  const off = ms.filter(m => m.online === false).map(m => m.name);
  $('#health').textContent = `${up} of ${dev.length} devices online` + (off.length ? ` · ${off.join(', ')} off` : '');

  // claude
  const c = d.claude;
  $('#claude').innerHTML = c ? ['session', 'week'].map(k => ring(k === 'session' ? '5-hour' : 'Week', (c[k] || {}).pct)).join('') : '';

  renderWidgets(d);
}

function gauge(label, value, pct, hot = false) {
  return `<div class="g ${hot ? 'hot' : ''}">${label}<b>${esc(value)}</b><div class="bar"><i style="width:${Math.min(100, pct || 0)}%"></i></div></div>`;
}
function ring(label, pct) {
  const p = Math.max(0, Math.min(100, pct || 0)), c = 2 * Math.PI * 14;
  return `<div class="ring"><svg viewBox="0 0 34 34"><circle class="bg" cx="17" cy="17" r="14"/><circle class="fg" cx="17" cy="17" r="14"
    stroke-dasharray="${c}" stroke-dashoffset="${c * (1 - p / 100)}" ${p >= 90 ? 'style="stroke:var(--bad)"' : ''}/></svg>
    <span>${label}<br><b>${Math.round(p)}%</b></span></div>`;
}

function renderWidgets(d) {
  // printer
  const p = d.printer;
  let html = '<p class="sub">Not connected</p>';
  if (p && p.connected) {
    if (p.printing || p.paused) {
      const pct = p.progress || 0, left = p.remaining_s;
      html = `<div class="big">${pct < 10 ? pct.toFixed(1) : Math.round(pct)}%</div>
        <div class="line">${esc((p.job || '').split(' · ')[0])}</div>
        <div class="sub">${p.paused ? 'Paused' : left ? `${dur(Math.round(left))} left · done ${new Date(Date.now() + left * 1000)
          .toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}` : 'Printing'} · ${Math.round(p.hotend || 0)}° / ${Math.round(p.bed || 0)}°</div>
        <div class="bar"><i style="width:${pct}%"></i></div>`;
    } else html = `<div class="line">Idle</div><div class="sub">Nozzle ${Math.round(p.hotend || 0)}° · bed ${Math.round(p.bed || 0)}°</div>`;
  }
  $('#w-printer .body').innerHTML = html;
  // music
  const m = d.music;
  $('#w-music .body').innerHTML = m && m.title ? `${m.art ? `<img src="${esc(m.art)}" alt="">` : ''}<div class="t">
    <div class="line">${esc(m.title)}</div><div class="sub">${esc(m.artist || '')}</div>
    <div class="sub">${m.playing ? '▶ playing' : '❚❚ paused'}${m.device ? ' · ' + esc(m.device) : ''}</div></div>` : '<p class="sub">Nothing playing</p>';
  // research
  const r = d.research || {};
  $('#w-research .body').innerHTML = r.active ? `<div class="line">${esc(r.active.name)}</div>
    <div class="sub">${r.active.notes} notes · ${r.active.sources} sources · on now</div>` + (r.all || []).filter(x => x.name !== r.active.name)
    .slice(0, 2).map(x => `<div class="sub">${esc(x.name)}</div>`).join('')
    : (r.all || []).length ? (r.all || []).slice(0, 3).map(x => `<div class="sub">${esc(x.name)} · ${x.sources} sources</div>`).join('')
    : '<p class="sub">No research yet</p>';
  // today (the to-dos under the clock) + the next reminder
  const b = d.batsy || {}, today = (b.today || []).map(t => typeof t === 'string' ? {text: t} : t);
  const rem = b.next_reminder;
  $('#w-today .body').innerHTML = (rem ? `<div class="sub">⏰ ${esc(rem.what)} · ${new Date(rem.at * 1000)
    .toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}</div>` : '') +
    (today.length ? '<ul>' + today.slice(0, 4).map(t => `<li class="${t.done ? 'done' : ''}">${esc(t.text)}</li>`).join('') + '</ul>'
      : '<p class="sub">Nothing planned</p>');
  // files
  const f = d.files || [];
  $('#w-files .body').innerHTML = f.length ? '<ul>' + f.map(x => `<li>${esc(x.name)}<small>${ago(Math.max(0, d.t - x.t))}</small></li>`).join('')
    + '</ul>' : '<p class="sub">Nothing received lately</p>';
}

// --- Batsy live: the wave -------------------------------------------------------------------------------------------
const live = {state: 'idle', level: 0, target: 0, env: null, envT0: 0, envDt: 0.04, t0: performance.now()};
const canvas = $('#wave'), ctx = canvas.getContext('2d');
function size() { const r = canvas.getBoundingClientRect(); canvas.width = r.width * devicePixelRatio; canvas.height = r.height * devicePixelRatio; }
addEventListener('resize', size); size();

function setState(s) {
  live.state = s;
  $('#live').className = 'live ' + s;
  $('#state').textContent = {idle: 'Idle', listen: 'Listening', think: 'Thinking', speak: 'Speaking'}[s] || s;
}

let lastFrame = 0;
const FPS = {idle: 12, think: 20, listen: 30, speak: 30};  // calm when calm: this laptop runs hot
function draw(now) {
  requestAnimationFrame(draw);
  if (!body.classList.contains('on') || now - lastFrame < 1000 / (FPS[live.state] || 12)) return;
  lastFrame = now;
  const W = canvas.width, H = canvas.height, t = (now - live.t0) / 1000;
  // how loud right now: your mic while listening, Batsy's own sentence while speaking
  if (live.state === 'speak' && live.env) {
    const i = Math.max(0, Math.floor(((now - live.envT0) / 1000) / live.envDt));  // (a frame can be stamped a hair
    live.target = i < live.env.length ? live.env[i] : 0.05;                           //  before the sentence arrived)
  } else if (live.state === 'think') live.target = 0.18 + 0.08 * Math.sin(t * 2.2);
  else if (live.state !== 'listen') live.target = 0.06;
  if (!Number.isFinite(live.target)) live.target = 0.06;
  live.level += (live.target - live.level) * (live.state === 'speak' ? 0.35 : 0.18);
  if (!Number.isFinite(live.level)) live.level = 0.06;  // one bad value must never freeze the wave
  ctx.clearRect(0, 0, W, H);
  const colour = live.state === 'listen' ? [56, 225, 255] : live.state === 'idle' ? [141, 149, 165] : [255, 194, 14];
  const mid = H * 0.46, amp = H * 0.34 * Math.max(0.04, live.level);
  for (let k = 0; k < 4; k++) {  // a few layered waves: the front one bright, the ones behind softer
    const freq = 1.3 + k * 0.55, speed = (live.state === 'think' ? 0.9 : 1.6) + k * 0.4, phase = k * 1.7;
    ctx.beginPath();
    for (let x = 0; x <= W; x += 7 * devicePixelRatio) {
      const u = x / W, taper = Math.sin(Math.PI * u) ** 1.6;  // calm at the edges, alive in the middle
      const y = mid + amp * taper * (Math.sin(u * Math.PI * 2 * freq + t * speed + phase) * 0.75
        + Math.sin(u * Math.PI * 2 * freq * 2.3 - t * speed * 1.3) * 0.25) * (1 - k * 0.18);
      x ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
    if (!k) {  // the glow: one wide, faint stroke under the bright one (a blur filter costs far more)
      ctx.strokeStyle = `rgba(${colour}, .16)`; ctx.lineWidth = 12 * devicePixelRatio; ctx.stroke();
    }
    ctx.strokeStyle = `rgba(${colour}, ${k ? 0.32 - k * 0.06 : 0.95})`;
    ctx.lineWidth = (k ? 1.5 : 3) * devicePixelRatio;
    ctx.stroke();
  }
}
requestAnimationFrame(draw);

// --- commands from Batsy ------------------------------------------------------------------------------------------
const handlers = {
  appear() { body.classList.add('on'); size(); },
  away() { body.classList.remove('on'); },
  data(m) { render(m); },
  state(m) { setState(m.state || 'idle'); if (m.you) $('#you').textContent = m.you; if (m.said) $('#said').textContent = m.said; },
  listen(m) { if (m.fresh) { $('#you').textContent = ''; $('#said').textContent = ''; } setState('listen'); },
  level(m) { if (live.state === 'listen') live.target = Math.max(0, Math.min(1, +m.v || 0)); },
  partial(m) { $('#you').textContent = m.text || ''; },
  heard(m) { $('#you').textContent = m.text || ''; },
  think() { setState('think'); },
  say(m) {
    const s = $('#said');
    s.textContent = (live.state === 'speak' ? s.textContent + ' ' : '') + (m.text || '');
    setState('speak');
  },
  speakwave(m) { live.env = m.env || null; live.envDt = +m.dt || 0.04; live.envT0 = performance.now(); if (live.state !== 'speak') setState('speak'); },
  status(m) { $('#status').textContent = m.text || ''; },
  quiet() { setState('idle'); },
  done() { setState('idle'); $('#status').textContent = ''; },
};
window.batsy = {cmd(m) { try { (handlers[m.cmd] || (() => {}))(m); } catch (e) { console.error('cockpit ' + m.cmd + ': ' + e); } }};
