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
  state: 'idle', status: '', statusT0: 0, partial: '', image: null, card: null,
  camera: false, model: false, saver: false, shown: false, lastInput: 0, mouse0: null,
  turns: [], turnOpen: false,  // the conversation: [{you, batsy}], oldest first; the last one is happening now
};
const MAX_TURNS = 4;
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

// --- text: one block per exchange (what you said, then Batsy's reply), newest at the bottom ---------------
function newTurn(you) {
  ui.turns.push({you, batsy: ''});
  ui.turns = ui.turns.slice(-MAX_TURNS);
  ui.turnOpen = true;
}

function para(cls, text) {
  const p = document.createElement('p');
  p.className = cls;
  p.textContent = text;
  return p;
}

function renderText() {
  const thread = $('#thread');
  thread.replaceChildren();
  ui.turns.forEach((t, i) => {
    const el = document.createElement('div');
    el.className = 'turn' + (i < ui.turns.length - 1 ? ' old' : '');
    if (t.you) el.appendChild(para('you', `“${t.you}”`));
    if (t.batsy) el.appendChild(para('batsy', t.batsy));
    thread.appendChild(el);
  });
  if (ui.partial) {  // your words as you speak them, under everything said so far
    const el = document.createElement('div');
    el.className = 'turn';
    el.appendChild(para('you live', ui.partial));
    thread.appendChild(el);
  }
  thread.classList.toggle('clipped', thread.scrollHeight > thread.clientHeight + 2);
  // subtitles under media: the latest sentence or two of the latest reply
  const last = ui.turns[ui.turns.length - 1] || {you: '', batsy: ''};
  const sentences = last.batsy.match(/[^.!?]+[.!?]*\s*/g) || [];
  let sub = '';
  for (let i = sentences.length - 1; i >= 0 && (sub.length + sentences[i].length) < 220; i--) sub = sentences[i] + sub;
  $('#subtitle').textContent = (sub || last.batsy.slice(-220)).trim() || (hasMedia() ? (ui.partial || last.you) : '');
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
  appear() { ui.shown = true; body.classList.add('on'); sky.target.fade = 1; kick(); openFromBat(); },
  collapse() { closeIntoBat(); },
  origin(m) {
    ui.origin = {x: +m.x, y: +m.y};
    if (m.logo) document.documentElement.style.setProperty('--logo', `url("${m.logo}")`);
  },
  hidden() {
    for (const el of [$('#world'), $('#flybat')]) el.getAnimations().forEach(a => a.cancel());
    ui.shown = false; ui.saver = false; body.classList.remove('on'); sky.target.fade = 0;
    sky.u.uFade.value = 0; $('#saver').hidden = true; setState('idle'); layout();
  },
  listen(m) {
    leaveSaver();
    if (m.fresh) {
      Object.assign(ui, {turns: [], turnOpen: false, partial: '', card: null, status: ''});
      if (!ui.camera) ui.image = null;
    }
    setState('listen'); layout();
  },
  level(m) { sky.target.level = Math.max(0, Math.min(1, +m.v || 0)); },
  partial(m) { ui.partial = m.text || ''; renderText(); },
  heard(m) { ui.partial = ''; newTurn(m.text || ''); renderText(); },
  think() { leaveSaver(); setState('think'); },
  say(m) {
    clearTimeout(ui.quietTimer);
    leaveSaver(); ui.status = '';
    if (!ui.turnOpen || !ui.turns.length) newTurn('');  // Batsy speaking up on its own (a reminder, a question)
    const t = ui.turns[ui.turns.length - 1];
    t.batsy = (t.batsy + ' ' + (m.text || '')).trim();
    setState('speak'); renderText();
  },
  status(m) {
    const text = m.text || '';
    if (text !== ui.status) { ui.status = text; ui.statusT0 = Date.now(); }
    renderStatus();
  },
  done() { ui.status = ''; ui.turnOpen = false; setState('idle'); },
  quiet() {  // the voice finished its last sentence; wait a moment in case the next one is still on its way
    clearTimeout(ui.quietTimer);
    ui.quietTimer = setTimeout(() => { if (ui.state === 'speak' && !ui.status) setState('idle'); }, 600);
  },
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

// --- opening out of the corner bat, and folding back into it ----------------------------------------------
// The standby bat (batsy/dock.py) sits at ui.origin. Opening: a circle of night grows from it while the bat flies
// to the voice line and dissolves into it. Closing is the same played backwards, ending exactly where the dock's
// bat reappears, so the hand-over between the two windows is invisible.
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)');
function origin() { return ui.origin || {x: innerWidth - 37, y: 94}; }
function lineCentre() {
  const cs = getComputedStyle(document.documentElement);
  return {x: parseFloat(cs.getPropertyValue('--sx')) || innerWidth / 2,
          y: parseFloat(cs.getPropertyValue('--sy')) || innerHeight * 0.34,
          r: parseFloat(cs.getPropertyValue('--sr')) || 120};
}
function flight() {
  const o = origin(), c = lineCentre();
  const at = (p, s) => `translate(${p.x - 23}px, ${p.y - 10.5}px) scale(${s})`;
  const big = Math.max(3, c.r * 2.2 / 46);  // the bat grows to about the voice line's size
  const radius = Math.hypot(Math.max(o.x, innerWidth - o.x), Math.max(o.y, innerHeight - o.y)) + 40;
  const circle = r => `circle(${r}px at ${o.x}px ${o.y}px)`;
  return {o, c, at, big, circle, radius};
}
function openFromBat() {
  const world = $('#world'), bat = $('#flybat');
  for (const el of [world, bat]) el.getAnimations().forEach(a => a.cancel());
  if (reduceMotion.matches) return;
  const f = flight();
  world.animate([{clipPath: f.circle(0)}, {clipPath: f.circle(f.radius)}],
                {duration: 640, easing: 'cubic-bezier(.7, 0, .25, 1)'});
  bat.animate([{transform: f.at(f.o, 1), opacity: 1},
               {transform: f.at(f.c, f.big), opacity: 1, offset: .62},
               {transform: f.at(f.c, f.big * 1.3), opacity: 0}],
              {duration: 900, easing: 'cubic-bezier(.6, 0, .2, 1)'});
}
function closeIntoBat() {
  const world = $('#world'), bat = $('#flybat');
  if (!ui.shown || reduceMotion.matches) { send({event: 'collapsed'}); return; }
  for (const el of [world, bat]) el.getAnimations().forEach(a => a.cancel());
  const f = flight();
  bat.animate([{transform: f.at(f.c, f.big * 1.3), opacity: 0},
               {transform: f.at(f.c, f.big), opacity: 1, offset: .28},
               {transform: f.at(f.o, 1), opacity: 1}],
              {duration: 720, easing: 'cubic-bezier(.6, 0, .2, 1)', fill: 'forwards'});
  const shrink = world.animate([{clipPath: f.circle(f.radius)}, {clipPath: f.circle(0)}],
                               {duration: 600, delay: 140, easing: 'cubic-bezier(.7, 0, .3, 1)', fill: 'forwards'});
  shrink.onfinish = () => send({event: 'collapsed'});
}

// --- input: Esc / click closes; any input ends the screensaver --------------------------------------------
addEventListener('keydown', e => {
  if (viewerOnly) return;
  if (ui.saver) return send({event: 'saver_exit'});
  if (['~', '`'].includes(e.key)) return send({event: 'stop'});  // end the conversation: stop talking and listening
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
