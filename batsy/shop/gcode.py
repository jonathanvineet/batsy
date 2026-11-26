"""How much filament a G-code has used by a given point: the loss when a print is stopped part-way.

The print streamer (batsy/printstream.py) counts the same lines this does: each line with its comment cut off,
stripped, empty ones dropped. So "line 41 203 of 120 000" there is index 41 203 here.

Extrusion is followed through absolute (M82) and relative (M83) E, and G92 E resets. Grams from millimetres of
1.75 mm filament x the material's density. The cumulative table is cached next to the G-code (.euse.json), sampled
every 200 lines, which is plenty for a cost.
"""
import json
import math
import re
from pathlib import Path

DENSITY = {"PLA": 1.24, "PETG": 1.27, "ABS": 1.04, "ASA": 1.07, "TPU": 1.21, "NYLON": 1.14}
STEP = 200
_E = re.compile(r"\bE(-?\d*\.?\d+)")
_G = re.compile(r"^G([0123])\b")


def _table(gcode: Path) -> dict:
    cache = gcode.with_suffix(".euse.json")
    try:
        if cache.stat().st_mtime >= gcode.stat().st_mtime:
            return json.loads(cache.read_text())
    except (OSError, ValueError):
        pass
    rel, e_abs, last, total, n, marks = False, 0.0, 0.0, 0.0, 0, []
    with open(gcode, errors="replace") as f:
        for raw in f:
            line = raw.split(";", 1)[0].strip()
            if not line:
                continue
            u = line.upper()
            if u.startswith("M83"):
                rel = True
            elif u.startswith("M82"):
                rel = False
            elif u.startswith("G92"):
                m = _E.search(u)
                if m:
                    last = float(m.group(1))
            elif _G.match(u):
                m = _E.search(u)
                if m:
                    v = float(m.group(1))
                    d = v if rel else v - last
                    if not rel:
                        last = v
                    if d > 0:
                        total += d
            if n % STEP == 0:
                marks.append(round(total, 2))
            n += 1
    marks.append(round(total, 2))
    out = {"lines": n, "total_mm": round(total, 2), "marks": marks}
    try:
        cache.write_text(json.dumps(out))
    except OSError:
        pass
    return out


def mm_to_grams(mm: float, material: str = "PLA", diameter: float = 1.75) -> float:
    area = math.pi * (diameter / 2) ** 2  # mm²
    return mm * area / 1000 * DENSITY.get((material or "PLA").upper(), 1.24)


def used(gcode: str | Path, line: int | None = None, fraction: float | None = None, material: str = "PLA") -> dict:
    """{grams, mm, fraction}: filament used up to `line` (or the fraction of lines), and of the whole print."""
    t = _table(Path(gcode))
    if line is None:
        line = round((fraction if fraction is not None else 1.0) * t["lines"])
    line = max(0, min(int(line), t["lines"]))
    marks = t["marks"]
    i = line // STEP
    lo = marks[min(i, len(marks) - 1)]
    hi = marks[min(i + 1, len(marks) - 1)]
    mm = lo + (hi - lo) * ((line % STEP) / STEP)
    return {"mm": round(mm, 1), "grams": round(mm_to_grams(mm, material), 1),
            "total_grams": round(mm_to_grams(t["total_mm"], material), 1),
            "fraction": round(mm / t["total_mm"], 4) if t["total_mm"] else 0.0, "line": line, "lines": t["lines"]}
