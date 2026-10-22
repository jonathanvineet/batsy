// Batsy overlay: receives commands from Python (window.batsy.cmd) and draws the conversation, pictures,
// cards, the live camera, the 3D model view and the screensaver. Events go back via console.log("@@batsy ...").
import {makeSky} from './sky.js';
import {Viewer} from './viewer.js';
import {renderSaver, tickClock} from './saver.js';

const $ = s => document.querySelector(s);
const send = ev => console.log('@@batsy ' + JSON.stringify(ev));
const body = document.body;
const params = new URLSearchParams(location.search);
const viewerOnly = params.get('mode') === 'viewer';

const sky = makeSky($('#sky'));
const viewer = makeViewer();

function makeViewer() {
  try {
    return new Viewer($('#viewer'), send);
  } catch (e) {  // no WebGL: say so in the frame instead of breaking the whole overlay
    console.warn('3D view unavailable: ' + e.message);
    const root = $('#viewer');
    root.querySelector('.hud .message').textContent = 'The 3D view needs graphics acceleration, which isn’t working right now.';
    const noop = () => {};
    return {show: on => (root.hidden = !on), setState: async s => { root.querySelector('.hud .lines').textContent = s.hud.join('\n'); },
            orbit: noop, zoom: noop, frame: noop, snapshot: () => null};
  }
}
const ui = {
  state: 'idle', status: '', statusT0: 0, answer: '', heard: '', partial: '', image: null, card: null,
  camera: false, model: false, saver: false, shown: false, lastInput: 0, mouse0: null,
};
const STATE_LABEL = {listen: 'Listening', think: 'Thinking', speak: 'Speaking'};

// --- layout: where the voice line sits, and which mode the screen is in -----------------------------------------
function hasMedia() { return !!(ui.image || ui.card || ui.camera || ui.model); }

function layout() {
  const media = hasMedia() && !ui.saver;
  body.classList.toggle('media', media);
  body.classList.toggle('model', ui.model && !ui.camera && !ui.saver);
  body.classList.toggle('saver', ui.saver);
  $('#media').hidden = !(ui.image || ui.camera) || (ui.model && !ui.camera);
  $('#card').hidden = !ui.card || ui.camera || ui.model;
  viewer.show(ui.model && !ui.camera && !ui.saver);
  const t = sky.target, h = innerHeight;
  if (ui.saver) {
    const orb = ui.saverStyle === 'orb';
    t.sigOn = orb ? 1 : 0;
    Object.assign(t, {x: 0.5, y: 0.5, r: 0.12, active: 0, think: 0, listen: 0, speak: 0});
  } else if (media) {  // a short line in the top-left corner, status beside it
    const r = 34 / h;
    Object.assign(t, {sigOn: 1, x: (innerWidth * 0.04 + 3.2 * 34) / innerWidth, y: (h * 0.065 + 17) / h, r});
  } else {
    Object.assign(t, {sigOn: 1, x: 0.5, y: 0.33, r: 0.125});
  }
  renderText();
}

function followSignal(sig) {
  const s = body.style, h = innerHeight;
  s.setProperty('--sx', `${sig.x * innerWidth}px`);
  s.setProperty('--sy', `${sig.y * h}px`);
  s.setProperty('--sr', `${sig.r * h}px`);
}

// --- text -------------------------------------------------------------------------------------------------
function renderText() {
  const heard = $('#heard');
  heard.textContent = ui.heard ? `“${ui.heard}”` : ui.partial;
  heard.classList.toggle('live', !ui.heard && !!ui.partial);
  $('#answer').textContent = ui.answer;
  // subtitles under media: the latest sentence or two
  const sentences = ui.answer.match(/[^.!?]+[.!?]*\s*/g) || [];
  let sub = '';
  for (let i = sentences.length - 1; i >= 0 && (sub.length + sentences[i].length) < 220; i--) sub = sentences[i] + sub;
  $('#subtitle').textContent = (sub || ui.answer.slice(-220)).trim() || (hasMedia() ? (ui.heard || ui.partial) : '');
  renderStatus();
}

function renderStatus() {
  const el = $('#status');
  const label = ui.status || STATE_LABEL[ui.state] || '';
  el.hidden = !label || ui.saver;
  if (!label) return;
  const busy = !!ui.status || ui.state === 'think';
  el.classList.toggle('busy', busy);
  el.classList.toggle('think', ui.state === 'think' && !ui.status);
  el.querySelector('.label').textContent = label;
  const secs = ui.status ? Math.floor((Date.now() - ui.statusT0) / 1000) : 0;
  el.querySelector('.secs').textContent = busy && secs >= 2 ? `${secs}s` : '';
  sky.setColor(ui.state === 'think' || (ui.status && ui.state !== 'listen') ? '#ffae1a' : '#ffc20e');
}

function setState(state) {
  ui.state = state;
  Object.assign(sky.target, {active: state === 'idle' ? 0 : 1, think: +(state === 'think'),
                             listen: +(state === 'listen'), speak: +(state === 'speak')});
  if (state !== 'listen') sky.target.level = 0;
  renderStatus();
}

// --- pictures, cards, camera ------------------------------------------------------------------------------
function showImage(url, caption, live) {
  const img = new Image();
  img.src = url;
  img.decode().then(() => {  // swap only once decoded: no flicker on the live feed
    $('#pic').src = url;
    if (!live) $('#media figcaption').textContent = caption || '';
  }).catch(() => {});
}

function renderCard(title, text) {
  const card = $('#card');
  card.querySelector('h2').textContent = title || '';
  const bodyEl = card.querySelector('.body');
  bodyEl.replaceChildren();
  let list = null;
  for (const raw of String(text || '').split('\n')) {
    const line = raw.trim();
    if (!line) { list = null; continue; }
    const bullet = line.match(/^[-*•]\s+(.*)/);
    if (bullet) {
      if (!list) { list = document.createElement('ul'); bodyEl.appendChild(list); }
      const li = document.createElement('li'); li.textContent = bullet[1]; list.appendChild(li);
    } else {
      list = null;
      const p = document.createElement('p'); p.textContent = line; bodyEl.appendChild(p);
    }
  }
}

// --- commands from Python ---------------------------------------------------------------------------------
const handlers = {
  appear() { ui.shown = true; body.classList.add('on'); sky.target.fade = 1; kick(); },
  hidden() {
    ui.shown = false; ui.saver = false; body.classList.remove('on'); sky.target.fade = 0;
    sky.u.uFade.value = 0; $('#saver').hidden = true; setState('idle'); layout();
  },
  listen(m) {
    leaveSaver();
    if (m.fresh) {
      Object.assign(ui, {answer: '', heard: '', partial: '', card: null, status: ''});
      if (!ui.camera) ui.image = null;
    }
    setState('listen'); layout();
  },
  level(m) { sky.target.level = Math.max(0, Math.min(1, +m.v || 0)); },
  partial(m) { ui.partial = m.text || ''; renderText(); },
  heard(m) { ui.heard = m.text || ''; ui.partial = ''; renderText(); },
  think() { leaveSaver(); setState('think'); },
  say(m) { leaveSaver(); ui.status = ''; ui.answer = (ui.answer + ' ' + (m.text || '')).trim().slice(-900); setState('speak'); renderText(); },
  status(m) {
    const text = m.text || '';
    if (text !== ui.status) { ui.status = text; ui.statusT0 = Date.now(); }
    renderStatus();
  },
  done() { ui.status = ''; setState('idle'); },
  image(m) { leaveSaver(); ui.image = m.url; showImage(m.url, m.caption); layout(); },
  card(m) { leaveSaver(); ui.card = [m.title, m.text]; renderCard(m.title, m.text); layout(); },
  camera(m) {
    ui.camera = !!m.on;
    $('#media').classList.toggle('live', ui.camera);
    if (ui.camera) { ui.card = null; $('#media figcaption').textContent = 'Live camera. Ask me what I see.'; }
    else { ui.image = null; $('#pic').removeAttribute('src'); }
    layout();
  },
  frame(m) { if (ui.camera) showImage(m.url, '', true); },
  camera_caption(m) {
    $('#media').classList.toggle('looking', !!m.looking);
    $('#media figcaption').textContent = m.looking ? 'Looking…' : 'Live camera. Ask me what I see.';
  },
  model3d(m) {
    ui.model = !!m.on;
    if (ui.model) { ui.image = null; ui.card = null; }
    layout();
  },
  model_state(m) { viewer.setState(m).catch(e => console.error('3d', e)); },
  model_orbit(m) { viewer.orbit(m.azimuth, m.elevation); },
  model_zoom(m) { viewer.zoom(m.factor); },
  saver(m) {
    const theme = m.theme || {};
    ui.saverStyle = theme.style || 'stars';
    if (!ui.saver) { ui.saver = true; ui.mouse0 = null; ui.saverSince = Date.now(); }
    document.documentElement.style.setProperty('--accent', theme.accent || '#ffc20e');
    sky.setStyle(ui.saverStyle === 'orb' ? 'orb' : ui.saverStyle, theme.accent, theme.dim);
    sky.setColor(ui.saverStyle === 'orb' ? (theme.accent || '#ffc20e') : '#ffc20e');
    $('#saver').hidden = false;
    $('#saver').classList.toggle('dim', !!theme.dim);
    renderSaver($('#saver'), m);
    setState('idle'); layout();
  },
};

function leaveSaver() {
  if (!ui.saver) return;
  ui.saver = false;
  $('#saver').hidden = true;
  sky.setStyle('gotham'); sky.setColor('#ffc20e');
  layout();
}

window.batsy = {
  cmd(m) {
    try { (handlers[m.cmd] || (() => {}))(m); } catch (e) { console.error('cmd ' + m.cmd + ': ' + e); }
    kick();
  },
  viewer,
};

// --- input: Esc / click closes; any input ends the screensaver --------------------------------------------
addEventListener('keydown', e => {
  if (viewerOnly) return;
  if (ui.saver) return send({event: 'saver_exit'});
  if (['Escape', ' ', 'q', 'Q'].includes(e.key)) send({event: 'dismiss'});
});
addEventListener('pointerdown', e => {
  if (viewerOnly) return;
  if (ui.saver) return send({event: 'saver_exit'});
  if (ui.model && $('#viewer').contains(e.target)) return;  // orbiting the model, not closing
  send({event: 'dismiss'});
});
addEventListener('pointermove', e => {
  if (!ui.saver || Date.now() - ui.saverSince < 1500) return;  // ignore the jitter as it appears
  if (!ui.mouse0) ui.mouse0 = [e.clientX, e.clientY];
  else if (Math.abs(e.clientX - ui.mouse0[0]) + Math.abs(e.clientY - ui.mouse0[1]) > 12) send({event: 'saver_exit'});
});
addEventListener('resize', layout);

// --- one loop for everything: full rate while something moves, a gentle 30 fps on the screensaver, ---------
// --- and nothing at all while the window is hidden ----------------------------------------------------------
let last = performance.now(), running = false, lastSecond = 0;
function loop(now) {
  const dt = Math.min(0.1, (now - last) / 1000);
  const cap = ui.saver ? 1 / 30 : 0;
  if (dt < cap - 0.002) { requestAnimationFrame(loop); return; }
  last = now;
  if (ui.shown || viewerOnly) {
    if (!viewerOnly) followSignal(sky.frame(dt, now));
    viewer.frame(dt, now);
    if (now - lastSecond > 1000) { lastSecond = now; renderStatus(); if (ui.saver) tickClock($('#saver')); }
    requestAnimationFrame(loop);
  } else {
    running = false;
  }
}
function kick() {
  if (running) return;
  running = true; last = performance.now();
  requestAnimationFrame(loop);
}

if (viewerOnly) {
  body.classList.add('on', 'viewer-only', 'media', 'model');
  document.querySelector('canvas').hidden = true;
  ui.model = true; viewer.show(true);
}
layout();
kick();
send({event: 'ready'});
