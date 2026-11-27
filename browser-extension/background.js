importScripts('common.js');

// The tab you're on: its title and address only, when you switch to it or it finishes loading.
let last = '';
async function report(tab) {
  if (!tab || !tab.url || tab.incognito || SKIP.test(tab.url) || tab.url === last) return;
  const s = await settings();
  if (!s.token || s.paused) return;
  last = tab.url;
  call('/api/browser/visit', {url: tab.url, title: tab.title || '', device: s.device});
}
chrome.tabs.onActivated.addListener(({tabId}) => chrome.tabs.get(tabId, report));
chrome.tabs.onUpdated.addListener((id, info, tab) => { if (info.status === 'complete' && tab.active) report(tab); });
chrome.windows.onFocusChanged.addListener(w => {
  if (w !== chrome.windows.WINDOW_ID_NONE) chrome.tabs.query({active: true, windowId: w}, t => report(t[0]));
});

// Right-click: the same as the popup's buttons
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({id: 'track', title: 'Batsy: keep track of this', contexts: ['page', 'link']});
  chrome.contextMenus.create({id: 'save', title: 'Batsy: save to brain', contexts: ['page', 'link']});
  chrome.contextMenus.create({id: 'ask', title: 'Batsy: what is this page about?', contexts: ['page']});
});
chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const text = await pageText(tab.id);
  const r = await call('/api/browser/action', {action: info.menuItemId, url: tab.url, title: tab.title, text}, {timeout: 120000});
  chrome.action.setBadgeText({text: r.error ? '!' : '✓', tabId: tab.id});
  setTimeout(() => chrome.action.setBadgeText({text: '', tabId: tab.id}), 4000);
});

// The page's visible text: only when you press a button, never on its own
async function pageText(tabId) {
  try {
    const [r] = await chrome.scripting.executeScript({target: {tabId}, func: () => document.body.innerText.slice(0, 20000)});
    return r.result || '';
  } catch (e) { return ''; }
}
