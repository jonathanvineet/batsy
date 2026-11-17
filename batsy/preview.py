"""What the slicer made, as a real slicer shows it: every extrusion line with its feature type (outer wall, inner
wall, infill, top surface, ironing, supports...) and its layer, read from OrcaSlicer's G-code comments (;TYPE:, ;Z:,
;HEIGHT:, ;WIDTH:).

- render(): a picture (PNG) coloured by feature type, in OrcaSlicer's own colours, for the phone and the laptop.
- export(): the data for the interactive 3D view (slice.html): positions, feature, layer and width per line, the
  layers' heights, and how much of each feature there is.
- both(): the two, in a short-lived process (matplotlib and a big parsed G-code don't stay in Batsy's memory).
"""
import json
import re
from pathlib import Path

# OrcaSlicer's preview colours (the default "Feature type" legend)
FEATURES = {
    "Outer wall": "#FF7D38", "Inner wall": "#FFE7A0", "Overhang wall": "#2E5CE6", "Sparse infill": "#B03029",
    "Internal solid infill": "#9654CC", "Top surface": "#F04040", "Bottom surface": "#7C6BC9", "Ironing": "#FF8C69",
    "Bridge": "#4D80BA", "Internal Bridge": "#6F98C8", "Gap infill": "#FFFFFF", "Skirt": "#00876E", "Brim": "#00876E",
    "Support": "#00C000", "Support interface": "#008000", "Support transition": "#00A000", "Prime tower": "#C2C2C2",
    "Custom": "#5E5E5E",
}
ORDER = list(FEATURES)


def parse(gcode: Path, every: int = 1) -> dict:
    """{segs: [(x0,y0,z0,x1,y1,z1), ...], feat: [i], layer: [i], width: [mm], layers: [{z, h}], features: [names]}
    for every extruding move of the model (the purge line before the first layer is left out)."""
    x = y = z = 0.0
    e_rel, e_last = True, 0.0
    segs, feat, layer, width = [], [], [], []
    layers: list[dict] = []
    names = list(ORDER)
    cur, w, h, li = names.index("Custom"), 0.42, 0.2, -1
    num = re.compile(r"([XYZEF])(-?[\d.]+)")
    n, started = 0, False
    for line in gcode.read_text(errors="replace").splitlines():
        if not line:
            continue
        if line[0] == ";":
            if line.startswith(";LAYER_CHANGE"):
                started = True
                li += 1
                layers.append({"z": z, "h": h, "set": False})
            elif line.startswith(";Z:") and layers:
                layers[-1]["z"] = float(line[3:] or 0)
            elif line.startswith(";HEIGHT:"):
                h = float(line[8:] or h)
                if layers and not layers[-1]["set"]:  # the layer's own height (ironing passes mark tiny ones later)
                    layers[-1]["h"], layers[-1]["set"] = h, True
            elif line.startswith(";WIDTH:"):
                w = float(line[7:] or w)
            elif line.startswith(";TYPE:"):
                t = line[6:].strip()
                if t not in names:
                    names.append(t)
                cur = names.index(t)
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
        elif code.startswith(("G1", "G0", "G2", "G3")):
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
                    feat.append(cur)
                    layer.append(max(0, li))
                    width.append(w)
            x, y, z = nx, ny, nz
    return {"segs": segs, "feat": feat, "layer": layer, "width": width, "layers": layers, "features": names}


def paths(gcode: Path, every: int = 1):
    """[(x0, y0, z0, x1, y1, z1), ...] for every extruding move (kept for older callers)."""
    return parse(gcode, every)["segs"]


def render(gcode: Path, out: Path, title: str = "", bands: list[tuple[float, int]] | None = None) -> Path:
    """A picture like a slicer's preview: coloured by feature type (by temperature band for a temperature tower)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import to_rgba
    from mpl_toolkits.mplot3d.art3d import Line3DCollection

    p = parse(gcode)
    segs, feat = p["segs"], np.array(p["feat"])
    step = len(segs) // 150000 + 1 if len(segs) > 150000 else 1  # big prints: every nth line is plenty for a picture
    a = np.array(segs[::step], dtype=float)
    feat = feat[::step]
    lines = a.reshape(-1, 2, 3)
    z = a[:, 2]
    fig = plt.figure(figsize=(9, 9), dpi=110, facecolor="#1d2026")
    ax = fig.add_subplot(111, projection="3d", facecolor="#1d2026")
    if bands:
        starts = [b[0] for b in bands]
        idx = np.clip(np.searchsorted(starts, z, side="right") - 1, 0, len(bands) - 1)
        cmap = plt.get_cmap("coolwarm_r")
        colours = cmap(idx / max(1, len(bands) - 1))
    else:
        palette = np.array([to_rgba(FEATURES.get(nm, "#5E5E5E")) for nm in p["features"]])
        colours = palette[feat].copy()
        shade = 0.72 + 0.28 * ((z - z.min()) / max(1e-6, z.max() - z.min()))  # a little depth: lower layers darker
        colours[:, :3] *= shade[:, None]
    mins, maxs = a.reshape(-1, 3).min(0), a.reshape(-1, 3).max(0)
    span = (maxs - mins).max() / 2 or 1
    mid = (maxs + mins) / 2
    ax.add_collection3d(Line3DCollection(lines, colors=colours, linewidths=0.9 if span < 40 else 0.6))
    ax.set_xlim(mid[0] - span, mid[0] + span)
    ax.set_ylim(mid[1] - span, mid[1] + span)
    ax.set_zlim(max(0, mid[2] - span), mid[2] + span)
    ax.view_init(elev=24, azim=-55)
    ax.set_axis_off()
    if bands:  # each temperature next to its block
        for i, (bz, temp) in enumerate(bands):
            top = bands[i + 1][0] if i + 1 < len(bands) else maxs[2]
            ax.text(maxs[0] + 6, mid[1], (bz + top) / 2, f"{temp}°", color=cmap(i / max(1, len(bands) - 1)),
                    fontsize=13, fontweight="bold")
    else:  # the legend: what each colour is, for the features this print has
        present = [nm for i, nm in enumerate(p["features"]) if (feat == i).any()]
        for k, nm in enumerate(present[:10]):
            fig.text(0.04, 0.88 - k * 0.032, "■", color=FEATURES.get(nm, "#5E5E5E"), fontsize=13)
            fig.text(0.075, 0.885 - k * 0.032, nm, color="#d7d2c6", fontsize=10)
    if title:
        fig.text(0.04, 0.95, title, color="#eee8da", fontsize=15, fontweight="bold")
    fig.subplots_adjust(0, 0, 1, 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=fig.get_facecolor())
    plt.close(fig)
    return out


def export(gcode: Path, out: Path, title: str = "", bands: list[tuple[float, int]] | None = None,
           limit: int = 400000) -> Path:
    """Everything the interactive view needs, compact: per line int16 x,y,z * 20 (0.05 mm), uint8 feature, uint16
    layer, uint8 width * 100; per layer z and height; and how much of each feature there is (mm of line)."""
    import base64

    import numpy as np

    p = parse(gcode)
    n = len(p["segs"])
    if n > limit:  # very big prints: thin evenly, keeping every layer
        keep = slice(None, None, n // limit + 1)
        p = {**p, **{k: p[k][keep] for k in ("segs", "feat", "layer", "width")}}
    a = np.array(p["segs"], dtype=float).reshape(-1, 6)
    pos = np.round(a.reshape(-1, 3) * 20).astype("<i2")
    feat = np.array(p["feat"], dtype="u1")
    lengths = np.hypot(a[:, 3] - a[:, 0], a[:, 4] - a[:, 1]) if len(a) else np.zeros(0)
    amount = {nm: round(float(lengths[feat == i].sum())) for i, nm in enumerate(p["features"]) if (feat == i).any()}

    def b64(arr) -> str:
        return base64.b64encode(arr.tobytes()).decode()

    out.write_text(json.dumps({
        "v": 2, "title": title, "segments": int(len(a)), "scale": 20,
        "pos": b64(pos), "feat": b64(feat), "layer": b64(np.array(p["layer"], dtype="<u2")),
        "width": b64(np.clip(np.round(np.array(p["width"]) * 100), 0, 255).astype("u1")),
        "layers": [{"z": round(l["z"], 3), "h": round(l["h"], 3)} for l in p["layers"]],
        "features": p["features"], "colors": [FEATURES.get(nm, "#5E5E5E") for nm in p["features"]],
        "amount_mm": amount, "bands": [{"z": z, "temp": t} for z, t in (bands or [])]}))
    return out


def both(gcode: Path, title: str = "", bands: list[tuple[float, int]] | None = None) -> tuple[Path, Path]:
    """The picture and the 3D data, drawn in a short-lived process: matplotlib and the parsed G-code (hundreds of
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
