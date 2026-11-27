// The company widget (batsy/shop/): what's printing, what's next, today's money; tap for the whole queue.
// The paired phone is the admin: Print / Reprint / Top / Stop work from here (each asks first).
// Loaded after app.js and uses its helpers (api, $, esc, toast, confirmBox).
'use strict';
let shop = null;
const shopInr = n => (n < 0 ? '−' : '') + '₹' + Math.abs(Math.round(n || 0)).toLocaleString('en-IN');  // −₹32, not ₹-32
const shopHrs = h => { const m = Math.round((h || 0) * 60); return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`; };
const SHOP_STATE = {waiting: 'waiting', slicing: 'slicing', ready: 'ready', printing: 'printing', failed: 'reprint'};

async function loadShop() {
  try {
    shop = await api('/api/shop/overview');
    drawShop();
    if (!$('#shopsheet').hidden) drawShopSheet();
  } catch (e) {
    $('#shop-w .sh-today').textContent = String(e.message).startsWith('Not paired') ? '' : 'Batsy’s company desk isn’t answering.';
  }
}

function drawShop() {
  const s = shop, q = s.queue || [];
  const running = q.find(j => j.status === 'printing');
  const next = q.filter(j => j.status !== 'printing');
  const p = s.printer || {};
  $('#shop-w .sh-today').textContent = s.today || '';
  const c = s.counts || {};
  $('#shop-w .sh-chips').innerHTML = [
    ['Quotes', c.quote || 0], ['In work', (c.confirmed || 0) + (c.printing || 0)], ['Ready', c.ready || 0],
  ].map(([k, v]) => `<span class="${k === 'Ready' && v ? 'hot' : ''}"><b>${v}</b>${k}</span>`).join('') +
    (s.month ? `<span class="${s.month.profit < 0 ? 'bad' : ''}"><b>${shopInr(s.month.profit)}</b>30 d profit</span>` : '');
  $('#shop-w .sh-now').innerHTML = running
    ? `<div class="sh-run"><b>${esc(running.part)}</b><small>${esc(running.order_code)} · ${Math.round(p.progress || 0)}%${p.remaining_s ? ' · ' + shopHrs(p.remaining_s / 3600) + ' left' : ''}</small>
       <i style="width:${Math.max(2, p.progress || 0)}%"></i></div>`
    : (p.printing || p.paused)  // printing something that isn't a company job (a "print it"): still not free
      ? `<div class="sh-run"><b>${esc(p.job || 'Printing')}</b><small>${p.paused ? 'paused' : 'printing'} · ${Math.round(p.progress || 0)}%${p.remaining_s ? ' · ' + shopHrs(p.remaining_s / 3600) + ' left' : ''} · not a company job</small>
         <i style="width:${Math.max(2, p.progress || 0)}%"></i></div>`
      : `<div class="sh-run idle"><small>${p.connected ? 'Printer free' : 'Printer offline'}</small></div>`;
  $('#shop-w .sh-next').innerHTML = next.slice(0, 3).map(j => `<li><b>${esc(j.part)} <em>×${j.copies - j.done_copies}</em></b>
      <small>${esc(j.customer || j.order_code)} · ${shopHrs(j.left_hours)}</small><span class="st ${j.status}">${SHOP_STATE[j.status] || j.status}</span></li>`).join('')
    || '<li class="empty">Nothing queued</li>';
  // quotes waiting for your price, and recent failed / stopped prints (what they cost)
  const qs = s.quotes || [], ls = s.losses || [];
  $('#shop-w .sh-quotes').innerHTML = (qs.length ? `<h4>Quotes to price</h4>` + qs.map(o => `<li><b>${esc(o.title)}</b>
      <small>${esc(o.code)}</small><span class="pr">${o.override ? shopInr(o.override) : shopInr(o.price) + ' <em>suggested</em>'}</span></li>`).join('') : '')
    + (ls.length ? `<h4>Losses</h4>` + ls.map(r => `<li class="loss"><b>${esc(r.title)}</b>
      <small>${r.outcome === 'cancelled' ? 'stopped' : 'failed'} · ${Math.round(r.grams)} g · ${shopHrs(r.hours)}</small><span class="pr bad">−${shopInr(r.loss)}</span></li>`).join('') : '');
  const first = next[0];
  const busy = p.printing || p.paused || s.running;
  $('#shop-w .sh-btns').innerHTML = (first && !busy && first.status === 'ready'
    ? `<button data-shop="print" data-id="${first.id}" class="go">Print next</button>` : '') +
    `<button data-shop="open">Queue${q.length ? ' · ' + q.length : ''}</button>`;
}

function drawShopSheet() {
  const s = shop, q = s.queue || [], p = s.printer || {};
  const run = q.find(j => j.status === 'printing');
  $('#shoppanel').innerHTML = `<div class="sh-head"><h3>Company</h3><button data-shop="close" aria-label="Close">✕</button></div>
    <p class="sh-sub">${esc(s.today || '')}</p>
    ${run ? `<div class="sh-run big"><b>${esc(run.part)}</b><small>${esc(run.order_code)} · ${esc(run.customer || '')} · ${Math.round(p.progress || 0)}%
      ${p.remaining_s ? ' · ' + shopHrs(p.remaining_s / 3600) + ' left' : ''}</small><i style="width:${Math.max(2, p.progress || 0)}%"></i>
      <div class="sh-row">${p.paused ? '<button data-shop="resume">Resume</button>' : '<button data-shop="pause">Pause</button>'}
      <button data-shop="stop" class="danger">Stop</button></div></div>` : ''}
    <ol class="sh-list">${q.filter(j => j.status !== 'printing').map((j, i) => `<li>
      <div class="sh-l"><b>${i + 1}. ${esc(j.part)} <em>×${j.copies - j.done_copies}</em></b>
      <small>${esc(j.order_code)} · ${esc(j.customer || j.order_title || '')}${j.due ? ' · due ' + esc(j.due) : ''}<br>
      ${esc((j.options || {}).material || 'PLA')} · ${j.plate}/plate · ${shopHrs(j.plate_hours)} · ${Math.round(j.plate_grams)} g${j.note ? '<br><span class="warn">' + esc(j.note) + '</span>' : ''}</small></div>
      <div class="sh-acts"><span class="st ${j.status}">${SHOP_STATE[j.status] || j.status}</span>
      ${j.status === 'failed' ? `<button data-shop="reprint" data-id="${j.id}">Reprint</button>`
        : j.status === 'ready' && !run && !s.running ? `<button data-shop="print" data-id="${j.id}" class="go">Print</button>` : ''}
      ${i ? `<button data-shop="top" data-id="${j.id}" aria-label="To the top">⤒</button>` : ''}</div></li>`).join('')
      || '<li class="empty">The queue is empty. Confirm a quote in PrintDesk and it lands here, sliced.</li>'}</ol>
    ${(s.events || []).length ? `<h4>Just happened</h4><ul class="sh-ev">${s.events.slice(0, 6).map(e => `<li>${esc(e.text)}</li>`).join('')}</ul>` : ''}`;
}

const STOP_REASONS = ['Spaghetti / detached', 'Bed adhesion', 'Layer shift', 'Filament ran out', 'Clog', 'Wrong settings'];

document.addEventListener('click', async e => {
  const b = e.target.closest('[data-shop]');
  if (!b) return;
  e.stopPropagation();
  const a = b.dataset.shop, id = Number(b.dataset.id || 0);
  const j = shop && (shop.queue || []).find(x => x.id === id);
  if (a === 'open') { drawShopSheet(); $('#shopsheet').hidden = false; return; }
  if (a === 'close') { $('#shopsheet').hidden = true; return; }
  let body = null;
  if (a === 'print') {
    if (!(await confirmBox(`Print ${j ? j.part + ' (' + j.plate + ' on the plate, ' + shopHrs(j.plate_hours) + ')' : 'it'}? Is the bed clear?`, b, {yes: 'Print'}))) return;
    body = {action: 'print', id};
  } else if (a === 'stop') {
    const reason = await pickReason(b);
    if (reason === null) return;
    body = {action: 'stop', reason};
  } else if (a === 'reprint') body = {action: 'reprint', id};
  else if (a === 'top') body = {action: 'move', id, where: 'top'};
  else if (a === 'pause' || a === 'resume') body = {action: a};
  if (!body) return;
  try {
    const r = await api('/api/shop/queue', body, 30000);
    toast(r.say || 'Done.');
  } catch (err) { toast(err.message); }
  loadShop();
});

function pickReason(from) {  // stop: why (it goes on the order's failed prints); null = keep printing
  return new Promise(res => {
    const box = document.createElement('div');
    box.className = 'glass-ask floating on';
    box.innerHTML = `<div class="glass-card"><p>Stop the print? Batsy books what it used as a loss and keeps the job for a reprint.</p>
      <div class="sh-reasons">${STOP_REASONS.map(r => `<button>${r}</button>`).join('')}</div>
      <div class="glass-row"><button class="g-no">Keep printing</button><button class="g-yes danger">Stop</button></div></div>`;
    document.body.appendChild(box);
    let reason = '';
    box.querySelectorAll('.sh-reasons button').forEach(x => x.onclick = ev => {
      ev.stopPropagation(); reason = x.textContent;
      box.querySelectorAll('.sh-reasons button').forEach(y => y.classList.toggle('on', y === x));
    });
    const done = v => { box.remove(); res(v); };
    box.querySelector('.g-no').onclick = ev => { ev.stopPropagation(); done(null); };
    box.querySelector('.g-yes').onclick = ev => { ev.stopPropagation(); done(reason); };
  });
}

$('#shopsheet').addEventListener('click', e => { if (e.target.id === 'shopsheet') $('#shopsheet').hidden = true; });
loadShop();
setInterval(() => { if (!document.hidden) loadShop(); }, 6000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) loadShop(); });
