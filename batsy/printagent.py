"""Batsy's print expert: talking a print through like a person would, not by rules.

Planning a print ("I need five of these", "16 hours is too much", "I want it in better quality", "it's huge")
goes to Claude with real tools (the `batsy print` command, below) and everything it needs to know: your printer,
your presets, the model's real size, what was sliced last and the conversation so far. It works out what you
mean, notices what matters (a die the size of a fist, a print that runs all night, quality vs time), slices to
check its plan, and tells you the result and the trade-off in a sentence or two; it asks only when there's a real
choice to make. Follow-ups ("make it faster", "no, smaller") stay with it.

  batsy print info [MODEL]       printer, presets (key settings), recent models with sizes, the last slice
  batsy print slice MODEL [--copies N] [--size MM | --scale F] [--process NAME] [--set key=value ...]
                                 scale + arrange copies on the bed, slice, report time / grams (JSON); it becomes
                                 the slice "print it" prints
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

from batsy.config import DATA_DIR

LAST = DATA_DIR / "last_slice.json"

# planning, as opposed to plain printer control ("pause the print", "preheat", "how's the printer")
PLANNING = re.compile(
    r"\b(?:copies|of them|of these|of those|on the bed|fill the bed|scale|scaled|size|smaller|bigger|larger|huge|tiny|"
    r"quality|fine|finer|detail|detailed|smooth|smoother|strong|stronger|sturdy|fast|faster|quicker|quick|layer|layers|"
    r"walls?|infill|ironing|supports?|brim|orientation|lay it|flip it|hours?|too long|takes? (?:too )?long|time|grams|"
    r"filament|cheaper|less plastic|tweak|settings?|preset|profile|dice|dies|die|resolution|mm|millimet)\b")
PRINTY = re.compile(r"\b(?:print|prints|printed|printing|slice|sliced|slicing|model|models|stl|bed|dice|dies|die|"
                    r"plate|copies|filament|layers?|infill|printer)\b")
NOT_PRINT = re.compile(r"\b(?:what(?:'s| is) the time|what time|volume|music|song|light|alarm|timer|remind)\b")
CONTROL = re.compile(r"\b(?:pause|resume|cancel|stop the print|preheat|cool|home the printer|bed temp|nozzle temp|"
                     r"how'?s the printer|printer status|motors? off|what'?s printing|print it|print that|print them)\b")


def wants(n: str, app=None) -> bool:
    """Is this about planning a print (for the expert), rather than a plain printer command?"""
    from batsy import slicer

    if slicer.explicit(n):  # "slice it with 0.16 layer height": an instruction, not a question; Batsy does it
        return False
    if CONTROL.search(n) and not re.search(r"\b(?:but|faster|slower|quality|smaller|bigger|instead)\b", n):
        return False
    if NOT_PRINT.search(n) or re.match(r"(?:hey |ok |so )?(?:batsy )?(?:please )?(?:remember|don'?t forget|keep in mind|"
                                        r"note (?:that|down)|jot|write down|i(?:'m| am) (?:learning|studying))\b", n):
        return False  # a note to keep (even about 3D design): memory and the brain, not the print expert
    if app is not None and time.time() - getattr(app, "print_asked_t", 0) < 240 and len(n.split()) <= 25 \
            and not re.match(r"(?:stop|cancel|never ?mind|forget it)\b", n):
        return True  # it just asked you something ("want three instead?"): this is the answer
    recent = app is not None and time.time() - getattr(app, "print_talk_t", 0) < 300  # mid-conversation about a print
    sliced = recall()  # or something was sliced in the last two hours (survives a restart): "make it 50 mm" is about it
    recent = recent or bool(sliced and time.time() - (sliced.get("t") or 0) < 7200)
    return bool(PLANNING.search(n) and (PRINTY.search(n) or recent and re.search(r"\b(?:it|them|this|that|those|these|all)\b", n)))


BRIEF = """You are Batsy's 3D printing expert, talking with Jonathan (an engineering student, he knows his way \
around a printer) by voice. Think like a friend who prints a lot, not like a form: work out what he really \
wants, notice what matters, and say it naturally.

He said: "{request}"

His printer, presets, recent models and the last slice:
{info}

Tools (Bash): `batsy print info [MODEL]` and `batsy print slice MODEL [--copies N] [--size MM] [--scale F] \
[--process NAME] [--set key=value ...]` (scales the longest side to --size, arranges copies on the bed, slices with \
his printer + preset + your --set overrides, prints JSON with hours, grams and sizes; that slice becomes the one \
"print it" prints). Settings use OrcaSlicer keys, e.g. layer_height=0.12, wall_loops=3, sparse_infill_density=15%, \
sparse_infill_pattern=gyroid, ironing_type=top, outer_wall_speed=40, enable_support=1, brim_width=5.
How to work:
- Use common sense about the object: a die is about 16-20 mm, a keychain 40-60 mm, a phone stand fits a phone. \
If a model is clearly the wrong size for what it is, say so and fix it (or ask, if you can't tell).
- Weigh quality against time and plastic like a person would. Long prints are fine if he wants them; if he \
complained about time, find the real fix (smaller, fewer copies, a sensible layer height, less infill on small \
parts) and say what it costs in quality. If he wants better quality, pick settings that actually show on that \
object (finer layers for curves and small text, ironing for flat tops, slower outer walls), not everything at once.
- Slice to check your plan before you answer; try an alternative if the first one misses what he asked for.
- Never print anything yourself, never change his saved presets, never run git.
Reply as Batsy, out loud: one to three short, natural sentences (what you did, the time and grams, the trade-off \
or one question if there's a real choice). No markdown, no lists, no settings jargon unless it helps.
{context}"""


def ask(request: str, context: str = "", on_text=None, cfg=None, timeout: int = 600) -> str:
    """Claude, with the print tool, on this request (and the conversation so far)."""
    from batsy import claude_code

    info = _info(None)
    return claude_code.run(BRIEF.format(request=request, info=json.dumps(info)[:6000], context=context),
                           "print: " + request[:40], cwd=Path.home() / "Batsy", timeout=timeout, on_text=on_text,
                           cfg=cfg, args=["--allowedTools", "Read,Bash(batsy print:*)", "--add-dir",
                                          str(Path.home() / "Batsy")])


# --- the tool ---------------------------------------------------------------------------------------------------------
KEYS = ("layer_height", "wall_loops", "top_shell_layers", "bottom_shell_layers", "sparse_infill_density",
        "sparse_infill_pattern", "ironing_type", "enable_support", "support_type", "brim_width", "outer_wall_speed",
        "inner_wall_speed", "sparse_infill_speed", "travel_speed", "seam_position", "fuzzy_skin")


def _brief_preset(name: str) -> dict:
    from batsy import slicer

    try:
        _, f = slicer._process_file(name)
        p = slicer.flatten(json.loads(f.read_text()), "process")
        return {k: (p[k][0] if isinstance(p.get(k), list) and len(p[k]) == 1 else p.get(k)) for k in KEYS if k in p}
    except Exception:
        return {}


def _model_info(path: Path) -> dict:
    import trimesh

    from batsy import printweight

    m = trimesh.load(str(path), force="mesh")
    est = printweight.estimate(m)
    return {"name": path.stem, "path": str(path), "size_mm": [round(float(v), 1) for v in m.extents],
            "triangles": int(len(m.faces)), "watertight": bool(m.is_watertight), "est_print_g": est["print_g"]}


def _info(model: str | None) -> dict:
    from batsy import printplan, slicer

    s = slicer.settings()
    out = {"printer": {"machine": s.get("machine"), "bed_mm": [220, 220, 300], "nozzle_mm": 0.4,
                       "filament": s.get("filament"), "nozzle_temp_c": s.get("nozzle_temp"),
                       "default_preset": s.get("default")},
           "presets": {p: _brief_preset(p) for p in slicer.processes()},
           "recent_models": [], "last_slice": recall()}
    for p in printplan._received_models()[:6]:
        try:
            out["recent_models"].append(_model_info(p))
        except Exception:
            out["recent_models"].append({"name": p.stem, "path": str(p)})
    if model:
        out["model"] = _model_info(Path(model).expanduser())
    return out


def remember(d: dict) -> None:
    """The newest slice, shared with the running Batsy ("print it" prints this one)."""
    LAST.write_text(json.dumps({**d, "t": time.time()}))


def recall() -> dict | None:
    try:
        d = json.loads(LAST.read_text())
        r = d.get("result") or {}
        return {"model": d.get("model"), "process": d.get("process"), "overrides": d.get("overrides"),
                "copies": d.get("copies"), "each_mm": d.get("each_mm"), "hours": d.get("hours"),
                "time": r.get("time"), "grams": r.get("grams"), "gcode": r.get("gcode"), "t": d.get("t"), "result": r}
    except (OSError, ValueError):
        return None


def _slice(a) -> dict:
    from batsy import printplan, slicer
    from batsy.config import Config

    model = Path(a.model).expanduser()
    if not model.exists():
        raise SystemExit(json.dumps({"error": f"no such file: {model}"}))
    overrides = {}
    for kv in a.set or []:
        k, _, v = kv.partition("=")
        overrides[k.strip()] = [v.strip()]
    path, info = model, {"copies": 1}
    if a.copies > 1 or a.size or a.scale:
        plan = {"copies": a.copies, **({"target_mm": a.size} if a.size else {}), **({"scale": a.scale} if a.scale else {})}
        path, info = printplan.prepare(model, plan, Config.load())
    r = slicer.slice_model(path, a.process, overrides, "x" + str(info["copies"]) if info["copies"] > 1 else
                           ("custom" if overrides else ""))
    out = {"model": str(path), "original": str(model), "process": r["process"], "overrides": overrides,
           "copies": info["copies"], "each_mm": info.get("each"), "hours": round(slicer.hours(r.get("time", "")), 2),
           "time": r.get("time"), "grams": r.get("grams"), "metres": r.get("metres"), "gcode": r.get("gcode")}
    remember({"model": str(path), "process": r["process"], "overrides": overrides, "copies": info["copies"],
              "each_mm": info.get("each"), "hours": out["hours"], "result": r})
    return out


def cli(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="batsy print")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("info")
    i.add_argument("model", nargs="?")
    s = sub.add_parser("slice")
    s.add_argument("model")
    s.add_argument("--copies", type=int, default=1)
    s.add_argument("--size", type=float, help="longest side, mm")
    s.add_argument("--scale", type=float, help="factor, e.g. 0.5")
    s.add_argument("--process", help="preset name")
    s.add_argument("--set", action="append", help="key=value (OrcaSlicer setting)")
    a = p.parse_args(argv)
    try:
        print(json.dumps(_info(a.model) if a.cmd == "info" else _slice(a), indent=1))
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
