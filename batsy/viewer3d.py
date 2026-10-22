"""Batsy's 3D viewer: exact geometry in trimesh, drawn by three.js (batsy/ui/viewer.js), driven over a local socket.

Normally it lives inside Batsy's overlay (one process, no extra window). Run on its own with
`python -m batsy.viewer3d [model]` it opens a normal window. Batsy's voice parser and Claude Code both send
JSON commands like {"cmd": "rotate", "axis": "z", "deg": 90} and get JSON replies (dimensions, volume...).
All the maths happens on the socket threads, so a big model never freezes the screen; the page only gets
geometry once per model plus small state updates. Click-to-measure events go out as JSON lines on stdout.
"""
import base64
import json
import logging
import os
import socket
import struct
import sys
import threading
import time
from pathlib import Path

import numpy as np
import trimesh

from batsy.config import SOCKET_PATH, WORKSPACE

log = logging.getLogger("batsy.3d")
SOCK = SOCKET_PATH.parent / f"batsy-3d-{os.getuid()}.sock"
OUT = WORKSPACE / "3d"
# Batman: bat-signal yellow first, then steel and gunmetal tones so several parts stay distinguishable.
PALETTE = ["#ffc20e", "#c9ced8", "#e8a317", "#8a93a6", "#fff1b8", "#5c6578", "#ffdd66", "#a7aebb"]
COLORS = {"red": "#ff5a5a", "green": "#5adf7a", "blue": "#5a9bff", "yellow": "#ffe15a", "orange": "#ffa64d",
          "purple": "#b07cff", "pink": "#ff7cc8", "white": "#f2f2f2", "black": "#222222", "grey": "#a0a0a0",
          "gray": "#a0a0a0", "silver": "#c8c8d0", "gold": "#e6c35a", "cyan": "#5ae6e6", "brown": "#a0704a"}
DENSITY = {"pla": 1.24, "petg": 1.27, "abs": 1.04, "asa": 1.07, "tpu": 1.21, "nylon": 1.14, "resin": 1.15,
           "aluminium": 2.70, "aluminum": 2.70, "steel": 7.85, "brass": 8.5, "wood": 0.7}
AXES = {"x": [1, 0, 0], "y": [0, 1, 0], "z": [0, 0, 1]}
SMOOTH_LIMIT = 1_500_000  # faces; above this, skip crease-aware normals (slow) and let the page compute them


def pack(mesh: trimesh.Trimesh) -> bytes:
    """Geometry for the page: 'BMSH', vertex count, face count, flags, float32 xyz, [float32 normals], uint32 faces."""
    shown = mesh
    if len(mesh.faces) < SMOOTH_LIMIT:
        try:
            shown = mesh.smooth_shaded  # splits vertices at sharp edges: CAD parts keep crisp corners
        except Exception:
            shown = mesh
    v = np.ascontiguousarray(shown.vertices, dtype="<f4")
    f = np.ascontiguousarray(shown.faces, dtype="<u4")
    normals = b""
    if shown is not mesh or len(mesh.faces) < SMOOTH_LIMIT:
        normals = np.ascontiguousarray(shown.vertex_normals, dtype="<f4").tobytes()
    return b"BMSH" + struct.pack("<III", len(v), len(f), 1 if normals else 0) + v.tobytes() + normals + f.tobytes()


def _rotation(axis: str, deg: float, center) -> np.ndarray:
    return trimesh.transformations.rotation_matrix(np.radians(deg), AXES[axis], center)


class Model:
    def __init__(self, name: str, path: str | None, mesh: trimesh.Trimesh, color: str):
        self.name, self.path, self.base, self.color = name, path, mesh, color
        self.matrix = np.eye(4)
        self.parts: list[trimesh.Trimesh] | None = None  # exploded view
        self.key = f"{abs(hash((name, path, time.time()))):x}"  # geometry id for the page's cache

    def mesh(self) -> trimesh.Trimesh:
        m = self.base.copy()
        m.apply_transform(self.matrix)
        return m


class Scene:
    """Models + view options. push(dict) sends state to the page, present(bool) puts the view on/off screen,
    snapshot() returns a PNG data URL of the current view."""

    def __init__(self, push, present, snapshot=None):
        from batsy import webui

        self.webui = webui
        self.push, self.present, self.snapshot = push, present, snapshot
        self.lock = threading.RLock()
        self.models: dict[str, Model] = {}
        self.dims_on, self.grid_on, self.style, self.opacity = True, True, "solid", 1.0
        self.section: dict | None = None
        self.spinning = self.measuring = False
        self.last_measure: float | None = None
        self.message = "Say: rotate it, top view, cross section, explode, measure"

    # --- page state -----------------------------------------------------------------------------------------
    def _bounds(self):
        meshes = [m.mesh() for m in self.models.values()]
        if not meshes:
            return None
        return np.min([x.bounds[0] for x in meshes], axis=0), np.max([x.bounds[1] for x in meshes], axis=0)

    def _hud(self) -> list[str]:
        lines = []
        for name, m in self.models.items():
            e = m.mesh().extents
            lines.append(f"{name}  {e[0]:.1f} × {e[1]:.1f} × {e[2]:.1f} mm  ·  {len(m.base.faces):,} triangles")
        if self.last_measure is not None:
            lines.append(f"Measured {self.last_measure:.2f} mm")
        return lines

    def sync(self, reset_camera: bool = False, view: str | None = None) -> None:
        """Send the whole scene (geometry by URL, cached by the page) plus overlays and HUD."""
        shown, keep = [], set()
        for m in self.models.values():
            if m.parts:
                for i, part in enumerate(m.parts):
                    key = f"{m.key}-p{i}-{id(m.parts):x}"
                    if key not in self.webui.MESHES:
                        self.webui.put_mesh(key, pack(part))
                    keep.add(key)
                    shown.append({"name": f"{m.name} part {i + 1}", "url": f"batsy://mesh/{key}",
                                  "matrix": np.eye(4).ravel().tolist(), "color": PALETTE[i % len(PALETTE)]})
            else:
                if m.key not in self.webui.MESHES:
                    self.webui.put_mesh(m.key, pack(m.base))
                keep.add(m.key)
                shown.append({"name": m.name, "url": f"batsy://mesh/{m.key}",
                              "matrix": m.matrix.T.ravel().tolist(), "color": m.color})  # three.js: column-major
        self.webui.drop_meshes(keep)
        b = self._bounds()
        self.push({"cmd": "model_state", "models": shown,
                   "bounds": [b[0].tolist(), b[1].tolist()] if b is not None else None,
                   "dims": self.dims_on, "grid": self.grid_on, "style": self.style, "opacity": self.opacity,
                   "section": self.section, "spin": self.spinning, "measure": self.measuring,
                   "hud": self._hud(), "message": self.message, "reset_camera": reset_camera, "view": view})

    def measured(self, mm: float) -> None:
        with self.lock:
            self.last_measure = float(mm)
            self.sync()

    def _stats(self, m: Model, material: str = "pla", infill: float = 1.0) -> dict:
        mm = m.mesh()
        out = {"name": m.name, "file": m.path, "size_mm": [round(float(v), 2) for v in mm.extents],
               "bounds_mm": [[round(float(v), 2) for v in mm.bounds[0]], [round(float(v), 2) for v in mm.bounds[1]]],
               "triangles": int(len(mm.faces)), "watertight": bool(mm.is_watertight),
               "surface_area_cm2": round(float(mm.area) / 100, 2), "parts": int(len(mm.split(only_watertight=False)))
               if len(mm.faces) < 400_000 else None}
        if mm.is_watertight:
            vol_cm3 = abs(float(mm.volume)) / 1000
            dens = DENSITY.get(material.lower(), 1.24)
            out.update(volume_cm3=round(vol_cm3, 2), center_of_mass=[round(float(v), 2) for v in mm.center_mass],
                       material=material, mass_g=round(vol_cm3 * dens * infill, 1))
        return out

    def _targets(self, cmd: dict) -> list[Model]:
        name = cmd.get("model")
        if name and name in self.models:
            return [self.models[name]]
        return list(self.models.values())

    # --- commands -------------------------------------------------------------------------------------------
    def run(self, c: dict) -> dict:
        with self.lock:
            reply = self._run(c)
        if c.get("cmd") not in ("close", "info", "screenshot", "export") and self.models and "error" not in reply:
            self.present(True)  # any command about the model brings it back on screen
        return reply

    def _run(self, c: dict) -> dict:
        cmd = c.get("cmd")
        if cmd == "load":
            path = os.path.expanduser(c["path"])
            mesh = trimesh.load(path, force="mesh", skip_materials=True)  # textures unused; they cost ~700 MB on big GLBs
            if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
                return {"error": f"no mesh found in {path}"}
            if path.lower().endswith((".glb", ".gltf")):  # glTF is Y-up; the viewer (and printing) is Z-up
                mesh.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))
            if not c.get("keep"):
                self.models.clear()
                self.section, self.measuring, self.last_measure = None, False, None
            name = c.get("name") or Path(path).stem
            self.models[name] = Model(name, path, mesh, PALETTE[len(self.models) % len(PALETTE)])
            self.message = f"Loaded {Path(path).name}"
            self.sync(reset_camera=True, view="iso")
            return self._stats(self.models[name])
        if cmd == "info":
            return {"models": [self._stats(m, c.get("material", "pla"), c.get("infill", 1.0))
                               for m in self._targets(c)], "last_measure_mm": self.last_measure, "view": self.style}
        if cmd == "close":
            self.spinning = self.measuring = False
            self.sync()
            self.present(False)
            return {"ok": True}
        if cmd == "screenshot":
            OUT.mkdir(parents=True, exist_ok=True)
            data = self.snapshot() if self.snapshot else None
            if not data or "," not in data:
                return {"error": "the view isn't on screen"}
            path = OUT / f"view-{int(time.time())}.png"
            path.write_bytes(base64.b64decode(data.split(",", 1)[1]))
            return {"ok": True, "path": str(path)}
        if not self.models:
            return {"error": "no model loaded"}
        view = None
        if cmd == "view":
            view = c.get("view", "iso")
            self.message = f"{view.capitalize()} view"
        elif cmd == "orbit":  # move the camera, not the model
            self.push({"cmd": "model_orbit", "azimuth": float(c.get("azimuth", 0)),
                       "elevation": float(c.get("elevation", 0))})
        elif cmd == "zoom":
            self.push({"cmd": "model_zoom", "factor": float(c.get("factor", 1.5))})
        elif cmd == "reset":
            for m in self._targets(c):
                m.matrix, m.parts = np.eye(4), None
            self.opacity, self.style, self.section = 1.0, "solid", None
            self.message = "Reset"
            self.sync(reset_camera=True, view="iso")
            return self._ok(c)
        elif cmd in ("rotate", "move", "scale", "mirror", "center", "drop"):
            for m in self._targets(c):
                mm = m.mesh()
                ctr = mm.bounds.mean(axis=0)
                if cmd == "rotate":
                    t = _rotation(c.get("axis", "z"), float(c.get("deg", 90)), ctr)
                elif cmd == "move":
                    t = trimesh.transformations.translation_matrix([float(c.get(k, 0)) for k in ("dx", "dy", "dz")])
                elif cmd == "scale":
                    if c.get("to_mm") is not None:  # "make it 50 mm tall"
                        axis = "xyz".index(c.get("axis", "z"))
                        f = float(c["to_mm"]) / max(float(mm.extents[axis]), 1e-9)
                    else:
                        f = float(c.get("factor", 1.0))
                    t = trimesh.transformations.scale_matrix(f, ctr)
                elif cmd == "mirror":
                    t = trimesh.transformations.reflection_matrix(ctr, AXES[c.get("axis", "x")])
                elif cmd == "center":
                    t = trimesh.transformations.translation_matrix([-ctr[0], -ctr[1], -mm.bounds[0][2]])
                else:  # drop onto the floor
                    t = trimesh.transformations.translation_matrix([0, 0, -mm.bounds[0][2]])
                m.matrix = t @ m.matrix
                m.parts = None
            self.section = None
            self.message = c.get("say") or cmd.capitalize()
        elif cmd == "style":
            s = c.get("style", "solid")
            if s in ("xray", "transparent", "see through"):
                self.opacity, self.style = 0.35, "solid"
            elif s == "opaque":
                self.opacity = 1.0
            else:
                self.style = s  # solid | wireframe | edges
        elif cmd == "color":
            for m in self._targets(c):
                m.color = COLORS.get(c.get("color", ""), c.get("color", m.color))
        elif cmd == "dims":
            self.dims_on = bool(c.get("on", True))
        elif cmd == "grid":
            self.grid_on = bool(c.get("on", True))
        elif cmd == "spin":
            self.spinning = bool(c.get("on", True))
        elif cmd == "section":
            m = self._targets(c)[0]
            mm = m.mesh()
            ax = "xyz".index(c.get("axis", "z"))
            origin = mm.bounds.mean(axis=0)
            origin[ax] = mm.bounds[0][ax] + float(c.get("at", 0.5)) * mm.extents[ax]
            normal = [0.0, 0.0, 0.0]
            upper = c.get("keep") == "upper"
            normal[ax] = 1.0 if upper else -1.0  # the page keeps the side the normal points to
            self.section = {"normal": normal, "constant": float(-origin[ax] if upper else origin[ax])}
            self.message = f"Cross-section along {'xyz'[ax].upper()} at {origin[ax]:.1f} mm"
            self.sync()
            out = {"ok": True}
            try:
                sec = mm.section(plane_origin=origin, plane_normal=AXES["xyz"[ax]])
                if sec is not None:
                    planar, _ = sec.to_2D()
                    out["section_extents_mm"] = [round(float(v), 2) for v in planar.extents]
                    out["section_area_mm2"] = round(float(planar.area), 2)
            except Exception as e:  # the cut is on screen either way; only the numbers are missing
                log.warning("section stats: %s", e)
            return out
        elif cmd == "explode":
            m = self._targets(c)[0]
            whole = m.mesh()
            parts = whole.split(only_watertight=False)
            if len(parts) < 2:
                return {"ok": False, "note": "this model is a single body"}
            ctr = whole.bounds.mean(axis=0)
            f = float(c.get("factor", 0.6))
            moved = []
            for part in parts[:64]:
                part = part.copy()
                part.apply_translation((part.bounds.mean(axis=0) - ctr) * f)
                moved.append(part)
            m.parts = moved
            self.section = None
            self.message = f"Exploded into {len(parts)} parts"
            self.sync(reset_camera=True)
            return {"ok": True, "parts": len(parts)}
        elif cmd == "measure":
            self.measuring = bool(c.get("on", True))
            self.message = "Click two points to measure" if self.measuring else "Measuring off"
        elif cmd == "export":
            OUT.mkdir(parents=True, exist_ok=True)
            fmt = c.get("format", "stl").lower().lstrip(".")
            m = self._targets(c)[0]
            path = os.path.expanduser(c.get("path") or str(OUT / f"{m.name}-batsy.{fmt}"))
            mesh = trimesh.util.concatenate(m.parts) if m.parts else m.mesh()
            mesh.export(path)
            return {"ok": True, "path": path}
        elif cmd == "message":
            self.message = c.get("text", "")
        else:
            return {"error": f"unknown command {cmd}"}
        self.sync(view=view)
        return self._ok(c)

    def _ok(self, c: dict) -> dict:
        return {"ok": True, **({"size_mm": [round(float(v), 2) for v in self._targets(c)[0].mesh().extents]}
                               if self.models else {})}


def serve(scene: Scene) -> None:
    """Answer JSON-line commands on SOCK (a thread per client; the scene lock keeps them in order)."""
    def client(conn: socket.socket) -> None:
        with conn, conn.makefile("rw") as f:
            for line in f:
                try:
                    reply = scene.run(json.loads(line))
                except ValueError:
                    reply = {"error": "bad json"}
                except Exception as e:  # a broken file must never take the viewer down
                    log.exception("3d command failed")
                    reply = {"error": f"{type(e).__name__}: {e}"}
                f.write(json.dumps(reply, default=float) + "\n")
                f.flush()

    def loop() -> None:
        SOCK.unlink(missing_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(SOCK))
        os.chmod(SOCK, 0o600)
        srv.listen(8)
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=client, args=(conn,), daemon=True, name="3d-client").start()

    threading.Thread(target=loop, daemon=True, name="3d-server").start()


def emit(ev: dict) -> None:
    """Events for whoever started us (Batsy reads our stdout)."""
    try:
        print(json.dumps(ev), flush=True)
    except (BrokenPipeError, OSError):
        pass


def main() -> None:
    """Stand-alone window (when the overlay is off): same page, viewer only."""
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QApplication, QMainWindow

    from batsy import webui

    webui.prepare()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    win = QMainWindow()
    win.setWindowTitle("Batsy 3D")
    win.resize(1280, 820)

    class Show(QObject):
        on = Signal(bool)

    show = Show()
    show.on.connect(lambda on: (win.show(), win.raise_(), win.activateWindow()) if on else win.hide())

    def event(ev: dict) -> None:
        if ev.get("event") == "measured":
            scene.measured(ev.get("mm", 0))
            emit(ev)

    web = webui.Web("index.html?mode=viewer", event)
    win.setCentralWidget(web.view)
    scene = Scene(web.send, show.on.emit, lambda: web.call("batsy.viewer.snapshot()"))
    web.on_reload = lambda: (web.send({"cmd": "model3d", "on": True}), scene.sync(reset_camera=True))
    serve(scene)
    if len(sys.argv) > 1:
        threading.Thread(target=scene.run, args=({"cmd": "load", "path": sys.argv[1]},), daemon=True).start()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
