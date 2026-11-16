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


def plan(text: str, base: dict) -> tuple[dict, list[str]]:
    """What you asked for -> (overrides on top of the base process, what changed in words)."""
    t = text.lower()
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
    m = re.search(r"(\d{1,3})\s*(?:%|percent)\s*(?:infill|fill)|infill (?:of |to |at )?(\d{1,3})\s*(?:%|percent)?", t)
    if m:
        o["sparse_infill_density"] = f"{min(100, int(m.group(1) or m.group(2)))}%"
    m = re.search(r"(\d{1,2})\s*(?:walls?|perimeters?)|(?:walls?|perimeters?) (?:of |to )?(\d{1,2})", t)
    if m:
        o["wall_loops"] = str(min(12, int(m.group(1) or m.group(2))))
    m = re.search(r"(0?\.\d{1,2})\s*(?:mm)?\s*(?:layers?|layer height)|layer height (?:of |to )?(0?\.\d{1,2})", t)
    if m:
        o["layer_height"] = str(max(0.06, min(0.32, float(m.group(1) or m.group(2)))))
    if has("no supports?", "without supports?", "supports? off", "remove (?:the )?supports?"):
        o["enable_support"] = "0"
    elif has("supports?", "with supports?", "add supports?"):
        o["enable_support"], o["support_type"] = "1", "tree(auto)"
    if has("no brim", "without (?:a )?brim", "brim off"):
        o["brim_type"] = "no_brim"
    elif has("brim"):
        o["brim_type"], o["brim_width"] = "outer_only", "8"
    if has("ironing", "iron"):
        o["ironing_type"] = "top"
    names = {"layer_height": "layers", "wall_loops": "walls", "sparse_infill_density": "infill",
             "sparse_infill_pattern": "infill pattern", "top_shell_layers": "top layers", "bottom_shell_layers": "bottom layers",
             "outer_wall_speed": "outer wall speed", "inner_wall_speed": "inner wall speed",
             "sparse_infill_speed": "infill speed", "enable_support": "supports", "brim_type": "brim", "ironing_type": "ironing"}
    notes = []
    for k, v in o.items():
        was_v = base.get(k, "")
        was_v = was_v[0] if isinstance(was_v, list) and was_v else was_v
        same = str(was_v) == v or (_num(was_v, -1) == _num(v, -2) and str(v).rstrip("%").replace(".", "").isdigit())
        if k in names and not same:
            was = base.get(k, "?")
            was = was[0] if isinstance(was, list) and was else was
            pretty = {"1": "on", "0": "off", "no_brim": "off", "outer_only": "on", "top": "on", "no ironing": "off"}.get(v, v)
            notes.append(f"{names[k]} {pretty}" + (f" (was {was})" if was not in ("?", "") else ""))
    return o, notes


SAFE_KEYS = {"layer_height", "wall_loops", "sparse_infill_density", "sparse_infill_pattern", "top_shell_layers",
             "bottom_shell_layers", "outer_wall_speed", "inner_wall_speed", "sparse_infill_speed",
             "internal_solid_infill_speed", "enable_support", "support_type", "brim_type", "brim_width", "ironing_type",
             "seam_position", "infill_wall_overlap", "wall_generator", "top_surface_pattern", "only_one_wall_top"}


def plan_with_claude(text: str, base: dict, cfg=None) -> tuple[dict, list[str]]:
    """Wishes the rules don't cover ("it has to survive drops", "it's a hook for a heavy bag") -> overrides, only
    from SAFE_KEYS and within sane limits."""
    from batsy import claude_code

    current = {k: base.get(k) for k in sorted(SAFE_KEYS) if k in base}
    prompt = (f"A 3D print on a Creality Ender-5 S1 (0.4 mm nozzle, PLA), sliced in OrcaSlicer. The user says: \"{text}\".\n"
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
    proc = {**flatten(json.loads(pfile.read_text()), "process"), **(overrides or {})}
    for cfg in (printer, proc):  # everything is complete now: no "only for printer X" checks against Orca's bundles
        cfg.pop("compatible_printers", None)
        cfg.pop("compatible_printers_condition", None)
    fil = {**_filament(printer), **(filament_overrides or {})}
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
        out_dir = model.parent / "sliced"
        out_dir.mkdir(exist_ok=True)
        dest = out_dir / f"{model.stem} - {pname}{(' ' + label) if label else ''}.gcode"
        shutil.move(str(gcodes[0]), dest)
    result = {"gcode": str(dest), "process": pname + (f" ({label})" if label else ""), "secs": round(time.time() - t0, 1),
              **_estimates(dest)}
    try:  # a picture of the sliced paths, so you can see the slicing
        from batsy import preview

        title = f"{model.stem.replace('_', ' ')} · {result['process']}"
        result["preview"] = str(preview.render(dest, dest.with_suffix(".png"), title))
        result["paths"] = str(preview.export(dest, dest.with_suffix(".paths.json"), title))
        result["name"] = model.stem.replace("_", " ")
    except Exception as e:
        log.warning("slicer: no preview (%s)", e)
    log.info("sliced %s: %s", model.name, result)
    return result


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


def handle(app, n: str) -> str | None:
    if SHOW.match(n):
        return "Opening the slice in 3D." if show_3d(app) else "I couldn't open the browser."
    if PROFILES.match(n):
        names = processes()
        if not names:
            return "I don't have your printer presets yet. Send me the printer bundle from OrcaSlicer or Creality Print."
        return f"I have {len(names)} print profiles: {', '.join(names)}. {settings().get('default', DEFAULT_PROCESS)} " \
               f"is the default, with {settings().get('filament', 'PLA')}."
    last = getattr(app, "slice_last", None)
    m = SAVE.match(n)
    if m and last and last.get("overrides"):
        name = save_process(m["name"].strip(), last["process"], last["overrides"])
        return f"Saved as a new preset, {name}. Say slice it with {name} any time."
    if HOW_LONG.match(n) and last and last.get("result"):
        return describe(Path(last["model"]), last["result"])
    if not (KICK.match(n) or HOW_LONG.match(n)):
        return None
    named = _named_model(n)
    # "slice it ...", "print this", "make it stronger", or just "slice": the last model. Anything else names a model.
    generic = re.fullmatch(r"(?:please )?(?:re ?slice|slice|print|make)(?: (?:it|this|that|the model|the last one|"
                           r"the last model|again)\b.*)?", n)
    if not named and not generic and not n.startswith("make"):
        return None  # you named something I don't have ("print a calibration test"): not the last model instead
    model = named or (Path(last["model"]) if last and n.startswith("make") else None) or _target(app, None)
    if n.startswith("make") and not (last or model):
        return None  # "make it louder": not about printing
    names = processes()
    proc = next((x for x in sorted(names, key=len, reverse=True) if re.search(rf"\b{re.escape(x.lower())}\b", n)), None)
    relative = n.startswith("make") and last  # "make it stronger": on top of what you sliced last
    base_name = proc or (last["process"] if relative else None) or settings().get("default", DEFAULT_PROCESS)
    if model is None:
        return "Which model? Send me an STL or a 3MF first."
    try:
        base_name, pfile = _process_file(base_name)
    except ValueError as e:
        return str(e)
    base = {**flatten(json.loads(pfile.read_text()), "process"), **((last or {}).get("overrides", {}) if relative else {})}
    wish = re.sub(r"^(?:please )?(?:re ?slice|slice|make|print)\s*(?:it|this|that|the model)?\s*", "", n)
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

    best, score = None, 0
    for r in received.all_rows(limit=300):
        if Path(r["path"]).suffix.lower() in MODELS and Path(r["path"]).exists():
            sc = fuzz.partial_ratio(received.title(r["name"]), n)
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
    if not models or not ready():
        return

    def run():
        for model in models:
            try:
                r = slice_model(model)
                app.slice_last = {"model": str(model), "process": r["process"], "overrides": {}, "result": r}
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
