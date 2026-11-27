// Talking to Batsy: the laptop itself when the browser is on it, else over Tailscale.
const SERVERS = ['http://127.0.0.1:8765', 'https://workstation.taila566eb.ts.net', 'http://100.117.229.111:8765'];
// Never sent: login, bank and payment pages, private windows, the browser's own pages.
const SKIP = /^(?:chrome|brave|about|edge|chrome-extension|file|data|blob):|\/\/(?:localhost|127\.0\.0\.1|accounts\.google|login\.|auth\.)|\/(?:login|signin|oauth|callback|checkout|payment)\b|bank|paypal|razorpay|netbanking/i;

async function settings() {
  return chrome.storage.local.get({token: '', server: '', device: '', paused: false});
}

async function call(path, body, opts = {}) {
  const s = await settings();
  const order = s.server ? [s.server, ...SERVERS.filter(x => x !== s.server)] : SERVERS;
  for (const base of order) {
    try {
      const r = await fetch(base + path, {
        method: 'POST', signal: AbortSignal.timeout(opts.timeout || 4000),
        headers: {'Content-Type': 'application/json', ...(s.token ? {Authorization: 'Bearer ' + s.token} : {})},
        body: JSON.stringify(body),
      });
      if (base !== s.server) chrome.storage.local.set({server: base});
      if (r.status === 401) return {error: 'not paired'};
      return await r.json();
    } catch (e) { /* not reachable from here: the next address */ }
  }
  return {error: 'Batsy is not reachable'};
}
