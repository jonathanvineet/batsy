// Screensaver dashboard: a clock and the widgets Batsy chose (batsy/saver.py), laid out on a 3-column grid.
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};

function fmt(n) {
  n = +n || 0;
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(n);
}

function bar(pct) {
  const b = el('div', 'bar'), i = el('i');
  i.style.width = `${Math.max(1.5, Math.min(100, pct))}%`;
  if (pct >= 90) i.className = 'full'; else if (pct >= 70) i.className = 'warn';
  b.appendChild(i);
  return b;
}

function meter(label, b, session) {
  const m = el('div', 'meter');
  const used = b.used || 0, limit = b.limit || 0, pct = Math.min(100, b.pct || 0);
  m.appendChild(el('div', 'label', label));
  const big = el('div', 'big', fmt(used));
  if (limit) big.appendChild(el('small', null, `of ${fmt(limit)}`));
  m.appendChild(big);
  if (limit) m.appendChild(bar(pct));
  let sub = 'Not used yet';
  if (b.resets_at && limit) {
    const d = new Date(b.resets_at * 1000);
    const when = session ? d.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'})
                         : d.toLocaleDateString([], {weekday: 'short'}) + ' ' + d.toLocaleTimeString([], {hour: 'numeric'});
    sub = `${pct.toFixed(0)}% used, resets ${when}`;
  }
  m.appendChild(el('div', 'sub', sub));
  return m;
}

const EMPTY = {running: 'Nothing running', waiting: 'Nothing to answer', upcoming: 'Nothing scheduled',
               agenda: 'Nothing planned', project: 'No updates yet', command: 'No output'};

function widget(w, d) {
  const kind = w.type, box = el('section', 'w');
  const span = Math.max(1, Math.min(3, +w.span || 1));
  if (span > 1) box.classList.add(`span${span}`);
  if (w.highlight) box.classList.add('hl');
  const title = w.title || d.title || '';
  if (kind === 'photo') {
    if (d.path) { const img = el('img', 'photo'); img.src = 'batsy://file' + encodeURI(d.path); box.appendChild(img); }
    box.style.borderTop = 'none';
    return box;
  }
  if (title) box.appendChild(el('h3', null, title));
  if (d.error) { box.appendChild(el('p', 'err', d.error)); return box; }
  if (kind === 'claude_budget') {
    const m = el('div', 'meters');
    m.append(meter('This session', d.session || {}, true), meter('This week', d.week || {}, false));
    box.appendChild(m);
  } else if (kind === 'local_tokens') {
    const big = el('div', 'big', fmt(d.tokens)); big.appendChild(el('small', null, 'tokens'));
    box.append(big, el('div', 'sub', `Free, ${d.requests || 0} requests today`));
  } else if (kind in EMPTY) {
    const rows = d.rows || d.lines || [];
    if (!rows.length) box.appendChild(el('p', 'empty', EMPTY[kind]));
    else {
      const ul = el('ul');
      rows.slice(0, 7).forEach(r => ul.appendChild(el('li', null, r)));
      box.appendChild(ul);
    }
    if (kind === 'command') box.classList.add('mono');
  } else if (kind === 'system') {
    const g = el('div', 'sys');
    for (const [label, val, unit] of [['CPU', d.cpu, '%'], ['Memory', d.ram, '%'], ['Temp', d.temp, '°C'], ['Battery', d.battery, '%']]) {
      if (val == null) continue;
      g.append(el('span', null, label), bar(label === 'Battery' ? Math.min(100, val) : val), el('b', null, `${Math.round(val)}${unit}`));
    }
    box.appendChild(g);
  } else if (kind === 'now_playing') {
    const p = el('p', 'np'), eq = el('span', 'eq');
    for (let i = 0; i < 4; i++) eq.appendChild(el('i'));
    p.append(eq, document.createTextNode(d.text || ''));
    box.appendChild(p);
  } else if (kind === 'countdown') {
    box.append(el('div', 'big', d.big || ''), el('div', 'sub', d.sub || ''));
  } else if (kind === 'weather') {
    const big = el('div', 'big', `${d.temp ?? '–'}°`);
    const hum = d.humidity != null ? `, ${d.humidity}% humidity` : '';
    box.append(big, el('div', 'sub', `${d.desc || ''} in ${d.place || ''}${hum}`));
  } else if (kind === 'quote') {
    box.appendChild(el('p', 'quote', d.text || ''));
  } else if (kind === 'note') {
    box.appendChild(el('p', 'note', d.text || ''));
  }
  return box;
}

export function tickClock(root) {
  const now = new Date();
  const hm = now.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hour12: false});
  const date = now.toLocaleDateString([], {weekday: 'long', month: 'long', day: 'numeric'});
  const t = root.querySelector('.clock .hm'), d = root.querySelector('.clock .date');
  if (t.textContent !== hm) t.textContent = hm;
  if (d.textContent !== date) d.textContent = date;
}

export function renderSaver(root, m) {
  const widgets = m.widgets || [], data = m.data || {};
  root.querySelector('.clock').hidden = !widgets.some(w => w.type === 'clock');
  const grid = root.querySelector('.grid');
  const out = [];
  widgets.forEach((w, i) => { if (w.type !== 'clock') out.push(widget(w, data[String(i)] || {})); });
  grid.replaceChildren(...out);
  tickClock(root);
}
