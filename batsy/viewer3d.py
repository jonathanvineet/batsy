"""Batsy's 3D viewer: a PyVista/VTK window driven by JSON commands over a local socket.

Run as its own process (`python -m batsy.viewer3d`). Batsy's voice parser and Claude Code both send it
commands like {"cmd": "rotate", "axis": "z", "deg": 90} and get JSON replies (dimensions, volume...).
Geometry lives in trimesh (exact maths in mm); the window shows it with rulers, a floor grid and a HUD.
Events the user triggers in the window (click-to-measure) are printed as JSON lines on stdout.
"""
import json
import os
import queue
import socket
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pyvista as pv
import trimesh
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMainWindow
from pyvistaqt import QtInteractor

from batsy.config import SOCKET_PATH, WORKSPACE

SOCK = SOCKET_PATH.parent / f"batsy-3d-{os.getuid()}.sock"
OUT = WORKSPACE / "3d"
PALETTE = ["#b9a7ff", "#7fd4ff", "#ffb86b", "#7dffb0", "#ff7fbf", "#ffe57f", "#c0c0c0", "#ff8080"]
COLORS = {"red": "#ff5a5a", "green": "#5adf7a", "blue": "#5a9bff", "yellow": "#ffe15a", "orange": "#ffa64d",
          "purple": "#b07cff", "pink": "#ff7cc8", "white": "#f2f2f2", "black": "#222222", "grey": "#a0a0a0",
          "gray": "#a0a0a0", "silver": "#c8c8d0", "gold": "#e6c35a", "cyan": "#5ae6e6", "brown": "#a0704a"}
DENSITY = {"pla": 1.24, "petg": 1.27, "abs": 1.04, "asa": 1.07, "tpu": 1.21, "nylon": 1.14, "resin": 1.15,
           "aluminium": 2.70, "aluminum": 2.70, "steel": 7.85, "brass": 8.5, "wood": 0.7}
AXES = {"x": [1, 0, 0], "y": [0, 1, 0], "z": [0, 0, 1]}


def _poly(tm: trimesh.Trimesh) -> pv.PolyData:
    faces = np.hstack([np.full((len(tm.faces), 1), 3), tm.faces]).ravel()
    return pv.PolyData(np.asarray(tm.vertices, dtype=float), faces)


def _rotation(axis: str, deg: float, center) -> np.ndarray:
    return trimesh.transformations.rotation_matrix(np.radians(deg), AXES[axis], center)


class Model:
    def __init__(self, name: str, path: str | None, mesh: trimesh.Trimesh, color: str):
        self.name, self.path, self.base, self.color = name, path, mesh, color
        self.matrix = np.eye(4)
        self.parts: list[trimesh.Trimesh] | None = None  # exploded view

    def mesh(self) -> trimesh.Trimesh:
        m = self.base.copy()
        m.apply_transform(self.matrix)
        return m


class Viewer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Batsy 3D")
        self.resize(1280, 820)
        self.plotter = QtInteractor(self)
        self.setCentralWidget(self.plotter.interactor)
        p = self.plotter
        p.set_background("#100d1c", top="#2b2244")
        p.add_axes(color="white")
        p.enable_anti_aliasing()
        self.models: dict[str, Model] = {}
        self.dims_on, self.grid_on, self.style, self.opacity = True, True, "solid", 1.0
        self.extra_actors: list = []
        self.last_measure: float | None = None
        self.message = "Say: rotate, top view, dimensions, cross section, explode..."
        self.spin = QTimer(self, interval=33, timeout=lambda: (p.camera.Azimuth(1.2), p.render()))
        self.inbox: queue.Queue = queue.Queue()
        QTimer(self, interval=30, timeout=self._drain).start()
        threading.Thread(target=self._serve, daemon=True).start()
        self._hud()

    # --- socket -------------------------------------------------------------
    def _serve(self) -> None:
        SOCK.unlink(missing_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(SOCK))
        os.chmod(SOCK, 0o600)
        srv.listen(8)
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn: socket.socket) -> None:
        with conn, conn.makefile("rw") as f:
            for line in f:
                box: dict = {}
                done = threading.Event()
                try:
                    self.inbox.put((json.loads(line), box, done))
                except ValueError:
                    box["error"] = "bad json"
                    done.set()
                done.wait(120)
                f.write(json.dumps(box.get("result", {"error": box.get("error", "timeout")}), default=float) + "\n")
                f.flush()

    def _drain(self) -> None:
        while True:
            try:
                cmd, box, done = self.inbox.get_nowait()
            except queue.Empty:
                return
            try:
                box["result"] = self.run(cmd)
            except Exception as e:
                box["result"] = {"error": f"{type(e).__name__}: {e}"}
            done.set()

    # --- helpers ------------------------------------------------------------
    def _targets(self, cmd: dict) -> list[Model]:
        name = cmd.get("model")
        if name and name in self.models:
            return [self.models[name]]
        return list(self.models.values())

    def _all_bounds(self):
        meshes = [m.mesh() for m in self.models.values()]
        if not meshes:
            return None
        lo = np.min([x.bounds[0] for x in meshes], axis=0)
        hi = np.max([x.bounds[1] for x in meshes], axis=0)
        return lo, hi

    def _redraw(self, reset_camera: bool = False) -> None:
        p = self.plotter
        for a in self.extra_actors:
            p.remove_actor(a)
        self.extra_actors = []
        for name, m in self.models.items():
            p.remove_actor(f"model-{name}")
            for i in range(64):
                p.remove_actor(f"part-{name}-{i}")
            kw = dict(smooth_shading=True, specular=0.35, specular_power=18, opacity=self.opacity,
                      show_edges=self.style == "edges", style="wireframe" if self.style == "wireframe" else "surface")
            if m.parts:
                for i, part in enumerate(m.parts):
                    p.add_mesh(_poly(part), color=PALETTE[i % len(PALETTE)], name=f"part-{name}-{i}", **kw)
            else:
                p.add_mesh(_poly(m.mesh()), color=m.color, name=f"model-{name}", **kw)
        b = self._all_bounds()
        if b is not None:
            lo, hi = b
            size = hi - lo
            if self.grid_on:
                span = float(max(size[0], size[1]) * 1.8 + 1)
                plane = pv.Plane(center=((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2] - 0.01), i_size=span,
                                 j_size=span, i_resolution=20, j_resolution=20)
                self.extra_actors.append(p.add_mesh(plane, style="wireframe", color="#6b5fa8", opacity=0.35,
                                                    line_width=1, pickable=False))
            if self.dims_on:
                off = float(max(size) * 0.08 + 0.5)
                rulers = [
                    ([lo[0], lo[1] - off, lo[2]], [hi[0], lo[1] - off, lo[2]], f"X  {size[0]:.1f} mm"),
                    ([hi[0] + off, lo[1], lo[2]], [hi[0] + off, hi[1], lo[2]], f"Y  {size[1]:.1f} mm"),
                    ([hi[0] + off, lo[1] - off, lo[2]], [hi[0] + off, lo[1] - off, hi[2]], f"Z  {size[2]:.1f} mm"),
                ]
                for a_, b_, title in rulers:
                    if np.linalg.norm(np.subtract(b_, a_)) > 1e-6:
                        self.extra_actors.append(p.add_ruler(a_, b_, title=title, number_labels=3,
                                                             font_size_factor=0.7, label_color="white",
                                                             tick_color="white"))
                self.extra_actors.append(p.add_mesh(pv.Box(bounds=(lo[0], hi[0], lo[1], hi[1], lo[2], hi[2])),
                                                    style="wireframe", color="#ff7cc8", opacity=0.5,
                                                    line_width=1, pickable=False))
        self._hud()
        if reset_camera:
            p.reset_camera()
        p.render()

    def _hud(self) -> None:
        lines = []
        for name, m in self.models.items():
            mm = m.mesh()
            e = mm.extents
            lines.append(f"{name}:  {e[0]:.1f} x {e[1]:.1f} x {e[2]:.1f} mm   ({len(mm.faces):,} triangles)")
        if self.last_measure is not None:
            lines.append(f"measured: {self.last_measure:.2f} mm")
        lines.append(self.message)
        self.plotter.add_text("\n".join(lines), position="upper_left", font_size=10, color="white", name="hud")

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

    # --- commands -------------------------------------------------------------
    def run(self, c: dict) -> dict:
        p, cmd = self.plotter, c.get("cmd")
        if cmd == "load":
            path = os.path.expanduser(c["path"])
            mesh = trimesh.load(path, force="mesh")
            if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
                return {"error": f"no mesh found in {path}"}
            if not c.get("keep"):
                self.models.clear()
                self.plotter.clear_actors()
                self.plotter.add_axes(color="white")
            name = c.get("name") or Path(path).stem
            self.models[name] = Model(name, path, mesh, PALETTE[len(self.models) % len(PALETTE)])
            self.message = f"Loaded {Path(path).name}"
            self._redraw(reset_camera=True)
            p.view_isometric()
            self.show()
            self.raise_()
            self.activateWindow()
            return self._stats(self.models[name])
        if cmd == "info":
            return {"models": [self._stats(m, c.get("material", "pla"), c.get("infill", 1.0))
                               for m in self._targets(c)], "last_measure_mm": self.last_measure,
                    "view": self.style}
        if not self.models and cmd not in ("close", "screenshot"):
            return {"error": "no model loaded"}
        if cmd == "view":
            v = c.get("view", "iso")
            {"top": p.view_xy, "bottom": lambda: (p.view_xy(), p.camera.Elevation(180)), "front": p.view_xz,
             "back": lambda: (p.view_xz(), p.camera.Azimuth(180)), "right": p.view_yz,
             "left": lambda: (p.view_yz(), p.camera.Azimuth(180)), "iso": p.view_isometric}.get(v, p.view_isometric)()
            p.reset_camera()
            self.message = f"{v} view"
        elif cmd == "orbit":  # move the camera, not the model
            p.camera.Azimuth(float(c.get("azimuth", 0)))
            p.camera.Elevation(float(c.get("elevation", 0)))
        elif cmd == "zoom":
            p.camera.zoom(float(c.get("factor", 1.5)))
        elif cmd == "reset":
            for m in self._targets(c):
                m.matrix, m.parts = np.eye(4), None
            self.opacity, self.style = 1.0, "solid"
            self._redraw(reset_camera=True)
            p.view_isometric()
            self.message = "reset"
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
            self.message = c.get("say") or cmd
            self._redraw()
        elif cmd == "style":
            s = c.get("style", "solid")
            if s in ("xray", "transparent", "see through"):
                self.opacity, self.style = 0.35, "solid"
            elif s == "opaque":
                self.opacity = 1.0
            else:
                self.style = s  # solid | wireframe | edges
            self._redraw()
        elif cmd == "color":
            for m in self._targets(c):
                m.color = COLORS.get(c.get("color", ""), c.get("color", m.color))
            self._redraw()
        elif cmd == "dims":
            self.dims_on = bool(c.get("on", True))
            self._redraw()
        elif cmd == "grid":
            self.grid_on = bool(c.get("on", True))
            self._redraw()
        elif cmd == "spin":
            (self.spin.start if c.get("on", True) else self.spin.stop)()
        elif cmd == "section":
            m = self._targets(c)[0]
            mm = m.mesh()
            ax = "xyz".index(c.get("axis", "z"))
            origin = mm.bounds.mean(axis=0)
            origin[ax] = mm.bounds[0][ax] + float(c.get("at", 0.5)) * mm.extents[ax]
            normal = [0, 0, 0]
            normal[ax] = -1 if c.get("keep") == "upper" else 1
            poly = _poly(mm)
            try:
                cut = poly.clip_closed_surface(normal=normal, origin=origin) if mm.is_watertight else \
                    poly.clip(normal=normal, origin=origin)
            except Exception:
                cut = poly.clip(normal=normal, origin=origin)
            p.remove_actor(f"model-{m.name}")
            p.add_mesh(cut, color=m.color, name=f"model-{m.name}", smooth_shading=True, specular=0.35)
            sec = mm.section(plane_origin=origin, plane_normal=AXES["xyz"[ax]])
            self.message = f"cross-section along {'xyz'[ax]} at {origin[ax]:.1f} mm"
            self._hud()
            p.render()
            if sec is not None:
                planar, _ = sec.to_2D()
                return {"ok": True, "section_extents_mm": [round(float(v), 2) for v in planar.extents],
                        "section_area_mm2": round(float(planar.area), 2)}
        elif cmd == "explode":
            m = self._targets(c)[0]
            parts = m.mesh().split(only_watertight=False)
            if len(parts) < 2:
                return {"ok": False, "note": "this model is a single body"}
            ctr = m.mesh().bounds.mean(axis=0)
            f = float(c.get("factor", 0.6))
            moved = []
            for part in parts:
                d = part.bounds.mean(axis=0) - ctr
                part = part.copy()
                part.apply_translation(d * f)
                moved.append(part)
            m.parts = moved
            self.message = f"exploded: {len(parts)} parts"
            self._redraw(reset_camera=True)
            return {"ok": True, "parts": len(parts)}
        elif cmd == "measure":
            def done(a, b, dist):
                self.last_measure = float(dist)
                print(json.dumps({"event": "measured", "mm": round(float(dist), 2)}), flush=True)
                self._hud()
            p.clear_measure_widgets()
            if c.get("on", True):
                p.add_measurement_widget(callback=done, color="#ffe15a")
                self.message = "click two points to measure"
            self._hud()
        elif cmd == "screenshot":
            OUT.mkdir(parents=True, exist_ok=True)
            path = str(OUT / f"view-{int(time.time())}.png")
            p.screenshot(path)
            return {"ok": True, "path": path}
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
            self._hud()
        elif cmd == "close":
            self.spin.stop()
            self.hide()
            return {"ok": True}
        else:
            return {"error": f"unknown command {cmd}"}
        p.render()
        return {"ok": True, **({"size_mm": [round(float(v), 2) for v in self._targets(c)[0].mesh().extents]}
                               if self.models else {})}


def main() -> None:
    pv.global_theme.font.family = "arial"
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    win = Viewer()
    if len(sys.argv) > 1:
        win.run({"cmd": "load", "path": sys.argv[1]})
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
