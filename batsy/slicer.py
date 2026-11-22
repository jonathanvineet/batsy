"""Slicing 3D models for your printer with OrcaSlicer, using the presets you sent Batsy.

Send Batsy a printer bundle from Creality Print or OrcaSlicer (.creality_printer / .orca_printer: a zip of
printer/, process/ and maybe filament/ JSON) and it becomes "your printer": import_bundle() keeps it in
~/.local/share/batsy/printer/. Then every 3D model you send (STL, 3MF, OBJ, STEP) is sliced in the background with
your default process ("nice"), and Batsy says how long the print takes and how much filament it needs. The G-code
goes next to the model, in a "sliced" folder, ready for the SD card.

Your presets only list what you changed: each "inherits" a system profile ("0.20mm Standard @Creality Ender-5 S1
0.4 nozzle"). OrcaSlicer's command line wants complete profiles, so flatten() follows the chain through Orca's own
Creality profiles (by name, or the closest name: Creality Print calls some of them slightly differently) and
merges your changes on top.

Voice: "slice it", "slice it with fast", "slice the iphone stand with litho", "how long will it take to print",
"which print profiles do I have".
"""
import json
import logging
import re
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path

from batsy.config import DATA_DIR

log = logging.getLogger("batsy.slicer")
PRINTER = DATA_DIR / "printer"           # printer.json, process/*.json, filament/*.json, settings.json
FLATPAK = "com.orcaslicer.OrcaSlicer"
MODELS = (".stl", ".3mf", ".obj", ".step", ".stp")
DEFAULT_PROCESS = "nicee"
_lock = threading.Lock()


# --- finding OrcaSlicer -------------------------------------------------------------------------------------
def _flatpak_root() -> Path | None:
    for base in (Path.home() / ".local/share/flatpak", Path("/var/lib/flatpak")):
        p = base / "app" / FLATPAK / "current" / "active" / "files"
        if p.exists():
            return p
    return None


ORCA_DATA = DATA_DIR / "orca-data"  # OrcaSlicer's settings folder for Batsy (its "system" folder must exist)


def command() -> list[str] | None:
    if _flatpak_root():
        (ORCA_DATA / "system").mkdir(parents=True, exist_ok=True)
        (ORCA_DATA / "user").mkdir(exist_ok=True)
        return ["flatpak", "run", FLATPAK, "--datadir", str(ORCA_DATA)]
    exe = shutil.which("orca-slicer") or shutil.which("OrcaSlicer")
    return [exe] if exe else None


ORCA_PROFILES = DATA_DIR / "orca-profiles"  # Orca's profile JSON from its GitHub (newer builds ship them packed, .opc)


def resources() -> Path | None:
    """Where OrcaSlicer's profiles are readable as JSON: the app's own (older builds), else the copy Batsy keeps."""
    root = _flatpak_root()
    for p in ([root / "share/OrcaSlicer/resources", root / "share/OrcaSlicer", root / "resources"] if root else []) + [ORCA_PROFILES]:
        if any((p / "profiles").glob("*/machine/*.json")):
            return p
    exe = shutil.which("orca-slicer")
    if exe:
        p = Path(exe).resolve().parent.parent / "resources"
        if (p / "profiles").exists():
            return p
    return None


# --- your presets ---------------------------------------------------------------------------------------------
def _pick_default(names: list[str], prefer: tuple) -> str:
    for want in prefer:
        hit = next((n for n in names if want in n.lower()), None)
        if hit:
            return hit
    return names[0] if names else ""


def import_bundle(path: Path) -> dict:
    """A .creality_printer / .orca_printer bundle (printer + processes + filaments) replaces your printer; a plain
    zip of process / filament JSON (e.g. "Process presets.zip") is added to it. Returns what's in it."""
    with zipfile.ZipFile(path) as z:
        files = {n: z.read(n) for n in z.namelist() if n.endswith(".json") and not n.endswith("bundle_structure.json")}
    has_printer = any(n.split("/")[0] == "printer" for n in files)
    if has_printer and PRINTER.exists():
        shutil.rmtree(PRINTER)
    (PRINTER / "process").mkdir(parents=True, exist_ok=True)
    (PRINTER / "filament").mkdir(exist_ok=True)
    got = {"printer": None, "process": [], "filament": []}
    for name, data in files.items():
        try:
            cfg = json.loads(data)
        except ValueError:
            continue
        top = name.split("/")[0] if "/" in name else ""
        if not top:  # a loose JSON: what it is, from its keys
            top = "filament" if "filament_type" in cfg or "nozzle_temperature" in cfg else \
                "printer" if "printable_area" in cfg else "process"
        if top in ("printer", "machine"):
            (PRINTER / "printer.json").write_bytes(data)
            got["printer"] = cfg.get("name") or Path(name).stem
        elif top in ("process", "filament"):
            label = (cfg.get("name") or Path(name).stem).strip()
            (PRINTER / top / f"{_safe(label)}.json").write_bytes(data)
            got[top].append(label)
    if not (PRINTER / "printer.json").exists():
        raise ValueError("no printer profile yet: send the printer bundle first")
    old = settings() if not has_printer else {}
    procs, fils = processes(), filaments()
    st = {"bundle": path.name if has_printer else old.get("bundle", path.name), "imported": time.time(),
          "default": old.get("default") if old.get("default") in procs else _pick_default(procs, ("nicee", "nice", "standard")),
          "filament": old.get("filament") if old.get("filament") in fils else _pick_default(fils, ("main", "pla"))}
    (PRINTER / "settings.json").write_text(json.dumps(st))
    log.info("printer presets: %s (defaults %s / %s)", got, st["default"], st["filament"])
    return got


def filaments() -> list[str]:
    return sorted(p.stem for p in (PRINTER / "filament").glob("*.json")) if PRINTER.exists() else []


def _safe(s: str) -> str:
    return re.sub(r"[^\w .@-]", "_", s).strip() or "profile"


def settings() -> dict:
    try:
        return json.loads((PRINTER / "settings.json").read_text())
    except (OSError, ValueError):
        return {}


def processes() -> list[str]:
    return sorted(p.stem for p in (PRINTER / "process").glob("*.json")) if PRINTER.exists() else []


def ready() -> bool:
    return (PRINTER / "printer.json").exists() and command() is not None


# --- complete profiles from the inheritance chain ------------------------------------------------------------
_index: dict[str, dict[str, Path]] = {}


def _system(kind: str) -> dict[str, Path]:
    """Orca's own profiles of a kind (machine / process / filament), by name."""
    if kind not in _index:
        found: dict[str, Path] = {}
        res = resources()
        if res:
            for f in (res / "profiles").glob(f"*/{kind}/*.json"):
                try:
                    name = json.loads(f.read_text()).get("name")
                except (OSError, ValueError):
                    continue
                if name:
                    found.setdefault(name, f)
        _index[kind] = found
    return _index[kind]


def _find(kind: str, name: str) -> Path | None:
    profiles = _system(kind)
    if name in profiles:
        return profiles[name]
    from rapidfuzz import fuzz, process  # Creality Print's name for it is often a little different

    key = lambda s: re.sub(r"[^a-z0-9.@]", "", s.lower().replace("ender-5", "ender5").replace(" nozzle", ""))  # noqa: E731
    best = process.extractOne(key(name), {n: key(n) for n in profiles}, scorer=fuzz.ratio)
    if best and best[1] >= 80:
        log.info("slicer: %r -> Orca's %r", name, best[2])
        return profiles[best[2]]
    return None


def flatten(cfg: dict, kind: str, depth: int = 0) -> dict:
    """cfg with everything it inherits filled in (parents first, cfg's own values on top)."""
    parent = cfg.get("inherits")
    base: dict = {}
    if parent and depth < 12:
        f = _find(kind, parent)
        if f:
            base = flatten(json.loads(f.read_text()), kind, depth + 1)
        else:  # Orca's own profiles are packed (.opc) in newer versions: let OrcaSlicer resolve the base itself
            return {**cfg, "from": "User"}
    out = {**base, **cfg}
    out.pop("inherits", None)
    out["from"] = "User"
    return out


def _filament(printer: dict) -> dict:
    chosen = settings().get("filament")
    if chosen and (PRINTER / "filament" / f"{chosen}.json").exists():
        return flatten(json.loads((PRINTER / "filament" / f"{chosen}.json").read_text()), "filament")
    for name in (printer.get("default_filament_profile") or []) + ["Creality Generic PLA", "Generic PLA"]:
        f = _find("filament", name)
        if f:
            return flatten(json.loads(f.read_text()), "filament")
    return {}


# --- tweaks: "make it strong", "fast but good quality" -> changes to the preset --------------------------------
def _num(v, default=0.0) -> float:
    try:
        return float(str(v[0] if isinstance(v, list) else v).rstrip("%"))
    except (ValueError, TypeError, IndexError):
        return default


def _f(x: float) -> str:
    """40.0 -> "40", 0.12 -> "0.12": how OrcaSlicer writes numbers."""
    return str(int(x)) if float(x).is_integer() else str(round(x, 3))


# "no X", "without X", "don't use X", "remove the X", "turn off X": that setting off
NEG = r"(?:no|without|don'?t (?:use|want|add|do)|do not (?:use|want|add)|remove|turn off|disable|skip|stop using|get rid of)" \
      r"\s+(?:the\s+|any\s+|a\s+)?"
# "don't use fuzzy skin, keep the other changes and slice it again": a change to the LAST slice, then slice
AGAIN = re.compile(r"^(?P<wish>(?:don'?t|do not|no|without|remove|turn off|disable|use|add|keep|change|reduce|increase|"
                   r"lower|raise|set|put|make|switch|go|try|only)\b.*?)[ ,]+(?:and |but |so )?(?:then )?(?:re ?slice|slice)"
                   r"(?: (?:it|that|this|the model|the print|[\w-]+))?(?: again)?$")
FILLERS = re.compile(r"\b(?:uh+|um+|er+|(?<![0-9] )(?<![0-9])mm+|hmm+|like|you know|i mean)\b")
# infill patterns you might name -> OrcaSlicer's value
PATTERNS = {"gyroid": "gyroid", "grid": "grid", "honeycomb": "honeycomb", "3d honeycomb": "3dhoneycomb",
            "lightning": "lightning", "cubic": "cubic", "adaptive cubic": "adaptivecubic", "triangles": "triangles",
            "triangle": "triangles", "lines": "line", "line": "line", "zig zag": "zig-zag", "zigzag": "zig-zag",
            "rectilinear": "zig-zag", "concentric": "concentric", "cross hatch": "crosshatch", "crosshatch": "crosshatch"}
# What a plain settings instruction is made of ("slice it with 0.16 layer height and 20% infill"). Anything left
# over ("…because it takes too long", "…but stronger", "five of them") needs judgement: the print expert.
_PLAIN = re.compile(r"\b(?:with|and|at|a|an|the|of|to|use|using|set|put|add|on|off|no|without|plus|also|please|then|"
                    r"mm|millimet\w*|percent|layers?|layer height|height|infill|fill|density|pattern|walls?|"
                    r"perimeters?|wall loops?|supports?|tree|brim|ironing|iron|fuzzy|skin|"
                    + "|".join(sorted(PATTERNS, key=len, reverse=True)) + r")\b|\d+(?:\.\d+)?\s*%?|%")


def canonical(n: str) -> tuple[str, bool]:
    """(the request as "slice it ...", is it a change to the last slice?). "don't use fuzzy skin and slice it again"
    -> ("slice it again don't use fuzzy skin", True)."""
    n = re.sub(r"\s+", " ", FILLERS.sub(" ", n.lower())).strip()
    n = re.sub(r"^[a-z]{1,2} (?=(?:please )?(?:re ?slice|slice|print)\b)", "", n)
    n = re.sub(r"\b(?:fudgy|fussy|fuzzie|fozzy|foggy)\b", "fuzzy", n)  # how the mic hears "fuzzy"
    n = re.sub(r"\b(0?\.\d{1,2})\s*(?:light|lite|late|lair|leyer|liar)\b", r"\1 layer", n)  # ...and "0.16 layer"
    m = AGAIN.match(n)
    if m:
        return "slice it again " + m.group("wish"), True
    return n, bool(re.match(r"(?:please )?(?:re ?slice|slice) (?:it|that|this|the model|[\w-]+) again\b", n))


# "scale it down to 50 mm and slice it", "i'll resize the mask to 8 cm tall", "make it 80 percent": plain arithmetic
RESIZE = re.compile(r"^(?:[a-z]{1,2} |i'?ll |i will |can you |could you |please |now |just )*(?:scale|resize|shrink|"
                    r"enlarge|make)\s+(?P<what>it|this|that|the model|the last one|.+?)?\s*(?:down |up )?"
                    r"(?:to|by|at)?\s*(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>mm|millimet\w*|cm|centimet\w*|percent|%)"
                    r"(?:\s+(?P<axis>tall|high|height|wide|width|long|length|across|big|size))?(?P<rest>.*)$")


def resize(model: Path, num: float, unit: str, axis: str | None) -> tuple[Path, str]:
    """A scaled copy next to the model ("mask small 50mm.stl"): its biggest side (or its height/width) becomes the
    size you said, or N percent. Returns (the copy, what was done to say)."""
    import trimesh

    m = trimesh.load(str(model), force="mesh")
    ext = [float(v) for v in m.extents]
    if unit.startswith(("percent", "%")):
        f, tag = num / 100, f"{num:g}pct"
    else:
        mm = num * (10 if unit.startswith("c") else 1)
        i = 2 if axis in ("tall", "high", "height") else 0 if axis in ("wide", "width") else \
            1 if axis in ("long", "length") else ext.index(max(ext))
        f, tag = mm / ext[i], f"{mm:g}mm"
    if not 0.01 < f < 100:
        raise ValueError(f"That would make it {f:g} times its size; say it again?")
    m.apply_scale(f)
    out = model.with_name(f"{model.stem} {tag}{'.stl' if model.suffix.lower() == '.obj' else model.suffix}")
    from batsy import inbox

    inbox.made(out)  # ours, not something you sent
    m.export(str(out))
    x, y, z = (round(float(v), 1) for v in m.extents)
    log.info("resized %s x%.3f -> %s (%s x %s x %s mm)", model.name, f, out.name, x, y, z)
    return out, f"Scaled {model.stem.replace('_', ' ')} to {x:g} by {y:g} by {z:g} millimetres"


def explicit(n: str) -> bool:
    """Is this a plain "slice it with <settings>" (or "with <preset>") that needs no thinking? Then Batsy does it
    itself, in seconds, without asking Claude."""
    if RESIZE.match(canonical(n)[0]):
        return True
    t, _ = canonical(n)
    if LAY.search(t):  # "slice the mask lying down", "lay it flat and slice it with 0.16 layer height"
        t = re.sub(r"^(?:and|then|,)\s+(?:then\s+)?", "", re.sub(r"\s+", " ", LAY.sub(" ", t)).strip(" ,"))
        t = re.sub(r"\s+(?:in|with|on)(?: the| its)?$", "", t)
        if re.match(r"(?:please )?(?:re ?slice|slice|print) (?:the |my )?[\w-]+(?: [\w-]+)?$", t) or not t:
            return True
        if not re.match(r"(?:please )?(?:re ?slice|slice|print)\b", t):
            t = "slice it " + t
    t = re.sub(r"\b(?:again|keep|all|other|the|changes?|settings?|rest|same|but|do|don'?t|not|use|reduce|increase|"
               r"lower|raise|change|remove|turn|disable|get|rid|stop|using|want|everything|else)\b", " ", t)
    m = re.match(r"\s*(?:[a-z]{1,2}\s+)?(?:please\s+)?(?:re ?slice|slice|print)\s+(?:it|this|that|the model|the last one|again)\b(?P<w>.*)$", t)
    if not m:
        return False
    w = m.group("w")
    for name in sorted(processes(), key=len, reverse=True):  # "with fast": one of your presets
        w = re.sub(rf"\b{re.escape(name.lower())}\b(?: preset| profile| settings)?", " ", w)
    return not _PLAIN.sub(" ", w).strip(" ,.")


def plan(text: str, base: dict) -> tuple[dict, list[str]]:
    """What you asked for -> (overrides on top of the base process, what changed in words)."""
    t = FILLERS.sub(" ", text.lower())  # "with 0.16 uh layer height": speech fillers mustn't hide the setting
    t = re.sub(r"\b(0?\.\d{1,2})\s*(?:mm\s*)?(?:light|lite|late|lair|leyer|liar|lay)\b", r"\1 layer", t)  # misheard
    t = re.sub(r"\b(?:fudgy|fussy|fuzzie|fozzy|fuzz|foggy)\b", "fuzzy", t)
    o: dict = {}
    has = lambda *ws: any(re.search(rf"\b{w}\b", t) for w in ws)  # noqa: E731
    fast, quality = has("fast", "faster", "quick", "quickly", "speed", "draft", "rush"), \
        has("quality", "fine", "finer", "detail", "detailed", "smooth", "smoother", "nice", "pretty", "clean")
    strong = has("strong", "stronger", "sturdy", "durable", "tough", "solid", "rigid", "functional", "load", "drops?")
    light = has("light", "lighter", "less filament", "cheap", "cheaper", "save filament", "hollow")
    walls, infill = _num(base.get("wall_loops"), 2), _num(base.get("sparse_infill_density"), 15)
    layer = _num(base.get("layer_height"), 0.2)
    if strong:
        o["wall_loops"] = str(max(4, int(walls) + 2))
        o["top_shell_layers"] = str(max(5, int(_num(base.get("top_shell_layers"), 4)) + 1))
        o["bottom_shell_layers"] = str(max(4, int(_num(base.get("bottom_shell_layers"), 3)) + 1))
        o["sparse_infill_density"] = f"{int(max(40, infill))}%"
        o["sparse_infill_pattern"] = "gyroid"
    if light and not strong:
        o["wall_loops"] = "2"
        o["sparse_infill_density"] = "10%"
        o["sparse_infill_pattern"] = "lightning"
        o["top_shell_layers"], o["bottom_shell_layers"] = "3", "3"
    if fast and quality:  # quick, but the outside still looks good
        o["layer_height"] = "0.2"
        o["outer_wall_speed"] = _f(min(60, max(40, _num(base.get("outer_wall_speed"), 40))))
        o["inner_wall_speed"] = _f(max(120, _num(base.get("inner_wall_speed"), 80)))
        o["sparse_infill_speed"] = _f(max(180, _num(base.get("sparse_infill_speed"), 120)))
        o["internal_solid_infill_speed"] = _f(max(150, _num(base.get("internal_solid_infill_speed"), 100)))
        if not strong:
            o["sparse_infill_pattern"] = "gyroid"
            o.setdefault("sparse_infill_density", f"{int(min(infill, 15))}%")
    elif fast:
        o["layer_height"] = "0.28" if has("draft", "very fast", "fastest", "rush") else "0.24"
        o["outer_wall_speed"] = _f(max(80, _num(base.get("outer_wall_speed"), 60)))
        o["inner_wall_speed"] = _f(max(150, _num(base.get("inner_wall_speed"), 80)))
        o["sparse_infill_speed"] = _f(max(200, _num(base.get("sparse_infill_speed"), 120)))
        if not strong:
            o["sparse_infill_pattern"] = "lightning"
            o.setdefault("sparse_infill_density", f"{int(min(infill, 12))}%")
        o["ironing_type"] = "no ironing"
    elif quality:
        o["layer_height"] = _f(0.1 if has("finer", "very fine", "finest") else min(0.12, layer) if layer > 0.12 or
                               has("fine", "detail", "detailed", "smooth", "smoother") else min(layer, 0.16))
        o["outer_wall_speed"] = _f(min(25, _num(base.get("outer_wall_speed"), 40)))
        o["ironing_type"] = "top"
        o["seam_position"] = "back"
    # exact values: "40% infill", "5 walls", "0.12 layers", "no supports", "add a brim"
    m = re.search(r"(\d{1,3})\s*(?:%|percent)?\s*(?:infill|fill)\b|infill (?:of |to |at )?(\d{1,3})\s*(?:%|percent)?", t)
    if m:
        o["sparse_infill_density"] = f"{min(100, int(m.group(1) or m.group(2)))}%"
    m = re.search(r"(\d{1,2})\s*(?:walls?|perimeters?)|(?:walls?|perimeters?) (?:of |to )?(\d{1,2})", t)
    if m:
        o["wall_loops"] = str(min(12, int(m.group(1) or m.group(2))))
    m = re.search(r"(0?\.\d{1,2})\s*(?:mm)?\s*(?:layers?|layer height)|layer height (?:of |to )?(0?\.\d{1,2})", t) or \
        (None if "nozzle" in t else re.search(r"(?<![\d.])(0?\.(?:0[6-9]|[1-3]\d?))(?![\d%])()", t))  # bare "0.16"
    if m:
        o["layer_height"] = str(max(0.06, min(0.32, float(m.group(1) or m.group(2)))))
    if has(f"{NEG}supports?", "supports? off"):
        o["enable_support"] = "0"
    elif has("supports?", "with supports?", "add supports?"):
        o["enable_support"], o["support_type"] = "1", "tree(auto)"
    if has(f"{NEG}brim", "brim off"):
        o["brim_type"] = "no_brim"
    elif has("brim"):
        o["brim_type"], o["brim_width"] = "outer_only", "8"
    if has(f"{NEG}iron(?:ing)?", "ironing off"):
        o["ironing_type"] = "no ironing"
    elif has("ironing", "iron"):
        o["ironing_type"] = "top"
    if has(f"{NEG}fuzzy(?: skin)?", "fuzzy(?: skin)? off"):
        o["fuzzy_skin"] = "none"
    elif has("fuzzy(?: skin)?"):
        o["fuzzy_skin"] = "external"  # the outside only: a textured look, the holes stay true
    m = re.search(rf"\b({'|'.join(PATTERNS)})\b", t)
    if m:
        o["sparse_infill_pattern"] = PATTERNS[m.group(1)]
    names = {"layer_height": "layers", "wall_loops": "walls", "sparse_infill_density": "infill",
             "sparse_infill_pattern": "infill pattern", "top_shell_layers": "top layers", "bottom_shell_layers": "bottom layers",
             "outer_wall_speed": "outer wall speed", "inner_wall_speed": "inner wall speed",
             "sparse_infill_speed": "infill speed", "enable_support": "supports", "brim_type": "brim", "ironing_type": "ironing",
             "fuzzy_skin": "fuzzy skin"}
    notes = []
    for k, v in o.items():
        was_v = base.get(k, "")
        was_v = was_v[0] if isinstance(was_v, list) and was_v else was_v
        same = str(was_v) == v or (_num(was_v, -1) == _num(v, -2) and str(v).rstrip("%").replace(".", "").isdigit())
        if k in names and not same:
            was = base.get(k, "?")
            was = was[0] if isinstance(was, list) and was else was
            pretty = {"1": "on", "0": "off", "no_brim": "off", "outer_only": "on", "top": "on", "no ironing": "off",
                      "external": "on", "none": "off"}.get(v, v)
            notes.append(f"{names[k]} {pretty}" + (f" (was {was})" if was not in ("?", "") else ""))
    return o, notes


SAFE_KEYS = {"layer_height", "wall_loops", "sparse_infill_density", "sparse_infill_pattern", "top_shell_layers",
             "bottom_shell_layers", "outer_wall_speed", "inner_wall_speed", "sparse_infill_speed",
             "internal_solid_infill_speed", "enable_support", "support_type", "brim_type", "brim_width", "ironing_type",
             "seam_position", "infill_wall_overlap", "wall_generator", "top_surface_pattern", "only_one_wall_top",
             "support_top_z_distance", "support_threshold_angle", "support_object_xy_distance", "elefant_foot_compensation",
             "xy_hole_compensation", "xy_contour_compensation", "fuzzy_skin", "top_surface_speed", "initial_layer_speed",
             "travel_speed", "line_width", "outer_wall_acceleration"}
KNOWLEDGE = Path(__file__).with_name("printer_knowledge.md")  # the printer, your presets, problem -> setting


def plan_with_claude(text: str, base: dict, cfg=None) -> tuple[dict, list[str]]:
    """Wishes the rules don't cover ("it has to survive drops", "it's a hook for a heavy bag") -> overrides, only
    from SAFE_KEYS and within sane limits."""
    from batsy import claude_code

    current = {k: base.get(k) for k in sorted(SAFE_KEYS) if k in base}
    notes = KNOWLEDGE.read_text() if KNOWLEDGE.exists() else ""
    prompt = (f"{notes}\n\nA 3D print on the Ender-5 Pro above (0.4 mm nozzle, PLA), sliced in OrcaSlicer. "
              f"The user says: \"{text}\".\n"
              f"Current process settings: {json.dumps(current)}\n"
              f"Answer ONLY with a JSON object of the OrcaSlicer settings to change (keys only from: {sorted(SAFE_KEYS)}), "
              "string values like OrcaSlicer uses (\"40%\", \"5\", \"0.16\", \"gyroid\"), then a key \"why\" with one short "
              "sentence. Keep it printable: layer 0.08-0.28, walls 1-10, speeds 20-250.")
    out = claude_code.run(prompt, "tuning print settings", args=["--model", "sonnet"], timeout=120, cfg=cfg)
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return {}, []
    try:
        got = json.loads(m.group(0))
    except ValueError:
        return {}, []
    why = str(got.pop("why", "")).strip()
    o = {k: str(v) for k, v in got.items() if k in SAFE_KEYS}
    if "layer_height" in o:
        o["layer_height"] = str(max(0.06, min(0.32, _num(o["layer_height"], 0.2))))
    if "wall_loops" in o:
        o["wall_loops"] = str(max(1, min(10, int(_num(o["wall_loops"], 3)))))
    return o, ([why] if why else []) + [f"{k.replace('_', ' ')} {v}" for k, v in o.items()]


def save_process(name: str, base_name: str, overrides: dict) -> str:
    """Keep a tweak as a preset of your own ("save it as strong")."""
    base = json.loads((PRINTER / "process" / f"{base_name}.json").read_text())
    new = {**base, **overrides, "name": name, "print_settings_id": name}
    (PRINTER / "process" / f"{_safe(name)}.json").write_text(json.dumps(new, indent=1))
    return name


# --- slicing ---------------------------------------------------------------------------------------------------
def _process_file(name: str | None) -> tuple[str, Path]:
    names = processes()
    if not names:
        raise ValueError("I don't have any print profiles. Send me your printer bundle first.")
    want = (name or settings().get("default") or DEFAULT_PROCESS).lower()
    pick = next((n for n in names if n.lower() == want), None) or next((n for n in names if want in n.lower()), None)
    if not pick:
        raise ValueError(f"I don't have a profile called {name}. I have {', '.join(names)}.")
    return pick, PRINTER / "process" / f"{pick}.json"


def _estimates(gcode: Path) -> dict:
    head = gcode.read_text(errors="replace")[-60000:] + "\n" + gcode.read_text(errors="replace")[:20000]
    out = {}
    m = re.search(r"estimated printing time \(normal mode\)\s*=\s*([^\n]+)", head)
    if m:
        out["time"] = m.group(1).strip()
    m = re.search(r"total filament used \[g\]\s*=\s*([\d.]+)|filament used \[g\]\s*=\s*([\d.]+)", head)
    if m:
        out["grams"] = float(m.group(1) or m.group(2))
    m = re.search(r"filament used \[mm\]\s*=\s*([\d.]+)", head)
    if m:
        out["metres"] = round(float(m.group(1)) / 1000, 1)
    return out


def slice_model(model: Path, process: str | None = None, overrides: dict | None = None, label: str = "",
                filament_overrides: dict | None = None) -> dict:
    """Slice model with your printer and the process (default "nicee"), plus any tweaks on top.
    Returns {"gcode", "time", "grams", ...}."""
    from batsy import activity

    with activity.work(f"Slicing {model.stem}", "slice", f"Slicing {model.stem} with {process or settings().get('default', 'your preset')} (OrcaSlicer)"):
        return _slice_model(model, process, overrides, label, filament_overrides)


def _slice_model(model: Path, process: str | None = None, overrides: dict | None = None, label: str = "",
                 filament_overrides: dict | None = None) -> dict:
    cmd = command()
    if not cmd:
        raise ValueError("OrcaSlicer isn't installed.")
    if not (PRINTER / "printer.json").exists():
        raise ValueError("I don't have your printer's presets. Send me the printer bundle from Creality Print.")
    pname, pfile = _process_file(process)
    machine = settings().get("machine")
    if machine:  # the printer you really have, from OrcaSlicer's own profiles (your bundle's printer is an S1)
        printer = flatten({"inherits": machine, "name": f"{machine} (Batsy)", "from": "User",
                           "printer_settings_id": f"{machine} (Batsy)"}, "machine")
    else:
        printer = flatten(json.loads((PRINTER / "printer.json").read_text()), "machine")
    # Arc fitting off: OrcaSlicer turned near-straight curves into G2/G3 arcs with radii up to 2 m, and the
    # Ender-5 Pro's Marlin can misread one of those as a full circle and race the head to the bed's edge. That's
    # what shifted tree2 a few mm up (19 Nov). Plain short moves print the same curves safely.
    proc = {**flatten(json.loads(pfile.read_text()), "process"), "enable_arc_fitting": ["0"], **(overrides or {})}
    for cfg in (printer, proc):  # everything is complete now: no "only for printer X" checks against Orca's bundles
        cfg.pop("compatible_printers", None)
        cfg.pop("compatible_printers_condition", None)
    temp = settings().get("nozzle_temp")  # your tested temperature (the temperature tower), over the preset's
    tested = {"nozzle_temperature": [str(temp)], "nozzle_temperature_initial_layer": [str(temp + 5)]} if temp else {}
    fil = {**_filament(printer), **tested, **(filament_overrides or {})}
    for k in ("compatible_printers", "compatible_printers_condition", "compatible_prints", "compatible_prints_condition"):
        fil.pop(k, None)
    with _lock:
        work = PRINTER / "work"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir()
        # OrcaSlicer needs to be told what each file is ("Preset type could not be resolved" otherwise)
        (work / "machine.json").write_text(json.dumps({**printer, "type": "machine"}))
        (work / "process.json").write_text(json.dumps({**proc, "type": "process"}))
        files = f"{work / 'machine.json'};{work / 'process.json'}"
        args = [*cmd, "--load-settings", files]
        if fil:
            (work / "filament.json").write_text(json.dumps({**fil, "type": "filament"}))
            args += ["--load-filaments", str(work / "filament.json")]
        args += ["--arrange", "1", "--orient", "0", "--slice", "0", "--outputdir", str(work), str(model)]
        t0 = time.time()
        p = subprocess.run(args, capture_output=True, text=True, timeout=900, errors="replace")
        gcodes = sorted(work.glob("*.gcode"))
        if not gcodes:
            tail = (p.stderr or p.stdout).strip().splitlines()[-3:]
            log.warning("slicer failed (%s): %s", p.returncode, " | ".join(tail))
            raise ValueError("OrcaSlicer couldn't slice it" + (f": {tail[-1][:160]}" if tail else "."))
        out_dir = model.parent if model.parent.name == "sliced" else model.parent / "sliced"  # (arrangements live there)
        out_dir.mkdir(exist_ok=True)
        dest = out_dir / f"{model.stem} - {pname}{(' ' + label) if label else ''}.gcode"
        shutil.move(str(gcodes[0]), dest)
    result = {"gcode": str(dest), "process": pname + (f" ({label})" if label else ""), "secs": round(time.time() - t0, 1),
              **_estimates(dest)}
    try:  # a picture of the sliced paths, so you can see the slicing
        from batsy import preview

        title = f"{model.stem.replace('_', ' ')} · {result['process']}"
        from batsy import activity

        activity.step("Drawing the slice preview")
        png, paths = preview.both(dest, title)  # (drawn in its own process)
        result["preview"], result["paths"] = str(png), str(paths)
        result["name"] = model.stem.replace("_", " ")
    except Exception as e:
        log.warning("slicer: no preview (%s)", e)
    log.info("sliced %s: %s", model.name, result)
    return result


def hours(t: str) -> float:
    """'16h 45m 3s' / '1d 2h 3m' -> hours."""
    unit = {"d": 24, "h": 1, "m": 1 / 60, "s": 1 / 3600}
    return sum(float(v) * unit[u] for v, u in re.findall(r"(\d+(?:\.\d+)?)\s*([dhms])", t or ""))


def too_long(r: dict, copies: int = 1, limit: float | None = None) -> str:
    """'' unless this print has a time limit (one you gave for it: "under 4 hours") and goes over it."""
    h = hours(r.get("time", ""))
    if not limit or h <= limit:
        return ""
    tip = f" That's over your {limit:g}-hour limit."
    if copies > 1:
        fit = int(limit // (h / copies))
        each = h / copies * fit
        tip += (f" {fit} of them would take about {_say_time(f'{int(each)}h {round(each % 1 * 60)}m')}." if fit >= 1 else
                " Even one is over it: say smaller, or faster.")
    else:
        tip += " Say faster, or a smaller size."
    return tip


def describe(model: Path, r: dict) -> str:
    bits = [f"{model.stem.replace('_', ' ').replace('-', ' ')} is sliced with {r['process']}"]
    if r.get("time"):
        bits.append(f"it prints in about {_say_time(r['time'])}")
    if r.get("grams"):
        bits.append(f"using {round(r['grams'])} grams of filament" + (f", {r['metres']} metres" if r.get("metres") else ""))
    return ", ".join(bits) + ". The G-code is in the sliced folder next to it."


def _say_time(t: str) -> str:
    """'2h 14m 3s' / '1d 2h 3m' -> '2 hours 14 minutes'."""
    units = {"d": "day", "h": "hour", "m": "minute"}
    parts = [f"{n} {units[u]}{'s' if n != '1' else ''}" for n, u in re.findall(r"(\d+)\s*([dhm])\b", t)]
    return " ".join(parts) or t


# --- voice ----------------------------------------------------------------------------------------------------
SLICE = re.compile(r"^(?:please )?(?:slice|re ?slice)(?: (?P<what>it|this|that|the model|the file|this model|that model|"
                   r"the last (?:model|file|one)|.+?))?(?: (?:with|using|on|in) (?:the )?(?P<proc>[a-z0-9 ]+?)(?: profile| settings| preset)?)?"
                   r"(?: please)?$")
HOW_LONG = re.compile(r"^how (?:long|much time) (?:will|would|does) (?:it|this|that) take to print|"
                      r"^how long (?:is|will be) the print|^how much filament (?:will|does) (?:it|this) (?:take|use|need)")
PROFILES = re.compile(r"^(?:which|what) (?:print(?:ing)? )?(?:profiles|presets|settings) (?:do i have|have i got|are there)")


SAVE = re.compile(r"^(?:please )?(?:save|keep|store) (?:it|this|that|these settings|this preset|the settings) as "
                  r"(?:a |my |the )?(?:new )?(?:preset |profile )?(?:called |named )?(?P<name>[a-z0-9 -]{2,30})$")
KICK = re.compile(r"^(?:please )?(?:slice|re ?slice|make|print)\b")


def handle(app, n: str, model_override: Path | None = None) -> str | None:
    # "s uh slice it with 0.16 uh layer height" -> "slice it with 0.16 layer height";
    # "don't use fuzzy skin and slice it again" -> "slice it again don't use fuzzy skin" (on top of the last slice)
    n, again = canonical(n)
    lay = bool(LAY.search(n))
    if lay:  # "slice it lying down": an orientation, not a setting for the slicer to plan
        n = re.sub(r"^(?:and|then|,)\s+(?:then\s+)?", "", re.sub(r"\s+", " ", LAY.sub(" ", n)).strip(" ,")) or "slice it"
        n = re.sub(r"\s+(?:in|with|on)(?: the| its)?$", "", n)
        if not KICK.match(n):
            n = "slice it " + n
    if SHOW.match(n):
        return "Opening the slice in 3D." if show_3d(app) else "I couldn't open the browser."
    if PROFILES.match(n):
        names = processes()
        if not names:
            return "I don't have your printer presets yet. Send me the printer bundle from OrcaSlicer or Creality Print."
        return f"I have {len(names)} print profiles: {', '.join(names)}. {settings().get('default', DEFAULT_PROCESS)} " \
               f"is the default, with {settings().get('filament', 'PLA')}."
    last = getattr(app, "slice_last", None)
    m = RESIZE.match(n)
    if m and (m["what"] or "").strip() in ("", "it", "this", "that", "the model", "the last one") or m and _named_model(m["what"]):
        what = (m["what"] or "").strip()
        model = (_named_model(what) if what not in ("", "it", "this", "that", "the model", "the last one") else None) \
            or _target(app, None)  # the newest model you sent (mask small, which didn't fit), or the copy made last time
        if model is None or model.suffix.lower() not in (".stl", ".obj"):
            return "Which model? Send me an STL first." if model is None else "I can only resize STL or OBJ files."
        try:
            out, said = resize(model, float(m["num"]), m["unit"], m["axis"])
        except Exception as e:
            return f"I couldn't resize it: {e}"
        app.last_file = out
        if re.search(r"\b(?:slice|print)\b", m["rest"] or "") or not re.search(r"\bdon'?t slice|no slicing|just\b", n):
            app._reply(said + ".")
            return handle(app, "slice it", out)  # the resized copy, sliced with your default preset
        return said + "."
    m = SAVE.match(n)
    if m and last and last.get("overrides"):
        name = save_process(m["name"].strip(), last["process"], last["overrides"])
        return f"Saved as a new preset, {name}. Say slice it with {name} any time."
    if HOW_LONG.match(n) and last and last.get("result"):
        return describe(Path(last["model"]), last["result"])
    if not (KICK.match(n) or HOW_LONG.match(n)):
        return None
    if n.startswith("make") and not re.match(r"(?:please )?make (?:it|this|that|the (?:model|print|part|last one))\b", n):
        return None  # "make my discovery playlist", "make a note": not a print tweak (those are "make it …")
    named = _named_model(n)
    # "slice it ...", "print this", "make it stronger", or just "slice": the last model. Anything else names a model.
    generic = re.fullmatch(r"(?:please )?(?:re ?slice|slice|print|make)(?: (?:it|this|that|the model|the last one|"
                           r"the last model|again)\b.*)?", n)
    if not named and not generic and not n.startswith("make"):
        return None  # you named something I don't have ("print a calibration test"): not the last model instead
    # "slice it finer" after "put five on the bed": the arrangement you sliced last, not the single original
    last_model = Path(last["model"]) if last and last.get("model") else None
    model = model_override or named or (last_model if last_model and (n.startswith("make") or (generic and last_model.exists()))
                      else None) or _target(app, None)
    if n.startswith("make") and not (last or model):
        return None  # "make it louder": not about printing
    names = processes()
    proc = next((x for x in sorted(names, key=len, reverse=True) if re.search(rf"\b{re.escape(x.lower())}\b", n)), None)
    relative = (n.startswith("make") or again) and last  # "make it stronger", "slice it again without X": on top of the last
    base_name = proc or (last["process"] if relative else None) or settings().get("default", DEFAULT_PROCESS)
    if model is None:
        return "Which model? Send me an STL or a 3MF first."
    model, prepared = prepare(model, force=lay)  # broken mesh closed; laid down when it stands badly
    if prepared:
        app._reply(prepared)
    try:
        base_name, pfile = _process_file(base_name)
    except ValueError as e:
        return str(e)
    base = {**flatten(json.loads(pfile.read_text()), "process"), **((last or {}).get("overrides", {}) if relative else {})}
    wish = re.sub(r"^(?:please )?(?:re ?slice|slice|make|print)\s*(?:it|this|that|the model)?\s*(?:again\s*)?", "", n)
    if proc:  # "with fast" picked the preset called fast: that's not also a wish to go faster
        wish = re.sub(rf"\s*(?:with|using|on|in|from)?\s*(?:the )?(?:my )?{re.escape(proc.lower())}(?: preset| profile| settings)?\b",
                      " ", wish).strip(" ,")
    overrides, notes = plan(wish, base)
    use_claude = not overrides and len(wish.split()) >= 3 and not SLICE.fullmatch(n)
    if not overrides and n.startswith("make") and not use_claude:
        return None
    label = "custom" if overrides or use_claude else ""
    app._reply(f"Slicing {model.stem.replace('_', ' ')} with {base_name}" + (", tuned: " + "; ".join(notes[:4]) if notes else "") + ".")

    def run():
        o, why = overrides, notes
        try:
            if use_claude:
                o, why = plan_with_claude(wish, base, getattr(app, "cfg", None))
                if why:
                    app.announce("Tuned: " + "; ".join(why[:4]))
            all_o = {**((last or {}).get("overrides", {}) if relative else {}), **o}
            r = slice_model(model, base_name, all_o, label if all_o else "")
            app.slice_last = {"model": str(model), "process": base_name, "overrides": all_o, "result": r}
            from batsy import vault_capture

            vault_capture.sliced(r, str(model))
            show_3d(app)  # the slicing in 3D: turn it around, scrub through the layers
            app.announce(describe(model, r))
        except Exception as e:
            app.announce(str(e))

    threading.Thread(target=run, daemon=True, name="slicing").start()
    return ""


def _named_model(n: str) -> Path | None:
    """A model you named in what you said ("slice the iphone stand strong")."""
    from rapidfuzz import fuzz

    from batsy import received

    # just the name you said: "slice the mask lying down" -> "mask" (matched inside "mask small 50mm")
    m = re.match(r"(?:please )?(?:re ?slice|slice|print|make)\s+(?:the |my |that |this )?(?P<name>.+?)"
                 r"(?:\s+(?:with|using|at|on|in|and|but|again|without)\b.*)?$", n)
    said = m["name"] if m else n
    if said in ("it", "this", "that", "the model", "the last one", "again"):
        return None
    best, score = None, 0
    for r in received.all_rows(limit=300):  # newest first: ties go to the newest
        if Path(r["path"]).suffix.lower() in MODELS and Path(r["path"]).exists():
            sc = max(fuzz.partial_ratio(received.title(r["name"]), n), fuzz.partial_ratio(said, received.title(r["name"]))
                     if len(said) >= 3 else 0)
            if sc > score:
                best, score = Path(r["path"]), sc
    return best if score >= 88 else None


def _target(app, said: str | None) -> Path | None:
    if said and said not in ("it", "this", "that", "the model", "the file", "this model", "that model") \
            and not said.startswith("the last"):
        from batsy import received

        hits = [h for h in received.search(said) if Path(h[1]["path"]).suffix.lower() in MODELS]
        if hits and hits[0][0] >= 70:
            return Path(hits[0][1]["path"])
        return None  # you named a model I don't have: not the last one instead
    for p in (getattr(app, "last_file", None), getattr(getattr(app, "inbox", None), "last", None)):
        if p and Path(p).suffix.lower() in MODELS and Path(p).exists():
            return Path(p)
    from batsy import received

    for r in received.all_rows(limit=200):
        if Path(r["path"]).suffix.lower() in MODELS and Path(r["path"]).exists():
            return Path(r["path"])
    return None


def fix_units(model: Path) -> str | None:
    """A model drawn in metres (tree.STL: 0.07 x 0.07 x 0.08 "mm", far too small to have a single layer) is
    rescaled x1000 in place, as slicers offer to. Returns what was done, to say, or None."""
    if model.suffix.lower() not in (".stl", ".obj"):
        return None
    try:
        import trimesh

        m = trimesh.load(str(model), force="mesh")
        if not len(m.faces) or not 0 < float(max(m.extents)) < 1.0:  # under 1 mm across: never a real print
            return None
        m.apply_scale(1000.0)
        m.export(str(model))
    except Exception as e:
        log.warning("checking %s's units: %s", model.name, e)
        return None
    x, y, z = (round(float(v)) for v in m.extents)
    log.info("%s was in metres: scaled to %s x %s x %s mm", model.name, x, y, z)
    return f"{model.stem} was drawn in metres, so I scaled it up: it's {x} by {y} by {z} millimetres."


# "slice it lying down", "lay it flat", "best orientation": pick the orientation for it
LAY = re.compile(r"\b(?:(?:lying|lie|lay|laid|put) (?:it |this |that )?(?:down|flat|on (?:its|the) (?:back|side|face|front))|"
                 r"(?:in |with )?(?:the )?best orientation|orient(?:ed|ate)? (?:it|this|that)?\s*(?:properly|better|right)?|"
                 r"flat on the bed)\b")


def _placed(m):
    """The mesh sitting on the bed (lowest point at z=0)."""
    m = m.copy()
    m.apply_translation([0, 0, -m.bounds[0][2]])
    return m


def _cost(m) -> tuple[float, float, float]:
    """(how bad, support needed, footprint mm²) for a mesh already on the bed. Support ~ overhang area x its height
    above the bed (works for hollow shells too, whose inside faces point down whichever way up); a tiny footprint
    (standing on spikes) wobbles and gets knocked."""
    from scipy.spatial import ConvexHull

    n, a, c = m.face_normals, m.area_faces, m.triangles_center
    down = (n[:, 2] < -0.707) & (c[:, 2] > 0.5)
    support = float((a[down] * -n[down, 2] * c[down, 2]).sum())
    v = m.vertices[m.vertices[:, 2] < 0.6][:, :2]
    try:
        foot = float(ConvexHull(v).volume) if len(v) >= 3 else 0.0  # (a 2-D hull's "volume" is its area)
    except Exception:
        foot = 0.0
    return support * (1.6 if foot < 20 else 1.0), support, foot


def prepare(model: Path, force: bool = False) -> tuple[Path, str | None]:
    """If the model stands badly (on a tiny footprint, or another way up needs far less support), lay it the best way
    up (mesh tidied on the way). Returns (the model to slice, what was done to say): the original when it's fine."""
    if model.suffix.lower() not in (".stl", ".obj") or (model.stem.endswith(" laid") and not force):
        return model, None
    try:
        import numpy as np
        import trimesh

        m = trimesh.load(str(model), force="mesh")
        if not len(m.faces) or len(m.faces) > 600000:
            return model, None
        c0, s0, f0 = _cost(_placed(m))
        tries = []
        for axis in ([1, 0, 0], [0, 1, 0]):  # the six ways up of its box...
            for ang in (np.pi / 2, np.pi, -np.pi / 2):
                tries.append(trimesh.transformations.rotation_matrix(ang, axis))
        try:  # ...and the ways it can rest on its own hull
            poses, _ = trimesh.poses.compute_stable_poses(m, n_samples=1, threshold=0.02)
            tries += list(poses[:10])
        except Exception:
            pass
        (c1, s1, f1), t = min(((_cost(_placed(m.copy().apply_transform(t))), t) for t in tries), key=lambda x: x[0][0])
        if not (c1 < 0.6 * c0 or (f0 < 20 and f1 >= 40 and c1 < c0)):
            return model, "It's already about the best way up." if force else None
        m.apply_transform(t)
        if not m.is_watertight:  # tidy what can be tidied (OrcaSlicer repairs the rest)
            m.merge_vertices()
            trimesh.repair.fix_normals(m)
            trimesh.repair.fill_holes(m)
        m = _placed(m)
        out = model.with_name(f"{model.stem} laid.stl")
        from batsy import inbox

        inbox.made(out)  # ours, not something you sent
        m.export(str(out))
        log.info("laid %s down -> %s: support %.0f -> %.0f, footprint %.0f -> %.0f mm²", model.name, out.name, s0, s1, f0, f1)
        why = f"stood on a {f0:.0f} square millimetre footprint" if f0 < 20 else "needed a lot of support"
        less = round(100 * (1 - s1 / s0)) if s0 > 0 else 0
        return out, (f"{model.stem.replace('_', ' ')} {why}, so I turned it the best way up: {less} percent less "
                     f"support, standing on {f1:.0f} square millimetres.")
    except Exception as e:
        log.warning("orienting %s: %s", model.name, e)
        return model, None


def on_arrival(app, files: list[Path]) -> None:
    """Called for every batch that arrives: printer bundles become your presets, models get sliced."""
    for f in files:
        if f.suffix.lower() in (".creality_printer", ".orca_printer") or \
                (f.suffix.lower() == ".zip" and re.search(r"preset|profile|process|filament", f.name, re.I)):
            try:
                got = import_bundle(f)
                app.announce(f"Got your printer presets: {got['printer']}, with {', '.join(got['process'])}.")
            except (OSError, ValueError, zipfile.BadZipFile) as e:
                log.warning("printer bundle %s: %s", f.name, e)
    models = [f for f in files if f.suffix.lower() in MODELS]
    for f in models:
        said = fix_units(f)
        if said:
            app.announce(said)
    if not models or not ready():
        return

    def run():
        for model in models:
            try:
                model, said = prepare(model)
                if said:
                    app.announce(said)
                r = slice_model(model)
                app.slice_last = {"model": str(model), "process": r["process"], "overrides": {}, "result": r}
                from batsy import vault_capture

                vault_capture.sliced(r, str(model))
                app.announce(describe(model, r))
            except Exception as e:
                log.warning("auto-slice %s: %s", model.name, e)

    threading.Thread(target=run, daemon=True, name="auto-slice").start()


def show_3d(app=None) -> bool:
    """The newest slice in 3D, in the browser on this laptop (the phone app's viewer page, with the token)."""
    from batsy import phone

    port = (getattr(app, "cfg", None) and app.cfg.get("phone.port")) or 8765
    url = f"http://127.0.0.1:{port}/app/slice.html#t={phone.token()}"
    try:
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return False


SHOW = re.compile(r"^(?:please )?(?:show|open|let me see)(?: me)? (?:the |my )?(?:slic(?:ing|e)|slice preview|preview|"
                  r"toolpaths?|g ?code preview)(?: in 3d)?$|^show (?:me )?(?:it|the print) in 3d$")
