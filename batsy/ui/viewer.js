// The 3D model view. Python (batsy/viewer3d.py) owns the geometry and the maths in millimetres; this draws it:
// Z-up CAD-style camera, dimension rulers, floor grid, cross-sections, exploded parts and click-to-measure.
// Renders only when something changes, so an idle model costs nothing.
import * as THREE from 'three';
import {OrbitControls} from './vendor/OrbitControls.js';

const SIGNAL = new THREE.Color('#ffc20e');
const VIEWS = {  // camera direction from the target, Z up
  iso: [1, -1.15, 0.85], top: [0, -0.0001, 1], bottom: [0, -0.0001, -1], front: [0, -1, 0], back: [0, 1, 0],
  right: [1, 0, 0], left: [-1, 0, 0],
};

async function loadMesh(url) {
  const buf = await (await fetch(url)).arrayBuffer();
  const head = new DataView(buf, 0, 16);
  if (String.fromCharCode(...new Uint8Array(buf, 0, 4)) !== 'BMSH') throw new Error('bad mesh');
  const nv = head.getUint32(4, true), nf = head.getUint32(8, true), hasN = head.getUint32(12, true) & 1;
  let off = 16;
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(buf, off, nv * 3), 3)); off += nv * 12;
  if (hasN) { g.setAttribute('normal', new THREE.BufferAttribute(new Float32Array(buf, off, nv * 3), 3)); off += nv * 12; }
  g.setIndex(new THREE.BufferAttribute(new Uint32Array(buf, off, nf * 3), 1));
  if (!hasN) g.computeVertexNormals();
  g.computeBoundingSphere();
  return g;
}

export class Viewer {
  constructor(root, send) {
    this.root = root; this.send = send;
    this.canvas = root.querySelector('canvas');
    this.labels = root.querySelector('.labels');
    this.renderer = new THREE.WebGLRenderer({canvas: this.canvas, antialias: true, powerPreference: 'high-performance'});
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 1.5));
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.localClippingEnabled = true;
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(35, 1, 0.1, 100000);
    this.camera.up.set(0, 0, 1);
    this.camera.position.set(100, -115, 85);
    this.scene.add(this.camera);
    this.scene.add(new THREE.HemisphereLight(0xdfe6f2, 0x1a1d24, 1.3));
    const key = new THREE.DirectionalLight(0xfff4dc, 2.4);  // rides with the camera: the model is always lit
    key.position.set(2, 3, 1); this.camera.add(key); this.camera.add(key.target); key.target.position.set(0, 0, -5);
    const rim = new THREE.DirectionalLight(0x9fb4ff, 0.9); rim.position.set(-3, 4, 2); this.scene.add(rim);
    this.controls = new OrbitControls(this.camera, this.canvas);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.12;
    this.controls.addEventListener('change', () => (this.dirty = true));
    this.models = new THREE.Group(); this.scene.add(this.models);
    this.extras = new THREE.Group(); this.scene.add(this.extras);
    this.marks = new THREE.Group(); this.scene.add(this.marks);
    this.geoms = new Map();       // url -> geometry (kept while still used)
    this.labelPts = [];           // [{el, pos}]
    this.state = null;
    this.radius = 50; this.center = new THREE.Vector3();
    this.tween = null; this.spin = false; this.visible = false; this.dirty = true;
    this.measure = {on: false, pts: []};
    this.clip = null;
    new ResizeObserver(() => this.resize()).observe(root);
    this.resize();
    let down = null;
    this.canvas.addEventListener('pointerdown', e => (down = [e.clientX, e.clientY]));
    this.canvas.addEventListener('pointerup', e => {
      if (down && Math.hypot(e.clientX - down[0], e.clientY - down[1]) < 5 && this.measure.on) this.pick(e);
      down = null;
    });
  }

  resize() {
    const w = this.root.clientWidth, h = this.root.clientHeight;
    if (!w || !h) return;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h; this.camera.updateProjectionMatrix();
    this.dirty = true;
  }

  show(on) { this.visible = on; this.root.hidden = !on; if (on) { this.resize(); this.dirty = true; } }

  // --- state from Python -------------------------------------------------------------------------------------
  async setState(s) {
    const seq = (this.seq = (this.seq || 0) + 1);
    const geoms = await Promise.all(s.models.map(m => this.geoms.get(m.url) || loadMesh(m.url)));
    if (seq !== this.seq) return;  // a newer state arrived while loading
    const used = new Set(s.models.map(m => m.url));
    s.models.forEach((m, i) => this.geoms.set(m.url, geoms[i]));
    for (const [url, g] of this.geoms) if (!used.has(url)) { g.dispose(); this.geoms.delete(url); }
    this.state = s;
    this.clip = s.section ? [new THREE.Plane(new THREE.Vector3(...s.section.normal), s.section.constant)] : null;
    this.clear(this.models);
    s.models.forEach((m, i) => {
      const g = geoms[i];
      const xray = s.opacity < 1;
      const mat = new THREE.MeshStandardMaterial({
        color: m.color, metalness: 0.12, roughness: 0.52, wireframe: s.style === 'wireframe',
        transparent: xray, opacity: s.opacity, depthWrite: !xray, clippingPlanes: this.clip,
        polygonOffset: s.style === 'edges', polygonOffsetFactor: 1, polygonOffsetUnits: 1,
      });
      const mesh = new THREE.Mesh(g, mat);
      mesh.matrixAutoUpdate = false;
      mesh.matrix.fromArray(m.matrix);
      this.models.add(mesh);
      if (this.clip) {  // the cut face: back faces seen through the cut, painted signal yellow
        const cap = new THREE.Mesh(g, new THREE.MeshBasicMaterial({color: SIGNAL, side: THREE.BackSide, clippingPlanes: this.clip}));
        cap.matrixAutoUpdate = false; cap.matrix.copy(mesh.matrix); this.models.add(cap);
      }
      if (s.style === 'edges' && g.index.count < 1.2e6) {
        const e = new THREE.LineSegments(new THREE.EdgesGeometry(g, 28), new THREE.LineBasicMaterial({color: 0x0b0c10, clippingPlanes: this.clip}));
        e.matrixAutoUpdate = false; e.matrix.copy(mesh.matrix); e.userData.own = true; this.models.add(e);
      }
    });
    this.root.querySelector('.hud .lines').textContent = s.hud.join('\n');
    this.root.querySelector('.hud .message').textContent = s.message || '';
    this.root.classList.toggle('measuring', !!s.measure);
    if (this.measure.on !== !!s.measure) { this.measure.pts = []; this.clear(this.marks); this.dropLabels('measure'); }
    this.measure.on = !!s.measure;
    this.spin = !!s.spin;
    this.drawExtras(s);
    if (s.reset_camera) this.fit(s.view || 'iso', true);
    else if (s.view) this.fit(s.view, false);
    this.dirty = true;
  }

  clear(group) {
    for (const o of [...group.children]) {
      group.remove(o);
      if (o.material) o.material.dispose();
      if (o.geometry && (o.userData.own || group !== this.models)) o.geometry.dispose();
    }
  }

  dropLabels(kind) {
    this.labelPts = this.labelPts.filter(l => { if (!kind || l.kind === kind) { l.el.remove(); return false; } return true; });
  }

  label(text, pos, kind = 'dim') {
    const el = document.createElement('span');
    el.textContent = text;
    if (kind === 'measure') el.className = 'measure';
    this.labels.appendChild(el);
    this.labelPts.push({el, pos: pos.clone(), kind});
  }

  drawExtras(s) {
    this.clear(this.extras);
    this.dropLabels('dim');
    if (!s.bounds) return;
    const lo = new THREE.Vector3(...s.bounds[0]), hi = new THREE.Vector3(...s.bounds[1]);
    const size = hi.clone().sub(lo);
    this.center.copy(lo).add(hi).multiplyScalar(0.5);
    this.radius = Math.max(size.length() / 2, 1);
    const line = (pts, color, opacity) => {
      const g = new THREE.BufferGeometry().setFromPoints(pts);
      return new THREE.LineSegments(g, new THREE.LineBasicMaterial({color, transparent: opacity < 1, opacity}));
    };
    if (s.grid) {
      const span = Math.max(size.x, size.y) * 1.8 + 1, n = 20, step = span / n, z = lo.z - 0.01;
      const cx = this.center.x - span / 2, cy = this.center.y - span / 2, pts = [];
      for (let i = 0; i <= n; i++) {
        pts.push(new THREE.Vector3(cx + i * step, cy, z), new THREE.Vector3(cx + i * step, cy + span, z));
        pts.push(new THREE.Vector3(cx, cy + i * step, z), new THREE.Vector3(cx + span, cy + i * step, z));
      }
      this.extras.add(line(pts, 0x3a4150, 0.55));
    }
    if (s.dims) {
      const c = [[lo.x, lo.y, lo.z], [hi.x, lo.y, lo.z], [hi.x, hi.y, lo.z], [lo.x, hi.y, lo.z],
                 [lo.x, lo.y, hi.z], [hi.x, lo.y, hi.z], [hi.x, hi.y, hi.z], [lo.x, hi.y, hi.z]].map(p => new THREE.Vector3(...p));
      const box = [];
      for (const [a, b] of [[0, 1], [1, 2], [2, 3], [3, 0], [4, 5], [5, 6], [6, 7], [7, 4], [0, 4], [1, 5], [2, 6], [3, 7]]) box.push(c[a], c[b]);
      this.extras.add(line(box, SIGNAL, 0.22));
      const off = Math.max(size.x, size.y, size.z) * 0.08 + 0.5, t = off * 0.35, pts = [];
      const rulers = [
        [new THREE.Vector3(lo.x, lo.y - off, lo.z), new THREE.Vector3(hi.x, lo.y - off, lo.z), new THREE.Vector3(0, t, 0), `X ${size.x.toFixed(1)} mm`],
        [new THREE.Vector3(hi.x + off, lo.y, lo.z), new THREE.Vector3(hi.x + off, hi.y, lo.z), new THREE.Vector3(t, 0, 0), `Y ${size.y.toFixed(1)} mm`],
        [new THREE.Vector3(hi.x + off, lo.y - off, lo.z), new THREE.Vector3(hi.x + off, lo.y - off, hi.z), new THREE.Vector3(t, 0, 0), `Z ${size.z.toFixed(1)} mm`],
      ];
      for (const [a, b, tick, text] of rulers) {
        if (a.distanceTo(b) < 1e-6) continue;
        pts.push(a, b, a.clone().sub(tick), a.clone().add(tick), b.clone().sub(tick), b.clone().add(tick));
        this.label(text, a.clone().add(b).multiplyScalar(0.5));
      }
      this.extras.add(line(pts, SIGNAL, 0.9));
    }
  }

  // --- camera ------------------------------------------------------------------------------------------------
  fit(view, snap) {
    const dir = new THREE.Vector3(...(VIEWS[view] || VIEWS.iso)).normalize();
    const dist = this.radius / Math.sin(THREE.MathUtils.degToRad(this.camera.fov / 2)) * 1.12;
    this.flyTo(this.center.clone().add(dir.multiplyScalar(dist)), this.center.clone(), snap ? 0 : 450);
  }

  flyTo(pos, target, ms) {
    this.camera.far = Math.max(100000, this.radius * 40); this.camera.near = Math.max(0.01, this.radius / 500);
    this.camera.updateProjectionMatrix();
    if (!ms) { this.camera.position.copy(pos); this.controls.target.copy(target); this.controls.update(); this.dirty = true; return; }
    this.tween = {p0: this.camera.position.clone(), t0: this.controls.target.clone(), p1: pos, t1: target, start: performance.now(), ms};
  }

  orbit(az, el) {
    const off = this.camera.position.clone().sub(this.controls.target);
    off.applyAxisAngle(new THREE.Vector3(0, 0, 1), THREE.MathUtils.degToRad(az));
    const axis = new THREE.Vector3().crossVectors(off, new THREE.Vector3(0, 0, 1)).normalize();
    if (axis.lengthSq() > 0.5) off.applyAxisAngle(axis, -THREE.MathUtils.degToRad(el));
    this.flyTo(this.controls.target.clone().add(off), this.controls.target.clone(), 450);
  }

  zoom(f) {
    const off = this.camera.position.clone().sub(this.controls.target).divideScalar(f || 1.5);
    this.flyTo(this.controls.target.clone().add(off), this.controls.target.clone(), 350);
  }

  // --- measuring ---------------------------------------------------------------------------------------------
  pick(e) {
    const r = this.canvas.getBoundingClientRect();
    const ray = new THREE.Raycaster();
    ray.setFromCamera(new THREE.Vector2((e.clientX - r.left) / r.width * 2 - 1, -(e.clientY - r.top) / r.height * 2 + 1), this.camera);
    const hit = ray.intersectObjects(this.models.children.filter(o => o.isMesh && o.material.side !== THREE.BackSide), false)[0];
    if (!hit) return;
    if (this.measure.pts.length >= 2) { this.measure.pts = []; this.clear(this.marks); this.dropLabels('measure'); }
    this.measure.pts.push(hit.point.clone());
    const dot = new THREE.Mesh(new THREE.SphereGeometry(this.radius * 0.012, 16, 12), new THREE.MeshBasicMaterial({color: SIGNAL, depthTest: false}));
    dot.position.copy(hit.point); dot.renderOrder = 10; this.marks.add(dot);
    if (this.measure.pts.length === 2) {
      const [a, b] = this.measure.pts, mm = a.distanceTo(b);
      const ln = new THREE.Line(new THREE.BufferGeometry().setFromPoints([a, b]), new THREE.LineBasicMaterial({color: SIGNAL, depthTest: false}));
      ln.renderOrder = 10; this.marks.add(ln);
      this.label(`${mm.toFixed(2)} mm`, a.clone().add(b).multiplyScalar(0.5), 'measure');
      this.send({event: 'measured', mm: Math.round(mm * 100) / 100});
    }
    this.dirty = true;
  }

  snapshot() {
    this.renderer.render(this.scene, this.camera);
    return this.canvas.toDataURL('image/png');
  }

  // --- loop (driven by app.js) -------------------------------------------------------------------------------
  frame(dt, now) {
    if (!this.visible) return;
    if (this.tween) {
      const tw = this.tween, k = Math.min(1, (now - tw.start) / tw.ms), e = 1 - Math.pow(1 - k, 3);
      this.camera.position.lerpVectors(tw.p0, tw.p1, e);
      this.controls.target.lerpVectors(tw.t0, tw.t1, e);
      if (k >= 1) this.tween = null;
      this.dirty = true;
    }
    if (this.spin && !this.tween) {
      const off = this.camera.position.clone().sub(this.controls.target);
      off.applyAxisAngle(new THREE.Vector3(0, 0, 1), dt * 0.6);
      this.camera.position.copy(this.controls.target).add(off);
      this.dirty = true;
    }
    if (this.controls.update()) this.dirty = true;
    if (!this.dirty) return;
    this.dirty = false;
    this.renderer.render(this.scene, this.camera);
    const w = this.canvas.clientWidth, h = this.canvas.clientHeight, v = new THREE.Vector3();
    for (const l of this.labelPts) {
      v.copy(l.pos).project(this.camera);
      const off = v.z > 1 || Math.abs(v.x) > 1.2 || Math.abs(v.y) > 1.2;
      l.el.style.display = off ? 'none' : '';
      l.el.style.left = `${(v.x * 0.5 + 0.5) * w}px`;
      l.el.style.top = `${(-v.y * 0.5 + 0.5) * h}px`;
    }
  }

  busy() { return this.visible && (this.dirty || this.tween || this.spin); }
}
