"""Calibration prints, the OrcaSlicer way: a test model, sliced with your presets, then the G-code changed by height.

Temperature tower: blocks of BLOCK mm, each printed STEP degrees cooler than the one below. Every block has a
bridge between two pillars (sags when too hot), a 45-degree overhang (curls when too hot) and the gap between the
pillars (strings when too hot; layers split when too cold). Pick the lowest-sagging, cleanest block: that's the
temperature for this filament. OrcaSlicer's own tower ships as a Draco mesh it assembles at run time, so Batsy
builds an equivalent one with trimesh and inserts "M104 S<temp>" at each block's first layer, as Orca does.
"""
import re
from pathlib import Path

from batsy.config import DATA_DIR

OUT = DATA_DIR / "calibration"
BLOCK = 10.0  # mm per temperature
STEP = 5


def tower_stl(blocks: int) -> Path:
    """The tower as an STL: blocks x (two pillars + bridge + overhang wedge) on a thin base."""
    import numpy as np
    import trimesh

    parts = [trimesh.creation.box(extents=(46, 14, 1.0), transform=_at(21, 5, 0.5))]  # base: sticks, no tipping
    for i in range(blocks):
        z0 = 1.0 + i * BLOCK
        parts.append(trimesh.creation.box(extents=(8, 10, BLOCK), transform=_at(4, 5, z0 + BLOCK / 2)))   # left pillar
        parts.append(trimesh.creation.box(extents=(8, 10, BLOCK), transform=_at(28, 5, z0 + BLOCK / 2)))  # right pillar
        parts.append(trimesh.creation.box(extents=(16, 10, 1.6), transform=_at(16, 5, z0 + BLOCK - 0.8)))  # bridge
        # 45-degree overhang growing out of the right pillar
        v = np.array([[32, 0, z0], [32, 10, z0], [32, 0, z0 + 8], [32, 10, z0 + 8], [40, 0, z0 + 8], [40, 10, z0 + 8]])
        f = [[0, 2, 4], [1, 5, 3], [0, 1, 3], [0, 3, 2], [2, 3, 5], [2, 5, 4], [0, 4, 5], [0, 5, 1]]
        parts.append(trimesh.Trimesh(vertices=v, faces=f))
        parts.append(trimesh.creation.box(extents=(8, 10, 2), transform=_at(36, 5, z0 + 9)))  # cap over the wedge
    mesh = trimesh.util.concatenate(parts)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"temperature_tower_{blocks}.stl"
    mesh.export(path)
    return path


def _at(x, y, z):
    import numpy as np

    m = np.eye(4)
    m[:3, 3] = (x, y, z)
    return m


ORCA_TOP = 350  # OrcaSlicer's tower: 70 blocks of 10 mm, the bottom one 350 C, each one up 5 C cooler


def orca_tower(hi: int, blocks: int) -> Path | None:
    """OrcaSlicer's own temperature tower (calib/temperature_tower.drc: overhangs, a bridge window, a stringing
    cone in every block), converted to STL once with OrcaSlicer itself, then cut to your range the way Orca does:
    keep the blocks from (350 - hi) / 5 up, `blocks` of them, and stand them on the bed."""
    import subprocess

    import trimesh

    from batsy import slicer

    src_dir = OUT / "orca"
    stl = next(iter(sorted((src_dir / "stl").glob("*.stl"))), None) if (src_dir / "stl").exists() else None
    if stl is None:
        root = slicer._flatpak_root()
        drc = root / "share/OrcaSlicer/calib/temperature_tower/temperature_tower.drc" if root else None
        cmd = slicer.command()
        if not drc or not drc.exists() or not cmd:
            return None
        src_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run([*cmd, "--export-stl", "--outputdir", str(src_dir), str(drc)], capture_output=True, timeout=180)
        stl = next(iter(sorted((src_dir / "stl").glob("*.stl"))), None)
        if stl is None:
            return None
    z0 = (ORCA_TOP - hi) / STEP * BLOCK
    full = trimesh.load(stl)
    if z0 < 0 or z0 + blocks * BLOCK > full.bounds[1][2] + 0.01:
        return None
    part = full.slice_plane([0, 0, z0], [0, 0, 1], cap=True).slice_plane([0, 0, z0 + blocks * BLOCK], [0, 0, -1], cap=True)
    part.apply_translation([0, 0, -z0])
    out = OUT / f"orca_tower_{hi}_{hi - (blocks - 1) * STEP}.stl"
    part.export(out)
    return out


def temps_for(hi: int, lo: int, step: int = STEP) -> list[int]:
    hi, lo = max(hi, lo), min(hi, lo)
    return list(range(hi, lo - 1, -step))


def set_temps_by_height(gcode: Path, temps: list[int]) -> list[tuple[float, int]]:
    """Insert M104 at the first layer of each block. Returns [(z, temp)] where each one starts."""
    lines = gcode.read_text(errors="replace").splitlines(keepends=True)
    out, marks, block = [], [], 0
    z_rx = re.compile(r"^;\s*Z:\s*([\d.]+)()")  # OrcaSlicer's layer markers only (not the start-up "G1 Z50")
    for line in lines:
        out.append(line)
        m = z_rx.match(line)
        if not m:
            continue
        z = float(m.group(1) or m.group(2))
        base = 1.0 if getattr(set_temps_by_height, "base", True) else 0.0
        # the layer ending exactly at 10.0 mm is still the first block: it switches on the layer after (10.2)
        want = min(int(max(z - base - 1e-6, 0) // BLOCK), len(temps) - 1)  # (Batsy's own tower has a 1 mm base)
        if want > block or (not marks and z > 0):
            block = max(block, want)
            out.append(f"M104 S{temps[block]} ; temperature tower: block {block + 1}, {temps[block]} C\n")
            marks.append((round(z, 2), temps[block]))
    gcode.write_text("".join(out))
    return marks


def temperature_tower(hi: int = 220, lo: int = 190, process: str | None = None) -> dict:
    """Build, slice (your printer + presets) and set the temperatures. Returns the slicer result + the blocks."""
    from batsy import slicer

    temps = temps_for(hi, lo)
    model = orca_tower(temps[0], len(temps)) or tower_stl(len(temps))  # OrcaSlicer's tower; Batsy's own if not
    overrides = {"enable_support": "0", "brim_type": "outer_only", "brim_width": "5", "wall_loops": "2",
                 "sparse_infill_density": "15%", "ironing_type": "no ironing", "layer_height": "0.2"}
    r = slicer.slice_model(model, process, overrides, f"{temps[0]}-{temps[-1]}C",
                           filament_overrides={"nozzle_temperature": [str(temps[0])],
                                               "nozzle_temperature_initial_layer": [str(temps[0])]})
    set_temps_by_height.base = "orca_tower" not in model.name  # Orca's tower starts its first block on the bed
    marks = set_temps_by_height(Path(r["gcode"]), temps)
    from batsy import preview

    title = f"Temperature tower {temps[0]} → {temps[-1]} °C"
    r["preview"] = str(preview.render(Path(r["gcode"]), Path(r["gcode"]).with_suffix(".png"), title, marks))
    r["paths"] = str(preview.export(Path(r["gcode"]), Path(r["gcode"]).with_suffix(".paths.json"), title, marks))
    r["name"] = title
    r.update(temps=temps, marks=marks, model=str(model))
    return r


# --- voice ----------------------------------------------------------------------------------------------------
import threading  # noqa: E402

TOWER = re.compile(r"\b(?:temp(?:erature)?s?|heat) ?(?:tower|test|calibration|calibrate)|calibrat\w* (?:the )?temp(?:erature)?|"
                   r"temperature (?:testing|test print|calibration print)")
RANGE = re.compile(r"(?:from )?(\d{3})\s*(?:to|-|down to|and)\s*(\d{3})")


def handle(app, n: str) -> str | None:
    """"print a temperature tower", "temperature calibration from 230 to 190", "slice a temperature tower"."""
    if not TOWER.search(n):
        if re.search(r"\bcalibrat|\btest print\b", n) and re.search(r"\b(print|slice|run|do|make|start)\b", n):
            return ("Which calibration? I can print a temperature tower now: say print a temperature tower, or from "
                    "230 to 190. Flow, retraction and max speed tests are next.")
        return None
    m = RANGE.search(n)
    hi, lo = (int(m.group(1)), int(m.group(2))) if m else (220, 190)
    if not (150 <= min(hi, lo) and max(hi, lo) <= 280):
        return "Give me temperatures between 150 and 280 degrees."
    want_print = bool(re.search(r"\bprint", n)) and not n.startswith("slice")
    app._reply(f"Making a temperature tower from {max(hi, lo)} down to {min(hi, lo)} degrees.")

    def run():
        from batsy import slicer

        try:
            r = temperature_tower(hi, lo)
        except Exception as e:
            app.announce(f"I couldn't make the tower: {e}")
            return
        blocks = ", ".join(f"{t}" for t in r["temps"])
        app.slice_last = {"model": r["model"], "process": r["process"], "overrides": {}, "result": r}
        slicer.show_3d(app)  # the tower in 3D, each temperature in its own colour
        said = (f"The tower is sliced: {len(r['temps'])} blocks, {blocks} degrees from the bottom up, about "
                f"{slicer._say_time(r.get('time', ''))} and {round(r.get('grams') or 0)} grams.")
        pr = getattr(app, "printer3d", None)
        if not want_print or pr is None or not pr.state["connected"]:
            app.announce(said + (" Say print it when the bed is clear." if pr and pr.state["connected"] else ""))
            return
        if pr.state["printing"] or pr.state["uploading"] is not None:
            app.announce(said + " The printer is busy, so I'll leave it there.")
            return
        if not app.ask_from_background(said + " Is the bed clear? Shall I print it?"):
            app.announce("Okay, it's ready whenever you say print it.")
            return
        threading.Thread(target=pr.print_file, args=(Path(r["gcode"]), "the temperature tower"), daemon=True).start()
        app.announce("Copying it to the printer. Afterwards, pick the block with the cleanest bridge and the least "
                     "stringing: that's your temperature.")

    threading.Thread(target=run, daemon=True, name="temp-tower").start()
    return ""
