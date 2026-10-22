// The living background: one glowing voice line and a field of drifting particles.
// The line ripples with your voice while listening, sweeps while thinking and pulses while speaking;
// particles drift on their own and get pulled into the line whenever Batsy is active.
// Everything moves on the GPU (two draw calls), so it costs the CPU almost nothing.
import * as THREE from 'three';

// y of the voice line at horizontal position t (-1..1 along the line), shared by both shaders.
const LINE = `
uniform float uTime, uLevel, uActive, uThink, uListen, uSpeak, uSigOn;
uniform vec3 uSig;  // centre x, y (0..1, y down), size (fraction of height)
float speakEnv() { return 0.55 + 0.45 * sin(uTime * 7.3) * sin(uTime * 2.3 + 1.0); }
float lineAmp() {
  return uSig.z * (0.035 + 0.5 * uLevel * uListen + 0.22 * speakEnv() * uSpeak + 0.12 * uThink + 0.03 * uActive);
}
float lineY(float t, float strand) {
  float e = pow(max(0.0, 1.0 - t * t), 2.0);                  // taper at both ends
  float packet = exp(-pow((t - sin(uTime * 1.7)) * 3.0, 2.0));  // thinking: a pulse that sweeps along
  float k = 1.0 + 0.6 * uThink + 0.5 * uSpeak;
  float w = sin(t * 9.0 + uTime * 3.1 * k + strand * 2.1) * 0.55
          + sin(t * 17.0 - uTime * 4.7 * k + strand * 1.3) * 0.28
          + sin(t * 4.0 + uTime * 1.3 + strand * 0.7) * 0.3;
  return lineAmp() * e * (w * (1.0 - 0.25 * strand) + packet * uThink * 1.4);
}`;

const BG_VERT = `void main() { gl_Position = vec4(position.xy, 0.0, 1.0); }`;
const BG_FRAG = `
precision highp float;
uniform vec2 uRes, uCss;
uniform float uFade, uStyle, uDim;
uniform vec3 uColor, uAccent;
${LINE}
float hash(vec2 p) { p = fract(p * vec2(123.34, 456.21)); p += dot(p, p + 45.32); return fract(p.x * p.y); }

void main() {
  vec2 uv = gl_FragCoord.xy / uRes;          // y up
  float aspect = uRes.x / uRes.y;
  vec2 q = vec2(uv.x * aspect, uv.y);        // screen heights
  vec3 col = mix(vec3(0.014, 0.016, 0.022), vec3(0.03, 0.035, 0.05), pow(uv.y, 1.3));
  int style = int(uStyle + 0.5);            // 0 line, 1 aurora, 2 stars, 3 waves, 4 orb (line), 5 minimal

  if (style == 1) {
    for (int i = 0; i < 3; i++) {
      float fi = float(i);
      float y0 = 0.58 + 0.1 * fi + 0.06 * sin(q.x * 1.4 + uTime * 0.05 * (fi + 1.0)) + 0.04 * sin(q.x * 3.1 - uTime * 0.07);
      col += mix(uAccent, uAccent.gbr, fi * 0.35) * exp(-pow((uv.y - y0) / (0.05 + 0.02 * fi), 2.0)) * 0.18;
    }
  }
  if (style == 2) {
    vec2 g = (q + vec2(uTime * 0.004, 0.0)) * 70.0, id = floor(g), f = fract(g) - 0.5;
    vec2 o = vec2(hash(id), hash(id + 3.1)) - 0.5;
    float h = hash(id + 7.7);
    col += vec3(0.85, 0.88, 1.0) * smoothstep(0.09, 0.0, length(f - o * 0.7)) * step(0.86, h) * (0.55 + 0.45 * sin(uTime * 1.3 + h * 40.0));
  }
  if (style == 3) {
    for (int k = 0; k < 5; k++) {
      float fk = float(k);
      float y = 0.3 - fk * 0.05 + 0.02 * sin(q.x * 6.0 + uTime * (0.35 + fk * 0.1) + fk);
      col += uAccent * smoothstep(0.003, 0.0, abs(uv.y - y)) * (0.3 - fk * 0.05);
    }
  }

  if (uSigOn > 0.001) {
    vec2 s = vec2(uSig.x * aspect, 1.0 - uSig.y);
    float L = uSig.z * 3.2;                  // half length of the line
    float t = (q.x - s.x) / L;
    if (abs(t) < 1.05) {
      float px = 1.0 / uCss.y;               // one css pixel, in screen heights
      for (int i = 0; i < 3; i++) {
        float fi = float(i);
        float y = s.y + lineY(t, fi);
        float slope = (lineY(t + 0.002, fi) - lineY(t - 0.002, fi)) / (0.004 * L);
        float d = abs(q.y - y) / sqrt(1.0 + slope * slope) / px;   // distance in css pixels
        float fadeEnds = smoothstep(1.0, 0.75, abs(t));
        float core = smoothstep(1.4 + 0.8 * uLevel * uListen, 0.0, d) * (i == 0 ? 1.0 : 0.35);
        float glow = exp(-d / (7.0 + 22.0 * uLevel * uListen + 6.0 * uActive)) * (i == 0 ? 0.42 : 0.12);
        col += (mix(uColor, vec3(1.0, 0.97, 0.9), core * 0.6) * core + uColor * glow) * fadeEnds * uSigOn * (0.55 + 0.45 * uActive);
      }
    }
  }

  col *= 1.0 - 0.35 * pow(length(uv - 0.5) * 1.25, 2.0);
  gl_FragColor = vec4(col * uFade * uDim, 1.0);
}`;

const P_VERT = `
attribute vec4 seed;
uniform float uFade, uDim, uPx;
uniform vec2 uRes;
${LINE}
varying float vA;
varying float vHot;
void main() {
  float aspect = uRes.x / uRes.y;
  float sp = 0.6 + seed.z;
  // free drift: a slow wandering flow, wrapping around the screen
  vec2 p = vec2(seed.x * aspect, seed.y);
  p += vec2(sin(uTime * 0.05 * sp + seed.w * 6.28 + p.y * 2.3), cos(uTime * 0.043 * sp + seed.z * 6.28 + p.x * 1.9)) * 0.07;
  p.x = mod(p.x + uTime * 0.008 * sp, aspect);
  // pulled into the line while Batsy is active; streaming along it while thinking
  vec2 s = vec2(uSig.x * aspect, 1.0 - uSig.y);
  float L = uSig.z * 3.2;
  float lx = s.x + (fract(seed.x + uTime * 0.06 * sp * uThink) * 2.0 - 1.0) * L;
  float t = (lx - s.x) / L;
  float spread = (seed.w - 0.5) * (uSig.z * 0.55 + lineAmp() * 1.6);
  vec2 onLine = vec2(lx, s.y + lineY(t, 0.0) + spread * (0.4 + 0.6 * abs(seed.y - 0.5) * 2.0));
  float pull = uSigOn * uActive * smoothstep(0.35, 0.95, seed.z) * (0.55 + 0.35 * uListen * uLevel + 0.3 * uThink);
  p = mix(p, onLine, clamp(pull, 0.0, 0.92));
  vHot = pull;
  float tw = 0.55 + 0.45 * sin(uTime * (0.8 + seed.w * 2.0) + seed.x * 40.0);
  vA = (0.18 + 0.5 * seed.z) * tw * uFade * uDim;
  gl_PointSize = (1.0 + 2.4 * seed.z * seed.z) * uPx * (1.0 + 0.8 * uLevel * uListen * pull);
  gl_Position = vec4(p.x / aspect * 2.0 - 1.0, p.y * 2.0 - 1.0, 0.0, 1.0);
}`;

const P_FRAG = `
precision highp float;
uniform vec3 uColor;
varying float vA;
varying float vHot;
void main() {
  float d = length(gl_PointCoord - 0.5);
  float a = smoothstep(0.5, 0.0, d) * vA;
  vec3 c = mix(vec3(0.72, 0.76, 0.84), uColor, clamp(vHot * 1.2, 0.0, 1.0));
  gl_FragColor = vec4(c * a, a);
}`;

const STYLES = {line: 0, gotham: 0, aurora: 1, stars: 2, waves: 3, orb: 4, minimal: 5};

export class Sky {
  constructor(canvas) {
    this.renderer = new THREE.WebGLRenderer({canvas, antialias: false, alpha: false, powerPreference: 'low-power'});
    this.scale = Math.min(1, 1.25 / (devicePixelRatio || 1));  // thin line needs ~full resolution; drops if slow
    this.u = {
      uRes: {value: new THREE.Vector2(1, 1)}, uCss: {value: new THREE.Vector2(1, 1)}, uTime: {value: 0},
      uFade: {value: 0}, uLevel: {value: 0}, uActive: {value: 0}, uThink: {value: 0}, uListen: {value: 0},
      uSpeak: {value: 0}, uStyle: {value: 0}, uSigOn: {value: 1}, uDim: {value: 1}, uPx: {value: 1},
      uSig: {value: new THREE.Vector3(0.5, 0.31, 0.125)}, uColor: {value: new THREE.Color('#ffc20e')},
      uAccent: {value: new THREE.Color('#ffc20e')},
    };
    this.scene = new THREE.Scene();
    const bg = new THREE.Mesh(new THREE.PlaneGeometry(2, 2),
      new THREE.ShaderMaterial({vertexShader: BG_VERT, fragmentShader: BG_FRAG, uniforms: this.u, depthTest: false, depthWrite: false}));
    bg.frustumCulled = false;
    this.scene.add(bg);
    const n = Math.round(Math.min(2200, Math.max(900, innerWidth * innerHeight / 1100)));
    const seeds = new Float32Array(n * 4);
    for (let i = 0; i < seeds.length; i++) seeds[i] = Math.random();
    const g = new THREE.BufferGeometry();
    g.setAttribute('seed', new THREE.BufferAttribute(seeds, 4));
    g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(n * 3), 3));
    this.points = new THREE.Points(g, new THREE.ShaderMaterial({
      vertexShader: P_VERT, fragmentShader: P_FRAG, uniforms: this.u, transparent: true,
      blending: THREE.AdditiveBlending, depthTest: false, depthWrite: false,
    }));
    this.points.frustumCulled = false;
    this.scene.add(this.points);
    this.cam = new THREE.Camera();
    this.target = {level: 0, active: 0, think: 0, listen: 0, speak: 0, fade: 0, sigOn: 1, x: 0.5, y: 0.31, r: 0.125};
    this.slow = 0;
    this.resize();
    addEventListener('resize', () => this.resize());
  }

  resize() {
    const w = innerWidth, h = innerHeight, rw = Math.max(2, Math.round(w * this.scale)), rh = Math.max(2, Math.round(h * this.scale));
    this.renderer.setPixelRatio(1);
    this.renderer.setSize(rw, rh, false);
    this.u.uRes.value.set(rw, rh);
    this.u.uCss.value.set(w, h);
    this.u.uPx.value = this.scale * (devicePixelRatio || 1);
  }

  setStyle(name, accent, dim) {
    this.u.uStyle.value = STYLES[name] ?? 0;
    this.points.visible = name !== 'minimal';
    if (accent) this.u.uAccent.value.set(accent);
    this.u.uDim.value = dim ? 0.6 : 1;
  }

  setColor(hex) { this.u.uColor.value.set(hex); }

  frame(dt, now) {
    const t = this.target, u = this.u, k = 1 - Math.exp(-dt * 7), kl = 1 - Math.exp(-dt * 16);
    u.uLevel.value += (t.level - u.uLevel.value) * kl;
    for (const [name, key] of [['active', 'uActive'], ['think', 'uThink'], ['listen', 'uListen'], ['speak', 'uSpeak'], ['sigOn', 'uSigOn']])
      u[key].value += (t[name] - u[key].value) * k;
    u.uFade.value += (t.fade - u.uFade.value) * (1 - Math.exp(-dt * 9));
    const s = u.uSig.value, km = 1 - Math.exp(-dt * 6);
    s.x += (t.x - s.x) * km; s.y += (t.y - s.y) * km; s.z += (t.r - s.z) * km;
    u.uTime.value = now / 1000;
    this.renderer.render(this.scene, this.cam);
    // adaptive quality: if frames get slow, draw fewer pixels
    if (dt > 0.03 && this.scale > 0.45) { if (++this.slow > 45) { this.scale *= 0.85; this.slow = 0; this.resize(); } }
    else this.slow = Math.max(0, this.slow - 1);
    return {x: s.x, y: s.y, r: s.z};
  }
}

// Fallback when WebGL isn't available (broken GPU driver, software mode): the same idea in 2D canvas.
class FlatSky {
  constructor(canvas) {
    this.canvas = canvas;
    this.g = canvas.getContext('2d');
    this.target = {level: 0, active: 0, think: 0, listen: 0, speak: 0, fade: 0, sigOn: 1, x: 0.5, y: 0.31, r: 0.125};
    this.s = {...this.target};
    this.color = '#ffc20e'; this.dim = 1; this.particles = true;
    this.u = {uFade: {value: 0}};
    this.dots = Array.from({length: 260}, () => [Math.random(), Math.random(), Math.random(), Math.random()]);
    this.resize();
    addEventListener('resize', () => this.resize());
  }
  resize() { this.canvas.width = Math.round(innerWidth / 2); this.canvas.height = Math.round(innerHeight / 2); }
  setStyle(name, accent, dim) { this.particles = name !== 'minimal'; this.dim = dim ? 0.6 : 1; }
  setColor(hex) { this.color = hex; }
  frame(dt, now) {
    const t = this.target, s = this.s, k = 1 - Math.exp(-dt * 7);
    for (const key of Object.keys(t)) s[key] += (t[key] - s[key]) * k;
    if (this.u.uFade.value === 0 && t.fade === 0) s.fade = 0;
    this.u.uFade.value = s.fade;
    const g = this.g, w = this.canvas.width, h = this.canvas.height, time = now / 1000;
    g.globalAlpha = 1; g.fillStyle = '#05060a'; g.fillRect(0, 0, w, h);
    const cx = s.x * w, cy = s.y * h, L = s.r * h * 3.2;
    const amp = s.r * h * (0.035 + 0.5 * s.level * s.listen + 0.15 * s.speak + 0.12 * s.think);
    const y = x => { const tt = (x - cx) / L, e = Math.max(0, 1 - tt * tt) ** 2;
      return cy + amp * e * (Math.sin(tt * 9 + time * 3.1) * 0.55 + Math.sin(tt * 17 - time * 4.7) * 0.28 + Math.sin(tt * 4 + time * 1.3) * 0.3); };
    if (this.particles) {
      g.fillStyle = '#b8c2d6';
      for (const [a, b, c, d] of this.dots) {
        g.globalAlpha = (0.15 + 0.45 * c) * s.fade * this.dim;
        const px = ((a + time * 0.004 * (0.6 + c)) % 1) * w + Math.sin(time * 0.05 + d * 6.28) * 20, py = b * h + Math.cos(time * 0.04 + c * 6.28) * 20;
        g.fillRect(px, py, 1 + c * 1.5, 1 + c * 1.5);
      }
    }
    if (s.sigOn > 0.02) {
      g.globalAlpha = s.fade * this.dim * s.sigOn * (0.55 + 0.45 * s.active);
      g.strokeStyle = this.color; g.lineWidth = 1.5; g.shadowColor = this.color; g.shadowBlur = 12;
      g.beginPath();
      for (let x = cx - L; x <= cx + L; x += 3) (x === cx - L ? g.moveTo : g.lineTo).call(g, x, y(x));
      g.stroke(); g.shadowBlur = 0;
    }
    return {x: s.x, y: s.y, r: s.r};
  }
}

export function makeSky(canvas) {
  try {
    return new Sky(canvas);
  } catch (e) {
    console.warn('WebGL unavailable, using the 2D background: ' + e.message);
    const fresh = canvas.cloneNode();  // a canvas that tried WebGL can't switch to 2D
    canvas.replaceWith(fresh);
    return new FlatSky(fresh);
  }
}
