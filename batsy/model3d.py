"""Talk to the 3D viewer: open models, and turn everyday phrases into viewer commands without any AI."""
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from batsy.config import DATA_DIR

log = logging.getLogger("batsy.3d")
MODEL_EXTS = {".stl", ".obj", ".ply", ".glb", ".gltf", ".3mf", ".off", ".step", ".stp", ".dae"}
MODEL_WORD = re.compile(r"\b(3d|three d|model|stl|obj|mesh|part|print|cad|step file|glb)\b")
NUM = r"(-?\d+(?:\.\d+)?)"
WORDNUM = {"ninety": "90", "forty five": "45", "one eighty": "180", "a hundred and eighty": "180", "thirty": "30",
           "sixty": "60", "half": "0.5", "double": "2", "twice": "2", "ten": "10", "twenty": "20", "fifty": "50",
           "a hundred": "100", "hundred": "100", "five": "5", "two": "2", "three": "3", "four": "4"}
UNIT = {"mm": 1, "millimeter": 1, "millimeters": 1, "millimetre": 1, "millimetres": 1, "cm": 10,
        "centimeter": 10, "centimeters": 10, "centimetre": 10, "centimetres": 10, "m": 1000, "meter": 1000,
        "meters": 1000, "inch": 25.4, "inches": 25.4, "in": 25.4}
DIM_AXIS = {"tall": "z", "high": "z", "height": "z", "wide": "x", "width": "x", "long": "y", "length": "y",
            "deep": "y", "depth": "y", "thick": "z"}


def _sock() -> Path:
    from batsy.viewer3d import SOCK

    return SOCK


class Viewer3D:
    def __init__(self, on_event=None):
        self.on_event = on_event  # e.g. speak "23.4 millimetres" after a click-to-measure
        self.host = None  # e.g. Batsy's overlay: a callable that starts the viewer inside it instead of a window
        self.proc: subprocess.Popen | None = None
        self.last_used = 0.0

    def is_open(self) -> bool:
        if self.proc is not None and self.proc.poll() is None:
            return True
        try:  # hosted in the overlay (or started by another Batsy process): is anyone serving the socket?
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect(str(_sock()))
            return True
        except OSError:
            return False

    def active(self) -> bool:
        """Open and used recently: plain phrases like "rotate it" are meant for the viewer."""
        return self.is_open() and time.time() - self.last_used < 1800

    def _start(self) -> None:
        if self.host is not None:
            self.host()
            for _ in range(200):  # VTK takes a few seconds to load the first time
                try:
                    self.send({"cmd": "info"}, start=False)
                    return
                except OSError:
                    time.sleep(0.1)
            raise RuntimeError("the 3D viewer didn't start in the overlay")
        log_f = open(DATA_DIR / "viewer3d.log", "ab")
        self.proc = subprocess.Popen([sys.executable, "-m", "batsy.viewer3d"], stdout=subprocess.PIPE,
                                     stderr=log_f, text=True, start_new_session=True)
        threading.Thread(target=self._events, args=(self.proc,), daemon=True).start()
        for _ in range(150):  # VTK takes a few seconds to start the first time
            if _sock().exists():
                try:
                    self.send({"cmd": "info"}, start=False)
                    return
                except OSError:
                    pass
            if self.proc.poll() is not None:
                raise RuntimeError("the 3D viewer failed to start; see viewer3d.log")
            time.sleep(0.1)
        raise RuntimeError("the 3D viewer didn't start in time")

    def _events(self, proc) -> None:
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if self.on_event:
                self.on_event(ev)

    def send(self, cmd: dict, start: bool = True) -> dict:
        if start and not self.is_open():
            self._start()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(130)
        s.connect(str(_sock()))
        with s, s.makefile("rw") as f:
            f.write(json.dumps(cmd) + "\n")
            f.flush()
            reply = json.loads(f.readline() or "{}")
        self.last_used = 0.0 if cmd.get("cmd") == "close" else time.time()  # closed: no longer what we're talking about
        return reply

    def open(self, path: Path, keep: bool = False) -> dict:
        return self.send({"cmd": "load", "path": str(path), "keep": keep})


VIEWER = Viewer3D()


# --- which model ---------------------------------------------------------------------------------------------
def find_model(text: str) -> Path | None:
    from batsy import docs

    t = text.lower()
    m = re.search(r"\b(?:this|that|the|my)\s+(?:(.+?)\s+)?(?:3d model|three d model|model|stl|obj|mesh|part|"
                  r"step file|3mf|glb)\b", t)
    name = (m.group(1) or "").strip() if m else ""
    if name in ("3d", "three d", "this", "that"):
        name = ""
    if name:
        recent = [p for p in docs.recent_files(MODEL_EXTS) if all(w in p.stem.lower().replace("_", " ")
                                                                    for w in name.split())]
        if recent:
            return recent[0]
        found = docs.search_files(name, MODEL_EXTS)
        if found and found[0][0] >= 0.6:
            return found[0][1]
        return None
    recent = docs.recent_files(MODEL_EXTS)
    if recent:
        return recent[0]
    found = sorted((p for d in (Path.home() / "Downloads", Path.home() / "Desktop", Path.home() / "Batsy/3d")
                    if d.is_dir() for p in d.iterdir() if p.suffix.lower() in MODEL_EXTS),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return found[0] if found else None


OPEN = re.compile(r"^(?:(?:can you |please )?(open|show|view|load|display|pull up|bring up|see|look at)) .*"
                  r"\b(3d|three d|model|stl|obj|mesh|step file|3mf|glb|part)\b")


# --- phrases -> viewer commands (no AI) ------------------------------------------------------------------------
def _num(t: str) -> str:
    for w, d in sorted(WORDNUM.items(), key=lambda x: -len(x[0])):
        t = re.sub(rf"\b{w}\b", d, t)
    return t


def _fmt_size(s) -> str:
    return f"{s[0]:.1f} by {s[1]:.1f} by {s[2]:.1f} millimetres"


def parse(n: str) -> dict | None:
    """Normalised utterance -> viewer command dict (with a 'say' template), or None."""
    t = _num(n)
    t = re.sub(r"\b(the model|the part|the object|it|this|that|model)\b", "it", t)
    if re.search(r"\b(close|exit|quit)\b.*\b(viewer|3d|model)\b", t):
        return {"cmd": "close", "say": "Closed the 3D viewer."}
    m = re.search(rf"\b(rotate|turn|spin|tilt)\b.*?(?:by )?{NUM}\s*(?:degrees?|deg)?(?:.*?\b(?:around|about|on|along) (?:the )?([xyz])\b)?", t)
    if m and m.group(1) != "spin":
        axis = m.group(3) or ("x" if re.search(r"\b(up|down|forward|backward|over)\b", t) else "z")
        deg = float(m.group(2)) * (-1 if re.search(r"\b(right|clockwise|down|backward)\b", t)
                                   and "anticlockwise" not in t and "counter" not in t else 1)
        return {"cmd": "rotate", "axis": axis, "deg": deg, "say": f"Rotated {abs(deg):g} degrees."}
    m = re.search(r"\b(rotate|turn) it (left|right|up|down|over)\b", t)
    if m:
        axis = "x" if m.group(2) in ("up", "down", "over") else "z"
        deg = {"left": 90, "right": -90, "up": 90, "down": -90, "over": 180}[m.group(2)]
        return {"cmd": "rotate", "axis": axis, "deg": deg, "say": ""}
    if re.fullmatch(r"(?:can you |please )?(?:rotate|turn) (?:it|the \w+)(?: (?:a bit|around a bit|again))?(?: please)?", t):
        return {"cmd": "rotate", "axis": "z", "deg": 90, "say": ""}  # no amount: a quarter turn
    if re.search(r"\b(flip it|flip over|upside down)\b", t):
        return {"cmd": "rotate", "axis": "x", "deg": 180, "say": "Flipped."}
    if re.search(r"\bturn it around\b", t):
        return {"cmd": "rotate", "axis": "z", "deg": 180, "say": "Turned around."}
    if re.search(r"\b(stop|pause) (spinning|rotating|turntable)\b", t):
        return {"cmd": "spin", "on": False, "say": ""}
    if re.search(r"\b(spin|turntable|keep rotating|rotate continuously|show it all around)\b", t):
        return {"cmd": "spin", "on": True, "say": ""}
    m = re.search(r"\b(top|bottom|front|back|left|right|side|isometric|iso|3 quarter)\b (?:view|side)", t) or \
        re.search(r"\bview (?:from|it from) (?:the )?(top|bottom|front|back|left|right|side)\b", t) or \
        re.search(r"\b(?:show|look at) (?:it )?from (?:the )?(top|bottom|front|back|left|right|side|above|below)\b", t)
    if m:
        v = {"side": "right", "isometric": "iso", "3 quarter": "iso", "above": "top", "below": "bottom"}.get(m.group(1), m.group(1))
        return {"cmd": "view", "view": v, "say": ""}
    m = re.search(r"\bzoom (in|out|into)\b", t)
    if m:
        return {"cmd": "zoom", "factor": 0.6 if m.group(1) == "out" else 1.6, "say": ""}
    if re.search(r"\b(look|orbit|move the camera|camera)\b.*\b(left|right|up|down)\b", t):
        d = re.search(r"\b(left|right|up|down)\b", t).group(1)
        return {"cmd": "orbit", "azimuth": {"left": -45, "right": 45}.get(d, 0),
                "elevation": {"up": 30, "down": -30}.get(d, 0), "say": ""}
    if re.search(r"\b(reset|start over|original position|undo everything)\b", t):
        return {"cmd": "reset", "say": "Reset."}
    if re.search(r"\b(hide|turn off|remove) (the )?(dimensions|measurements|rulers)\b", t):
        return {"cmd": "dims", "on": False, "say": ""}
    if re.search(r"\b(show|turn on|add|display) (the )?(dimensions|measurements|rulers|sizes?)\b", t):
        return {"cmd": "dims", "on": True, "say": "{size}."}
    if re.search(r"\b(how big|what size|dimensions|how large|its size|measurements)\b", t):
        return {"cmd": "info", "say": "{size}."}
    m = re.search(r"\bhow (tall|high|wide|long|deep|thick)\b", t)
    if m:
        return {"cmd": "info", "axis": DIM_AXIS[m.group(1)], "say": "{axis_size}."}
    m = re.search(r"\b(?:how (?:heavy|much (?:will it )?weigh)|weight|mass)\b(?:.*\b(pla|petg|abs|asa|tpu|nylon|resin|aluminium|aluminum|steel|brass|wood)\b)?", t)
    if m:
        return {"cmd": "info", "material": m.group(1) or "pla", "say": "{mass}"}
    if re.search(r"\b(volume|how much (filament|material|plastic))\b", t):
        return {"cmd": "info", "say": "{volume}"}
    if re.search(r"\b(is it|it is|it's) (watertight|printable|manifold|solid)\b|\bany holes in the mesh\b", t):
        return {"cmd": "info", "say": "{watertight}"}
    m = re.search(rf"\bmove it (up|down|left|right|forward|back|backward) (?:by )?{NUM} ?([a-z]+)?", t)
    if m:
        dist = float(m.group(2)) * UNIT.get(m.group(3) or "mm", 1)
        d = {"up": ("dz", 1), "down": ("dz", -1), "left": ("dx", -1), "right": ("dx", 1), "forward": ("dy", 1),
             "back": ("dy", -1), "backward": ("dy", -1)}[m.group(1)]
        return {"cmd": "move", d[0]: d[1] * dist, "say": f"Moved {m.group(2)} {m.group(3) or 'mm'} {m.group(1)}."}
    m = re.search(rf"\bmake it {NUM} ?([a-z]+) (tall|high|wide|long|deep|thick)\b", t)
    if m:
        mm = float(m.group(1)) * UNIT.get(m.group(2), 1)
        return {"cmd": "scale", "to_mm": mm, "axis": DIM_AXIS[m.group(3)], "say": "Scaled. Now {size}."}
    m = re.search(rf"\bscale (?:it )?(?:by |to )?{NUM} ?(percent|%|times|x)?", t)
    if m:
        f = float(m.group(1)) / (100 if m.group(2) in ("percent", "%") else 1)
        return {"cmd": "scale", "factor": f, "say": "Scaled. Now {size}."}
    if re.search(r"\b(make it|scale it) (bigger|larger)\b", t):
        return {"cmd": "scale", "factor": 1.25, "say": "Bigger. Now {size}."}
    if re.search(r"\b(make it|scale it) smaller\b", t):
        return {"cmd": "scale", "factor": 0.8, "say": "Smaller. Now {size}."}
    m = re.search(r"\bmirror (?:it )?(?:along |on |across )?(?:the )?([xyz])?", t)
    if m and "mirror" in t:
        return {"cmd": "mirror", "axis": m.group(1) or "x", "say": "Mirrored."}
    if re.search(r"\b(center it|centre it|put it in the middle)\b", t):
        return {"cmd": "center", "say": "Centered."}
    if re.search(r"\b(drop it|put it on the (floor|bed|ground)|lay it flat)\b", t):
        return {"cmd": "drop", "say": "On the floor."}
    m = re.search(r"\b(wireframe|wire frame|x ?ray|see through|transparent|solid|opaque|show edges|edges)\b", t)
    if m:
        s = {"wire frame": "wireframe", "x ray": "xray", "xray": "xray", "see through": "xray",
             "transparent": "xray", "show edges": "edges"}.get(m.group(1), m.group(1))
        return {"cmd": "style", "style": s, "say": ""}
    m = re.search(r"\b(?:colou?r it|make it|paint it|change (?:the )?colou?r to) (red|green|blue|yellow|orange|purple|pink|white|black|grey|gray|silver|gold|cyan|brown)\b", t)
    if m:
        return {"cmd": "color", "color": m.group(1), "say": ""}
    m = re.search(r"\b(cross ?section|section view|cut it (?:in half|open)?|slice it|cut through)\b(?:.*\b(?:along|on|through) (?:the )?([xyz])\b)?(?:.*?{0})?".format(NUM), t)
    if m:
        at = 0.5
        pm = re.search(r"(\d+(?:\.\d+)?) ?(percent|%)", t)
        if pm:
            at = float(pm.group(1)) / 100
        return {"cmd": "section", "axis": m.group(2) or "z", "at": at, "say": "Here's the cross-section. {section}"}
    if re.search(r"\b(explode|exploded view|take it apart|separate the parts)\b", t):
        return {"cmd": "explode", "say": "{parts}"}
    if re.search(r"\b(measure|ruler|distance between)\b", t):
        return {"cmd": "measure", "say": "Click two points on the model to measure."}
    if re.search(r"\b(hide|remove) (the )?(grid|floor)\b", t):
        return {"cmd": "grid", "on": False, "say": ""}
    if re.search(r"\b(show|add) (the )?(grid|floor)\b", t):
        return {"cmd": "grid", "on": True, "say": ""}
    m = re.search(r"\b(?:save|export) (?:it )?(?:as )?(?:an? )?(stl|obj|ply|glb|3mf|off)?\b", t)
    if m and re.search(r"\b(save|export)\b", t):
        return {"cmd": "export", "format": m.group(1) or "stl", "say": "Saved {path_name}."}
    return None


def speakable(cmd: dict, reply: dict) -> str:
    """Fill the parser's 'say' template from the viewer's reply."""
    say = cmd.get("say", "")
    if "error" in reply:
        return f"The viewer said: {reply['error']}"
    stats = (reply.get("models") or [{}])[0] if "models" in reply else {}
    size = stats.get("size_mm") or reply.get("size_mm")
    fill = {
        "size": _fmt_size(size) if size else "",
        "axis_size": f"{size['xyz'.index(cmd.get('axis', 'z'))]:.1f} millimetres" if size else "",
        "mass": (f"About {stats['mass_g']} grams in {stats['material'].upper()} at full infill, "
                 f"{stats['volume_cm3']} cubic centimetres." if stats.get("mass_g") is not None else
                 "I can't weigh it: the mesh isn't watertight."),
        "volume": (f"{stats['volume_cm3']} cubic centimetres." if stats.get("volume_cm3") is not None else
                   "The mesh isn't watertight, so the volume isn't reliable."),
        "watertight": ("Yes, it's watertight and should slice fine." if stats.get("watertight") else
                       "No, the mesh has holes or open edges. It may need repair before printing."),
        "section": (f"The cut is {reply['section_extents_mm'][0]:.1f} by {reply['section_extents_mm'][1]:.1f} mm."
                    if reply.get("section_extents_mm") else ""),
        "parts": (f"Exploded into {reply['parts']} parts." if reply.get("parts") else
                  reply.get("note", "It's a single body.")),
        "path_name": Path(reply.get("path", "")).name,
    }
    for k, v in fill.items():
        say = say.replace("{" + k + "}", v)
    return say.strip()


def spoken_name(path: Path) -> str:
    """'skoda_top_tether_hook_part2_20260812.stl' -> 'top tether hook': no dates, versions or part numbers."""
    words = [w for w in re.split(r"[\s_\-.]+", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", path.stem).lower())
             if w and not re.fullmatch(r"\d+|(?:v|ver|rev|part|pt)\d*", w) and w not in ("final", "copy", "new", "old")]
    return " ".join(words[-3:]) or "model"


def handle(n: str) -> str | None:
    """Zero-AI 3D handling. Returns what to say ('' = silent), or None if this isn't a 3D request."""
    if OPEN.search(n):
        path = find_model(n)
        if not path:
            return "I couldn't find that 3D model. Tell me its file name."
        st = VIEWER.open(path)
        if "error" in st:
            return f"I couldn't open it: {st['error']}"
        return f"Here's the {spoken_name(path)}."  # full name and size are on screen
    if not VIEWER.active():
        return None
    cmd = parse(n)
    if cmd is None:
        return None
    reply = VIEWER.send({k: v for k, v in cmd.items() if k != "say"})
    return speakable(cmd, reply)
