const $ = s => document.querySelector(s);
let tab;

async function show() {
  const s = await settings();
  [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  $('#pair').hidden = !!s.token;
  $('#main').hidden = !s.token;
  $('#state').textContent = s.token ? (s.paused ? 'paused' : 'connected') : 'not paired';
  $('#page').textContent = tab ? tab.title : '';
  $('#paused').checked = s.paused;
  if (tab && SKIP.test(tab.url || '')) $('#page').textContent = 'Batsy ignores this page';
}

$('#go').onclick = async () => {
  const code = $('#code').value.replace(/\D/g, '');
  $('#go').disabled = true;
  for (const base of SERVERS) {
    try {
      const r = await fetch(base + '/api/pair', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                 body: JSON.stringify({code}), signal: AbortSignal.timeout(4000)});
      const j = await r.json();
      if (j.token) {
        await chrome.storage.local.set({token: j.token, server: base, device: $('#device').value.trim() || 'browser'});
        return show();
      }
      $('#state').textContent = j.error || 'wrong code';
      break;
    } catch (e) { /* the next address */ }
  }
  $('#go').disabled = false;
};

async function act(action, extra = {}) {
  document.querySelectorAll('button').forEach(b => b.disabled = true);
  $('#out').textContent = 'Working on it…';
  let text = '';
  try {
    const [r] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: () => document.body.innerText.slice(0, 20000)});
    text = r.result || '';
  } catch (e) { /* a page scripts can't read: title and address only */ }
  const r = await call('/api/browser/action', {action, url: tab.url, title: tab.title, text, ...extra}, {timeout: 120000});
  $('#out').textContent = r.say || r.error || 'Done.';
  if (r.error === 'not paired') { await chrome.storage.local.set({token: ''}); show(); }
  document.querySelectorAll('button').forEach(b => b.disabled = false);
}

document.querySelectorAll('[data-a]').forEach(b => b.onclick = () => act(b.dataset.a));
$('#askf').onsubmit = e => { e.preventDefault(); act('ask', {question: $('#q').value}); };
$('#paused').onchange = () => chrome.storage.local.set({paused: $('#paused').checked}).then(show);
show();
