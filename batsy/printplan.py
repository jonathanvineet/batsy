"""Print plans in plain words: how many copies, what size, and what it'll take.

  "I need five dice on the print" · "5 of them" · "fill the bed with them"           -> copies, arranged on the bed
  "scale it to 20 mm" · "50 percent" · "half size" · "make them 3 cm"              -> size
  "it's huge, scale it down" (no number)                                            -> Claude says what's sensible
                                                                                       for the thing ("a die: 16-20 mm")
  "what's the size", "how long will all five take"                                  -> answered after slicing

The copies are laid out in a grid on the 220 x 220 bed (6 mm apart) as one file, sliced together, so the time and
grams are for all of them. "Print it" afterwards prints that.
"""
import json
import logging
import math
import re
import threading
from pathlib import Path

log = logging.getLogger("batsy.printplan")
BED, MARGIN, GAP = (220.0, 220.0, 300.0), 8.0, 6.0
NUM = {"one": 1, "a": 1, "single": 1, "two": 2, "a couple": 2, "couple": 2, "three": 3, "four": 4, "five": 5, "six": 6,
       "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "a dozen": 12, "dozen": 12,
       "fifteen": 15, "twenty": 20}
N = r"(?P<n>\d{1,2}|" + "|".join(sorted(NUM, key=len, reverse=True)) + r")"
THINGS = r"(?:copies|copy|of (?:them|it|those|these)|dice|dices|dies|die|pieces|parts|ones|of each)"  # (not "times": "two times bigger" is a size)
COUNT = [re.compile(r"\b" + N + r" " + THINGS + r"\b"),
         re.compile(r"\b(?:print|make|need|want|put|fit|place) (?:me )?" + N + r"(?: of (?:them|it|those))?\b(?! (?:mm|cm|percent|%))"),
         re.compile(r"\b" + N + r" (?:\w+ ){0,2}on (?:the|one|a) (?:bed|plate|print|build plate)\b")]
FILL = re.compile(r"\bfill (?:the|my) (?:whole )?(?:bed|plate|build plate)\b|\bas many as (?:fit|will fit|possible)\b")
SIZE = re.compile(r"\b(?:to|at|make (?:it|them|each|each one)|scale (?:it |them )?(?:down |up )?to|about|around)?\s*"
                  r"(?P<v>\d+(?:\.\d+)?) ?(?P<u>mm|millimet(?:er|re)s?|cm|centimet(?:er|re)s?|percent|%)(?: (?:big|wide|tall|"
                  r"across|long|high))?(?!\w)")
HALF = re.compile(r"\b(?:half|half the) size\b|\bscale (?:it |them )?(?:down )?by half\b")
SHRINK = re.compile(r"\b(?:scale (?:it|them)? ?down|make (?:it|them) smaller|smaller|too big|huge|massive|enormous|"
                    r"way too large|too large|shrink)\b")
ASKS = re.compile(r"\b(?:size|how big|how long|time|how much (?:time|filament|plastic)|grams|weight)\b")


def understand(n: str) -> dict | None:
    """What the request asks for: {copies, fill, target_mm | scale | shrink}. None if it's not about that."""
    plan: dict = {}
    for rx in COUNT:
        m = rx.search(n)
        if m:
            w = m.group("n")
            c = int(w) if w.isdigit() else NUM.get(w, 0)
            if 1 < c <= 50:
                plan["copies"] = c
                break
    if FILL.search(n):
        plan["fill"] = True
    m = re.search(r"\b(?:under|within|in less than|less than|at most|max(?:imum)?|no more than) (?P<h>\d+(?:\.\d+)?|"
                  + "|".join(NUM) + r")(?: (?:or|to) \d+)? ?(?:hours?|hrs?|h)\b", n)
    if m:  # a time limit for this print only
        w = m.group("h")
        plan["max_hours"] = float(w) if w[0].isdigit() else float(NUM.get(w, 0)) or None
    m = SIZE.search(n)
    if m:
        v, u = float(m.group("v")), m.group("u")
        if u in ("percent", "%"):
            plan["scale"] = v / 100
        else:
            plan["target_mm"] = v * (10 if u.startswith("c") else 1)
    elif re.search(r"\bscale (?:it|them|this|that)? ?(?:down |up )?to (\d+(?:\.\d+)?)\b(?! ?(?:x|times))", n):
        plan["target_mm"] = float(re.search(r"\bto (\d+(?:\.\d+)?)\b", n).group(1))  # "scale it to 20": millimetres
    elif HALF.search(n):
        plan["scale"] = 0.5
    elif SHRINK.search(n):
        plan["shrink"] = True
    if not plan:
        return None
    plan["asks"] = bool(ASKS.search(n))
    return plan


def sensible_size(name: str, size: list[float], cfg) -> tuple[float, str]:
    """'AKTP Dice', 96 mm -> (20, 'a standard die is 16 to 20 mm'): Claude knows what things usually measure."""
    try:
        from batsy import claude_code

        out = claude_code.run(
            f"A 3D model file is called {name!r} and measures {size[0]:.0f} x {size[1]:.0f} x {size[2]:.0f} mm. The "
            "user says it's too big and wants it scaled down to a sensible real-world size for printing. Reply with "
            'JSON only: {"longest_mm": <number>, "why": "<at most 10 words, e.g. a standard die is 16-20 mm>"}',
            "print size", cfg=cfg, timeout=90)
        d = json.loads(re.search(r"\{.*\}", out or "", re.S).group(0))
        mm = float(d["longest_mm"])
        if 2 <= mm < max(size):
            return mm, str(d.get("why", ""))
    except Exception as e:
        log.info("no size suggestion from Claude: %s", e)
    return round(max(size) / 2), "half its size"


def layout(each_xy: tuple[float, float], copies: int | None) -> tuple[int, int, int]:
    """(copies, columns, rows) on the bed; copies=None: as many as fit. Negative copies: only that many fit."""
    ux, uy = BED[0] - 2 * MARGIN, BED[1] - 2 * MARGIN
    cols_max = max(1, int((ux + GAP) // (each_xy[0] + GAP)))
    rows_max = max(1, int((uy + GAP) // (each_xy[1] + GAP)))
    fit = cols_max * rows_max
    n = fit if copies is None else copies
    if n > fit:
        return -fit, cols_max, rows_max
    cols = min(cols_max, max(1, math.ceil(math.sqrt(n * (each_xy[1] + GAP) / (each_xy[0] + GAP)))))
    while math.ceil(n / cols) > rows_max and cols < cols_max:
        cols += 1
    return n, cols, math.ceil(n / cols)


def prepare(model: Path, plan: dict, cfg) -> tuple[Path, dict]:
    """The scaled copies, arranged, as one STL next to the model's slices. Raises ValueError if they can't fit."""
    import trimesh

    m = trimesh.load(str(model), force="mesh")
    size = [float(v) for v in m.extents]
    scale, why = plan.get("scale") or 1.0, ""
    if plan.get("target_mm"):
        scale = plan["target_mm"] / max(size)
    elif plan.get("shrink"):
        target, why = sensible_size(model.stem.replace("_", " "), size, cfg)
        scale = target / max(size)
    if abs(scale - 1) > 1e-3:
        m.apply_scale(scale)
    each = [float(v) for v in m.extents]
    if each[2] > BED[2]:
        raise ValueError(f"At {each[2]:.0f} millimetres tall it's taller than the printer can print.")
    copies = plan.get("copies") or (None if plan.get("fill") else 1)
    n, cols, rows = layout((each[0], each[1]), copies)
    if n < 0:
        raise ValueError(f"Only {-n} fit on the bed at {max(each):.0f} millimetres each. Say a smaller size, or fewer.")
    m.apply_translation(-m.bounds[0])
    parts = []
    for i in range(n):
        c = m.copy()
        c.apply_translation([(i % cols) * (each[0] + GAP), (i // cols) * (each[1] + GAP), 0])
        parts.append(c)
    whole = trimesh.util.concatenate(parts)
    whole.apply_translation([BED[0] / 2 - whole.extents[0] / 2, BED[1] / 2 - whole.extents[1] / 2, 0])
    out_dir = model.parent / "sliced"
    out_dir.mkdir(exist_ok=True)
    tag = f"x{n} {max(each):.0f}mm" if n > 1 else f"{max(each):.0f}mm"
    out = out_dir / f"{model.stem} ({tag}).stl"
    whole.export(str(out))
    return out, {"copies": n, "each": [round(v, 1) for v in each], "was": [round(v, 1) for v in size],
                 "scale": round(scale, 3), "why": why, "grid": (cols, rows)}


def _mm(v: float) -> str:
    return f"{v:.0f}" if v >= 10 else f"{v:.1f}"


def run(app, model: Path, plan: dict) -> None:
    from batsy import slicer

    try:
        if plan.get("shrink"):
            app.announce("Working out a sensible size for it.")
        path, info = prepare(model, plan, app.cfg)
        was, each, n = info["was"], info["each"], info["copies"]
        thing = model.stem.replace("_", " ").replace("-", " ")
        said = []
        if info["scale"] != 1:
            why = info["why"].rstrip(".")
            said.append(f"Each one was {_mm(max(was))} millimetres" + (f"; {why[:1].lower() + why[1:]}" if why else "") +
                        f". So I've scaled it to {_mm(each[0])} by {_mm(each[1])} by {_mm(each[2])} millimetres")
        else:
            said.append(f"Each one is {_mm(each[0])} by {_mm(each[1])} by {_mm(each[2])} millimetres")
        if n > 1:
            said.append(f"{n} fit on the bed in a {info['grid'][0]} by {info['grid'][1]} grid")
        app.announce(". ".join(said) + ". Slicing now.")
        r = slicer.slice_model(path, label=f"x{n}" if n > 1 else "")
        app.slice_last = {"model": str(path), "process": r["process"], "overrides": {}, "result": r}
        slicer.show_3d(app)
        many = f"All {n}" if n > 1 else thing.capitalize()
        app.announce(f"{many}: about {slicer._say_time(r['time'])} and {round(r.get('grams') or 0)} grams of "
                     "filament." + (slicer.too_long(r, n, plan.get("max_hours")) or " Say print it when the bed is clear."))
    except ValueError as e:
        app.announce(str(e))
    except Exception as e:
        log.exception("print plan")
        app.announce(f"I couldn't set that up: {e}")


MODEL_EXTS = (".stl", ".3mf", ".obj", ".step", ".stp")


def _received_models() -> list[Path]:
    """3D files you were sent, newest first (not Batsy's own slices and arrangements)."""
    from batsy.config import Config

    try:
        inbox = Path(Config.load().get("files.inbox")).expanduser()
    except Exception:
        inbox = Path.home() / "Batsy" / "inbox"
    files = [p for p in inbox.rglob("*") if p.suffix.lower() in MODEL_EXTS and "sliced" not in p.parts]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def target(app, n: str = "") -> Path | None:
    """The model meant: one named in the request ("the dice file"), else the one you just sent or sliced last,
    else the newest 3D file you received (after a restart Batsy has no "last one" yet)."""
    words = set(re.findall(r"[a-z]{3,}", n)) - {"the", "file", "model", "print", "scale", "down", "put", "five", "them",
                                               "and", "bed", "huge", "smaller", "copies", "size"}
    if words:
        for p in _received_models():
            name = set(re.findall(r"[a-z]{3,}", p.stem.lower()))
            if words & name or any(w.rstrip("s") in name or w + "s" in name for w in words):
                return p
    for p in (getattr(app, "last_file", None), (getattr(app, "slice_last", None) or {}).get("model")):
        if p and Path(p).suffix.lower() in (".stl", ".3mf", ".obj", ".step", ".stp"):
            p = Path(p)
            if p.parent.name == "sliced" and " (" in p.stem:  # one of these arrangements: plan from the original
                orig = p.parent.parent / (p.stem.split(" (")[0] + ".stl")
                p = orig if orig.exists() else p
            if p.exists():
                return p
    recent = _received_models()
    return recent[0] if recent else None


def handle(app, n: str, raw: str = "") -> str | None:
    if raw:  # the words as said: Batsy's filler stripping also eats "mm" ("20 mm" -> "20")
        n = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9.%' ]", " ", raw.lower())).strip()
    plan = understand(n)
    if not plan:
        return None
    printy = r"\b(?:print|prints|printed|bed|plate|model|stl|scale|scaled|size|dice|dies|die|object|part|parts|copies)\b"
    if plan.get("copies") or plan.get("fill"):  # "five of them", "fill the bed"
        if not re.search(printy + r"|\b(?:of them|of it|of those)\b", n):
            return None
    elif not re.search(printy, n):  # size alone: "turn it up to 40 percent" is the volume, not the model
        return None
    model = target(app, n)
    if model is None:
        return None
    threading.Thread(target=run, args=(app, model, plan), daemon=True, name="print-plan").start()
    bits = []
    if plan.get("copies"):
        bits.append(f"{plan['copies']} of them")
    if plan.get("fill"):
        bits.append("as many as fit")
    if plan.get("target_mm"):
        bits.append(f"{_mm(plan['target_mm'])} millimetres each")
    elif plan.get("scale"):
        bits.append(f"at {round(plan['scale'] * 100)} percent")
    elif plan.get("shrink"):
        bits.append("smaller")
    return f"Got it: {', '.join(bits)}." if bits else "Got it."
