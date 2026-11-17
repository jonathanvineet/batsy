"""A picture of what the slicer made: the printed paths from the G-code, in 3D, so you can see the slicing.

Only extruding moves are drawn (travel is left out), coloured by height, or by temperature band for a temperature
tower, with labels. Rendered with matplotlib off-screen to a PNG that Batsy shows on its screen and in the app.
"""
import json
import re
from pathlib import Path


def paths(gcode: Path, every: int = 1):
    """[(x0, y0, z0, x1, y1, z1), ...] for every extruding move."""
    x = y = z = 0.0
    e_rel, e_last = True, 0.0
    segs = []
    num = re.compile(r"([XYZEF])(-?[\d.]+)")
    n, started = 0, False
    for line in gcode.read_text(errors="replace").splitlines():
        if not started:  # the purge line before the first layer isn't the model: it would skew the framing
            started = line.startswith(";LAYER_CHANGE") or line.startswith(";Z:")
        if not line or line[0] == ";":
            continue
        code = line.split(";", 1)[0].strip()
        if code.startswith("M83"):
            e_rel = True
        elif code.startswith("M82"):
            e_rel = False
        elif code.startswith("G92"):
            m = re.search(r"E(-?[\d.]+)", code)
            if m:
                e_last = float(m.group(1))
        elif code.startswith(("G1", "G0")):
            v = dict(num.findall(code))
            nx, ny, nz = float(v.get("X", x)), float(v.get("Y", y)), float(v.get("Z", z))
            extruding = False
            if "E" in v:
                e = float(v["E"])
                extruding = e > 0 if e_rel else e > e_last
                if not e_rel:
                    e_last = e
            if extruding and started and (nx != x or ny != y):
                n += 1
                if n % every == 0:
                    segs.append((x, y, z, nx, ny, nz))
            x, y, z = nx, ny, nz
    return segs


def render(gcode: Path, out: Path, title: str = "", bands: list[tuple[float, int]] | None = None) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from mpl_toolkits.mplot3d.art3d import Line3DCollection

    segs = paths(gcode)
    if len(segs) > 120000:  # big prints: every nth move is plenty for a picture
        step = len(segs) // 120000 + 1
        segs = segs[::step]
    a = np.array(segs, dtype=float)
    lines = a.reshape(-1, 2, 3)
    z = a[:, 2]
    fig = plt.figure(figsize=(9, 9), dpi=110, facecolor="#0b0d12")
    ax = fig.add_subplot(111, projection="3d", facecolor="#0b0d12")
    if bands:
        starts = [b[0] for b in bands]
        idx = np.clip(np.searchsorted(starts, z, side="right") - 1, 0, len(bands) - 1)
        cmap = plt.get_cmap("coolwarm_r")
        colours = cmap(idx / max(1, len(bands) - 1))
    else:
        cmap = plt.get_cmap("plasma")
        colours = cmap((z - z.min()) / max(1e-6, z.max() - z.min()))
    ax.add_collection3d(Line3DCollection(lines, colors=colours, linewidths=0.5, alpha=0.9))
    mins, maxs = a.reshape(-1, 3).min(0), a.reshape(-1, 3).max(0)
    span = (maxs - mins).max() / 2 or 1
    mid = (maxs + mins) / 2
    ax.set_xlim(mid[0] - span, mid[0] + span)
    ax.set_ylim(mid[1] - span, mid[1] + span)
    ax.set_zlim(max(0, mid[2] - span), mid[2] + span)
    ax.view_init(elev=22, azim=-55)
    ax.set_axis_off()
    if bands:  # each temperature next to its block
        for i, (bz, temp) in enumerate(bands):
            top = bands[i + 1][0] if i + 1 < len(bands) else maxs[2]
            ax.text(maxs[0] + 6, mid[1], (bz + top) / 2, f"{temp}°", color=cmap(i / max(1, len(bands) - 1)),
                    fontsize=13, fontweight="bold")
    if title:
        fig.text(0.04, 0.95, title, color="#eee8da", fontsize=15, fontweight="bold")
    fig.subplots_adjust(0, 0, 1, 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=fig.get_facecolor())
    plt.close(fig)
    return out


def export(gcode: Path, out: Path, title: str = "", bands: list[tuple[float, int]] | None = None,
           limit: int = 160000) -> Path:
    """The paths for the interactive 3D view (phone app and laptop): every vertex as int16 x,y,z * 20 (0.05 mm),
    base64, so a whole print is a few hundred KB."""
    import base64
    import json

    import numpy as np

    segs = paths(gcode)
    if len(segs) > limit:
        segs = segs[::len(segs) // limit + 1]
    a = np.round(np.array(segs, dtype=float).reshape(-1, 3) * 20).astype("<i2")
    out.write_text(json.dumps({"title": title, "segments": len(segs), "scale": 20,
                               "bands": [{"z": z, "temp": t} for z, t in (bands or [])],
                               "pos": base64.b64encode(a.tobytes()).decode()}))
    return out


def both(gcode: Path, title: str = "", bands: list[tuple[float, int]] | None = None) -> tuple[Path, Path]:
    """The picture and the 3D paths, drawn in a short-lived process: matplotlib and the parsed G-code (hundreds of
    MB for a big print) go away with it instead of staying in Batsy's memory for good."""
    import subprocess
    import sys

    png, paths_json = gcode.with_suffix(".png"), gcode.with_suffix(".paths.json")
    r = subprocess.run([sys.executable, "-m", "batsy.preview", str(gcode), title, json.dumps(bands or [])],
                       capture_output=True, text=True, timeout=300)
    if r.returncode or not png.exists():
        raise RuntimeError(r.stderr.strip()[-300:] or "the preview failed")
    return png, paths_json


if __name__ == "__main__":
    import sys

    g, t = Path(sys.argv[1]), sys.argv[2]
    b = [tuple(x) for x in json.loads(sys.argv[3])] if len(sys.argv) > 3 else None
    render(g, g.with_suffix(".png"), t, b)
    export(g, g.with_suffix(".paths.json"), t, b)

