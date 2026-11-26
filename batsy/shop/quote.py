"""The virtual slicer: a customer's file in, everything about printing it out, before anyone says yes.

1. Instant (a second or two): the mesh's size, whether it fits the bed (220 x 220 x 300), its volume, the grams
   (walls + infill, batsy/printweight.py) and a time from what this printer really does in grams per hour (learned
   from every real slice so far). Price from that.
2. Real (in the background, one at a time): OrcaSlicer with your printer and presets via batsy/slicer.py, with the
   changes asked for (material, layer height, infill, walls, supports, brim, scale, orientation). Exact grams, time,
   a picture of the toolpaths, and the price again from those.

The same settings are what the job is printed with: confirming the order queues exactly what was quoted, and
changing a setting later re-slices it.

Options (all optional):
  material PLA|PETG|ABS|ASA|TPU   color   process (a preset name)   layer_height 0.08-0.32   infill 0-100 (%)
  walls 1-10   supports auto|on|off   brim auto|on|off   scale_pct 1-1000   orient as_sent|auto   per_plate n
  design_hours (modelling work, charged by the hour)
"""
import hashlib
import json
import logging
import queue
import re
import shutil
import threading
import time
from pathlib import Path

from batsy.shop import costing, db

log = logging.getLogger("batsy.shop.quote")
BED = (220.0, 220.0, 300.0)
MODELS = (".stl", ".3mf", ".obj", ".step", ".stp")
GPH_DEFAULT = 11.0  # grams per hour at 0.2 mm on the Ender-5 Pro, until real slices say otherwise
FILAMENT_TEMPS = {"PLA": (200, 60), "PETG": (235, 75), "ABS": (240, 95), "ASA": (245, 95), "TPU": (220, 50)}


# --- options -> OrcaSlicer ----------------------------------------------------------------------------------------
def clean_options(o: dict | None) -> dict:
    o = dict(o or {})
    out = {"material": str(o.get("material") or "PLA").upper(), "color": str(o.get("color") or "")[:40]}

    def num(k, lo, hi, cast=float):
        v = o.get(k)
        if v in (None, ""):
            return
        try:
            out[k] = cast(min(max(float(v), lo), hi))
        except (TypeError, ValueError):
            pass

    num("layer_height", 0.06, 0.32)
    num("infill", 0, 100, int)
    num("walls", 1, 12, int)
    num("scale_pct", 1, 1000)
    num("per_plate", 1, 50, int)
    num("design_hours", 0, 200)
    for k in ("supports", "brim"):
        if o.get(k) in ("on", "off", "auto"):
            out[k] = o[k]
    if o.get("orient") in ("as_sent", "auto"):
        out["orient"] = o["orient"]
    if o.get("process"):
        out["process"] = str(o["process"])[:60]
    if o.get("notes"):
        out["notes"] = str(o["notes"])[:500]
    return out


def to_slicer(o: dict) -> tuple[str | None, dict, dict]:
    """(process preset, process overrides, filament overrides): OrcaSlicer wants every value as a list of strings."""
    p: dict = {}
    if "layer_height" in o:
        p["layer_height"] = [f"{o['layer_height']:g}"]
    if "infill" in o:
        p["sparse_infill_density"] = [f"{o['infill']}%"]
    if "walls" in o:
        p["wall_loops"] = [str(o["walls"])]
    if o.get("supports") == "on":
        p.update(enable_support=["1"], support_type=["tree(auto)"])
    elif o.get("supports") == "off":
        p["enable_support"] = ["0"]
    if o.get("brim") == "on":
        p.update(brim_type=["outer_only"], brim_width=["5"])
    elif o.get("brim") == "off":
        p["brim_type"] = ["no_brim"]
    f: dict = {}
    mat = o.get("material", "PLA")
    if mat != "PLA" and mat in FILAMENT_TEMPS:  # PLA keeps your tested temperature (slicer.settings nozzle_temp)
        noz, bed = FILAMENT_TEMPS[mat]
        from batsy.shop.gcode import DENSITY

        f = {"nozzle_temperature": [str(noz)], "nozzle_temperature_initial_layer": [str(noz + 5)],
             "hot_plate_temp": [str(bed)], "hot_plate_temp_initial_layer": [str(bed)], "filament_type": [mat],
             "filament_density": [str(DENSITY.get(mat, 1.24))]}
        if mat == "PETG":
            f.update(fan_max_speed=["50"], fan_min_speed=["30"])
        if mat == "TPU":
            f["filament_max_volumetric_speed"] = ["3.5"]
    return o.get("process"), p, f


def slice_key(part: dict, per_plate: int = 1) -> str:
    o = part.get("options") or {}
    relevant = {k: o.get(k) for k in ("material", "layer_height", "infill", "walls", "supports", "brim", "scale_pct",
                                      "orient", "process")}
    return hashlib.sha1(json.dumps([part["file"], relevant, per_plate], sort_keys=True).encode()).hexdigest()[:16]


# --- files in ---------------------------------------------------------------------------------------------------------
def safe_name(name: str) -> str:
    name = Path(name or "model.stl").name
    stem, suf = Path(name).stem, Path(name).suffix.lower()
    stem = re.sub(r"[^A-Za-z0-9 ._-]+", "_", stem).strip(" ._") or "model"
    return f"{stem[:80]}{suf}"


def add_part(order_id: int, filename: str, stream=None, length: int = 0, src: Path | None = None,
             options: dict | None = None, qty: int = 1) -> dict:
    name = safe_name(filename)
    if Path(name).suffix not in MODELS:
        raise ValueError(f"{name}: send an STL, 3MF, OBJ or STEP file.")
    d = db.order_dir(order_id)
    dest = d / name
    i = 1
    while dest.exists():
        dest = d / f"{Path(name).stem} ({i}){Path(name).suffix}"
        i += 1
    if src is not None:
        shutil.copy2(src, dest)
    else:
        if length > 300 * 1024 * 1024:
            raise ValueError("That file is over 300 MB.")
        left = length
        with open(dest, "wb") as f:
            while left > 0:
                chunk = stream.read(min(1 << 16, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
    now = time.time()
    pid = db.insert("parts", order_id=order_id, name=Path(name).stem.replace("_", " "), file=str(dest),
                    qty=max(1, int(qty or 1)), options=clean_options(options), status="new", created=now, updated=now)
    estimate(pid)
    queue_slice(pid)
    return db.get("parts", pid)


def add_manual_part(order_id: int, name: str, grams: float, hours: float, qty: int = 1,
                    options: dict | None = None) -> dict:
    """A part without a file (an imported order, design work, something you'll model yourself): your numbers."""
    now = time.time()
    est = {"grams_each": float(grams or 0), "hours_each": float(hours or 0), "how": "manual", "warnings": [], "t": now}
    pid = db.insert("parts", order_id=order_id, name=(name or "Part")[:120], file="", qty=max(1, int(qty or 1)),
                    options=clean_options(options), estimate=est, status="manual", created=now, updated=now)
    reprice(order_id)
    return db.get("parts", pid)


# --- 1. instant ------------------------------------------------------------------------------------------------------
def _mesh_file(part: dict) -> Path:
    """The file to measure and slice: STEP made into STL, scaled when asked (cached beside the original)."""
    f = Path(part["file"])
    if f.suffix.lower() in (".step", ".stp"):
        stl = f.with_suffix(".stl")
        if not stl.exists():
            from batsy import slicer

            made = slicer.step_to_stl(f)
            if made != stl and Path(made).exists():
                shutil.copy2(made, stl)
        f = stl
    pct = (part.get("options") or {}).get("scale_pct")
    if pct and abs(pct - 100) > 0.01:
        out = f.with_name(f"{f.stem} @{pct:g}pct.stl")
        if not out.exists():
            import trimesh

            m = trimesh.load(str(f), force="mesh")
            m.apply_scale(pct / 100)
            m.export(str(out))
        f = out
    return f


def grams_per_hour(layer_height: float = 0.2) -> float:
    """What this printer really does, from the real slices so far (median), scaled for the layer height."""
    rows = db.q("SELECT slice FROM parts WHERE slice IS NOT NULL ORDER BY id DESC LIMIT 60")
    rates = []
    for r in rows:
        s = r["slice"] or {}
        if s.get("grams_each") and s.get("hours_each"):
            lh = s.get("layer_height") or 0.2
            rates.append(s["grams_each"] / s["hours_each"] * 0.2 / lh)
    base = sorted(rates)[len(rates) // 2] if len(rates) >= 3 else GPH_DEFAULT
    return base * (layer_height / 0.2)


def estimate(part_id: int) -> dict:
    part = db.get("parts", part_id)
    o = part.get("options") or {}
    if not part.get("file"):  # a manual part keeps the numbers you gave it
        reprice(part["order_id"])
        return part.get("estimate") or {}
    db.update("parts", part_id, status="estimating", updated=time.time())
    try:
        import trimesh

        from batsy import printweight

        f = _mesh_file(part)
        m = trimesh.load(str(f), force="mesh")
        if not len(m.faces):
            raise ValueError("the file has no surface in it")
        ext = sorted([float(v) for v in m.extents[:2]], reverse=True) + [float(m.extents[2])]
        x, y, z = (round(v, 1) for v in m.extents)
        fits = ext[0] <= BED[0] and ext[1] <= BED[1] and ext[2] <= BED[2]
        any_way = sorted(m.extents.tolist())  # laid another way up?
        fits_turned = any_way[0] <= BED[2] and any_way[2] <= BED[0] and any_way[1] <= BED[1]
        infill = (o.get("infill") if o.get("infill") is not None else 15) / 100
        w = printweight.estimate(m, o.get("material", "PLA").lower(), infill)
        lh = o.get("layer_height") or 0.2
        grams = float(w.get("print_g") or 0)
        hours = grams / max(grams_per_hour(lh), 0.5) if grams else 0
        warn = []
        if not fits:
            warn.append("Too big for the bed as it is" + (" (it fits laid another way)" if fits_turned else
                                                          ": scale it down or split it"))
        if w.get("how") == "approx":
            warn.append("The mesh has holes: the weight is approximate")
        if hours > 20:
            warn.append(f"Long print: about {hours:.0f} hours")
        est = {"size_mm": [x, y, z], "volume_cm3": w.get("volume_cm3"), "grams_each": round(grams, 1),
               "hours_each": round(hours, 2), "fits": fits, "fits_turned": fits_turned, "faces": int(len(m.faces)),
               "watertight": bool(m.is_watertight), "warnings": warn, "how": "estimate", "t": time.time()}
        db.update("parts", part_id, estimate=est, status="estimated", error=None, updated=time.time())
    except Exception as e:
        log.warning("estimate part %s: %s", part_id, e)
        est = {"error": str(e)[:200]}
        db.update("parts", part_id, estimate=est, status="failed", error=f"Couldn't read the model: {str(e)[:160]}",
                  updated=time.time())
    reprice(part["order_id"])
    return est


# --- 2. real (OrcaSlicer) ----------------------------------------------------------------------------------------------
_work: "queue.Queue[tuple[int, int]]" = queue.Queue()
_started = False
_start_lock = threading.Lock()


def _ensure_worker() -> None:
    global _started
    with _start_lock:
        if not _started:
            threading.Thread(target=_worker, daemon=True, name="shop-slicer").start()
            _started = True


def queue_slice(part_id: int, per_plate: int = 1, for_job: int | None = None) -> None:
    p = db.get("parts", part_id)
    if not p or not p.get("file"):
        return
    _ensure_worker()
    if for_job is None and p["status"] != "failed":
        db.update("parts", part_id, status="slicing", updated=time.time())
    _work.put((part_id, per_plate if for_job else 1, for_job or 0))


def _worker() -> None:
    while True:
        part_id, per_plate, job_id = _work.get()
        try:
            slice_now(part_id, per_plate, job_id or None)
        except Exception:
            log.exception("shop slicing part %s", part_id)


def slice_now(part_id: int, per_plate: int = 1, job_id: int | None = None) -> dict:
    """Slice the part (per_plate copies on one plate). A quote's slice is one copy; a job's is its plate."""
    from batsy import slicer

    part = db.get("parts", part_id)
    if part is None or not part.get("file") or part["status"] == "failed" and not (part.get("estimate") or {}).get("grams_each"):
        return {}
    key = slice_key(part, per_plate)
    if job_id:
        db.update("jobs", job_id, status="slicing", updated=time.time())
    if not slicer.ready():
        msg = "OrcaSlicer or your printer presets aren't set up on this machine: the numbers are estimates."
        if job_id:
            db.update("jobs", job_id, status="waiting", note=msg, updated=time.time())
        else:
            db.update("parts", part_id, status="estimated", error=msg, updated=time.time())
        return {}
    o = part.get("options") or {}
    process, over, fil = to_slicer(o)
    try:
        model = _mesh_file(part)
        if o.get("orient") == "auto":
            model, _said = slicer.prepare(model, force=True)
        label = f"{o.get('material', 'PLA')}" + (f" x{per_plate}" if per_plate > 1 else "")
        r = slicer.slice_model(model, process, over, label=f"shop {label}", filament_overrides=fil,
                               others=[model] * (per_plate - 1) if per_plate > 1 else None)
    except Exception as e:
        log.warning("shop slice part %s: %s", part_id, e)
        if job_id:
            db.update("jobs", job_id, status="waiting", note=f"Slicing failed: {str(e)[:200]}", updated=time.time())
        else:
            db.update("parts", part_id, status="estimated", error=f"Slicing failed: {str(e)[:200]}",
                      updated=time.time())
        db.event("batsy", "slice_failed", f"{part['name']}: {e}", f"part:{part_id}")
        return {}
    hours = slicer.hours(r.get("time", ""))
    grams = float(r.get("grams") or 0)
    # keep the G-code with the order (the slicer leaves it in a "sliced" folder next to the model)
    keep = db.order_dir(part["order_id"]) / "gcode"
    keep.mkdir(exist_ok=True)
    gfile = keep / f"part{part_id}-{key}.gcode"
    shutil.copy2(r["gcode"], gfile)
    for k in ("preview", "paths"):
        if r.get(k) and Path(r[k]).exists():
            dst = keep / f"part{part_id}-{key}.{k}{Path(r[k]).suffix}"
            shutil.copy2(r[k], dst)
            r[k] = str(dst)
    res = {"gcode": str(gfile), "time": r.get("time"), "grams_plate": grams, "hours_plate": round(hours, 3),
           "grams_each": round(grams / per_plate, 1), "hours_each": round(hours / per_plate, 3), "per_plate": per_plate,
           "metres": r.get("metres"), "process": r.get("process"), "decided": r.get("decided", []),
           "preview": r.get("preview"), "paths": r.get("paths"), "key": key, "how": "sliced", "t": time.time(),
           "layer_height": o.get("layer_height") or _preset_layer(process)}
    if job_id:
        db.update("jobs", job_id, gcode=str(gfile), gcode_key=key, status="ready", note=None, updated=time.time())
        db.event("batsy", "sliced", f"Job {job_id} sliced: {r.get('time')}, {grams:.0f} g for {per_plate}",
                 f"job:{job_id}")
    if per_plate == 1:
        db.update("parts", part_id, slice=res, slice_key=key, status="sliced", error=None, updated=time.time())
        reprice(part["order_id"])
        db.event("batsy", "sliced", f"{part['name']}: {r.get('time')}, {grams:.0f} g", f"part:{part_id}")
    return res


def _preset_layer(process: str | None) -> float:
    try:
        from batsy import slicer

        _n, f = slicer._process_file(process)
        flat = slicer.flatten(json.loads(f.read_text()), "process")
        v = flat.get("layer_height")
        return float(v[0] if isinstance(v, list) else v)
    except Exception:
        return 0.2


# --- numbers and price ---------------------------------------------------------------------------------------------------
def numbers(part: dict) -> dict:
    """grams and hours for ONE copy: the real slice when it matches the current settings, else the estimate."""
    s = part.get("slice") or {}
    if s.get("grams_each") and part.get("slice_key") == slice_key(part):
        return {"grams": s["grams_each"], "hours": s["hours_each"], "how": "sliced"}
    e = part.get("estimate") or {}
    return {"grams": e.get("grams_each") or 0, "hours": e.get("hours_each") or 0, "how": "estimate"}


def reprice(order_id: int) -> dict:
    o = db.get("orders", order_id)
    if not o:
        return {}
    parts = db.q("SELECT * FROM parts WHERE order_id = ?", (order_id,))
    rows = []
    for p in parts:
        n = numbers(p)
        rows.append({**n, "material": (p.get("options") or {}).get("material", "PLA"), "qty": p["qty"],
                     "design_hours": (p.get("options") or {}).get("design_hours", 0)})
    rush = False
    if o.get("due"):
        try:
            rush = time.mktime(time.strptime(o["due"][:10], "%Y-%m-%d")) - time.time() < 48 * 3600
        except ValueError:
            pass
    pr = costing.order_price(rows, rush=rush, discount=o.get("discount") or 0)
    if o["status"] == "quote":  # a confirmed order keeps the price the customer agreed to (reprice it yourself)
        db.update("orders", order_id, price=pr["price"], updated=time.time())
    return pr
