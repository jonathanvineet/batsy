"""Before anything is sliced: models you send wait for you, and you choose how each one sits on the bed.

  arrive   bolt.stl and nut.stl come in: nothing is sliced; Batsy says they're here and waits
  "slice them" / "slice both"   both go on one plate. For each model Batsy shows the ways it can sit (a numbered
           picture on the screen: 1 is always as you sent it) and asks which: "2", "the first one", "as it is",
           "you pick". Then it slices the plate.
  "slice it as it is" / "…lying down"   no questions: as sent / Batsy's best way up
"""
import json
import logging
import re
import time
from pathlib import Path

from batsy.config import DATA_DIR

log = logging.getLogger("batsy.plate")
WAITING: dict = {"models": [], "t": 0.0}     # what arrived and hasn't been sliced yet (kept across restarts)
_FILE = DATA_DIR / "plate_waiting.json"
ASK: dict = {}                               # the orientation questions in progress
BOTH = re.compile(r"\b(?:both|them|all(?: of them)?|together|same plate|one plate|these|those|everything)\b")
KEEP = re.compile(r"\b(?:as (?:it is|is|they are|sent|you got it)|the way (?:it is|i sent it)|keep (?:it|them|the) "
                  r"(?:as|orientation|position)|don'?t (?:turn|rotate)|no rotation)\b")
PICKS = {"1": 0, "one": 0, "first": 0, "2": 1, "two": 1, "second": 1, "3": 2, "three": 2, "third": 2,
         "4": 3, "four": 3, "fourth": 3, "last": -1}


def arrived(app, models: list[Path]) -> None:
    """New models: kept waiting for you (never sliced on their own)."""
    WAITING.update(models=list(models), t=time.time())
    _save()
    # (the inbox already says "You sent camera stand": this just puts it on the slice card, quietly)


def _save() -> None:
    try:
        _FILE.write_text(json.dumps({"models": [str(m) for m in WAITING["models"]], "t": WAITING["t"]}))
    except OSError:
        pass


def done(sliced: list[Path]) -> None:
    """These were sliced (as sent, or a turned copy of them): no longer waiting."""
    base = lambda p: re.sub(r" (?:laid|turned)$", "", p.stem)  # noqa: E731  (a turned copy is still that model)
    gone = {base(m) for m in sliced}
    WAITING["models"] = [w for w in WAITING["models"] if base(w) not in gone]
    _save()


def waiting() -> list[Path]:
    if not WAITING["t"] and _FILE.exists():  # after a restart
        try:
            d = json.loads(_FILE.read_text())
            WAITING.update(models=[Path(x) for x in d["models"]], t=d["t"])
        except (OSError, ValueError, KeyError):
            pass
    if time.time() - WAITING["t"] > 6 * 3600:
        return []
    return [m for m in WAITING["models"] if m.exists()]


# --- the ways a model can sit -----------------------------------------------------------------------------------------
def options(model: Path) -> tuple[object, list[dict]]:
    """(the mesh, up to 4 ways it can sit): 1 is always as sent, then the ways it naturally rests (a bolt upright on
    its head, a nut flat), then the least support. Each is described by its shape: upright, flat, on its side."""
    import numpy as np
    import trimesh

    from batsy import slicer

    m = trimesh.load(str(model), force="mesh")
    cands = [(np.eye(4), 0.0)]
    try:  # the ways it comes to rest on its own (most likely first): the natural ways to print it
        poses, probs = trimesh.poses.compute_stable_poses(m, n_samples=1, threshold=0.0)
        cands += [(t, float(pr)) for t, pr in zip(poses, probs)]
    except Exception:
        pass
    for axis in ([1, 0, 0], [0, 1, 0]):
        for ang in (np.pi / 2, np.pi, -np.pi / 2):
            cands.append((trimesh.transformations.rotation_matrix(ang, axis), 0.0))
    longest = float(max(m.extents))
    found, keys = [], []
    for i, (t, prob) in enumerate(cands):
        placed = slicer._placed(m.copy().apply_transform(t))
        cost, support, foot = slicer._cost(placed)
        h = float(placed.extents[2])
        key = (round(h / 1.5), round(foot / 25))  # the same pose turned around Z is the same choice
        if i and key in keys:
            continue
        keys.append(key)
        shape = ("standing upright" if h >= 0.85 * longest else "lying flat" if h <= 0.4 * longest else "on its side")
        if foot < 5:
            shape += ", balancing on a point"
        found.append({"t": t, "cost": cost, "support": support, "foot": foot, "height": h, "prob": prob,
                      "how": ("as you sent it: " if i == 0 else "") + shape, "shape": shape, "first": i == 0})
    first, rest = found[0], found[1:]
    natural = sorted([o for o in rest if o["prob"] >= 0.05 and o["foot"] >= 5], key=lambda o: -o["prob"])[:2]
    cheap = [o for o in sorted(rest, key=lambda o: o["cost"]) if all(o is not x for x in natural) and o["foot"] >= 5]
    opts = [first, *natural, *cheap][:4]
    ups = [o for o in opts if o["shape"].startswith("standing upright") and o["foot"] >= 5]
    if len(ups) > 1 and max(o["foot"] for o in ups) >= 3 * min(o["foot"] for o in ups):  # a bolt: head or tip
        wide = max(ups, key=lambda o: o["foot"])
        for o in ups:
            end = "on its wide end (head down)" if o is wide else "on its narrow end"
            o["shape"] = o["shape"].replace("standing upright", f"standing upright {end}")
            o["how"] = o["how"].replace("standing upright", f"standing upright {end}")
    base = first["support"] or 1
    for o in opts:
        less = round(100 * (1 - o["support"] / base))
        o["label"] = (f"{o['how']}, {o['height']:.0f} mm tall"
                      + (f", on {o['foot']:.0f} mm²" if o["foot"] >= 5 else "")
                      + ("" if o["first"] else f", {less}% less support" if less >= 0 else
                         f", {-less}% more support" if less > -100 else ", needs more support"))
    best = min(range(len(opts)), key=lambda k: opts[k]["cost"] * (0.6 if opts[k]["prob"] >= 0.1 else 1.0))
    opts[best]["best"] = True
    return m, opts


def picture(model: Path, mesh, opts: list[dict], single: int | None = None) -> Path:
    """The options side by side, numbered, sitting on a grey bed (or just option `single`, small, for the phone)."""
    import matplotlib
    import numpy as np

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    from batsy import slicer
    from batsy.config import DATA_DIR

    show = [(single, opts[single])] if single is not None else list(enumerate(opts))
    fig = plt.figure(figsize=(3.2 * len(show), 3.9) if single is None else (2.6, 2.6), facecolor="#0d0f13")
    for j, (k, o) in enumerate(show):
        p = slicer._placed(mesh.copy().apply_transform(o["t"]))
        ax = fig.add_subplot(1, len(show), j + 1, projection="3d", facecolor="#0d0f13")
        step = max(1, len(p.faces) // 60000)  # every face (skipping some left see-through holes), unless huge
        f, n = p.faces[::step], p.face_normals[::step]
        shade = 0.35 + 0.65 * np.clip(n @ np.array([0.3, -0.5, 0.8]), 0, 1)
        col = np.c_[shade * 1.0, shade * 0.76, shade * 0.06, np.ones(len(shade))]  # Batsy yellow, shaded
        mn, mx = p.vertices.min(0), p.vertices.max(0)
        c, r = (mn + mx) / 2, max((mx - mn).max() / 2, 1)
        bx = np.array([[c[0] - r * 1.2, c[1] - r * 1.2, 0], [c[0] + r * 1.2, c[1] - r * 1.2, 0],
                       [c[0] + r * 1.2, c[1] + r * 1.2, 0], [c[0] - r * 1.2, c[1] + r * 1.2, 0]])
        ax.add_collection3d(Poly3DCollection([bx], facecolors=(0.25, 0.27, 0.3, 0.6), edgecolor="none"))
        ax.add_collection3d(Poly3DCollection(p.vertices[f], facecolors=col, edgecolor="none", zorder=2))
        ax.set_xlim(c[0] - r, c[0] + r)
        ax.set_ylim(c[1] - r, c[1] + r)
        ax.set_zlim(0, 2 * r)
        ax.view_init(22, -55)
        ax.set_axis_off()
        if single is not None:  # a phone tile: the part fills it
            ax.set_position([0, 0, 1, 1])
            ax.set_box_aspect(None, zoom=1.25)
        if single is None:
            ax.set_title(f"{k + 1}" + ("  ★ best" if o.get("best") else "") + f"\n{o['how']}", color="#e9e6df",
                         fontsize=13, fontweight="bold")
    if single is None:
        fig.suptitle(f"How should {model.stem.replace('_', ' ')} sit?", color="#ffc20e", fontsize=15, fontweight="bold")
    if single is None:
        plt.tight_layout()
    out = DATA_DIR / ("plate_options.png" if single is None else f"plate_option_{single}.png")
    fig.savefig(out, dpi=90, facecolor=fig.get_facecolor())
    plt.close(fig)
    return out


# --- asking -----------------------------------------------------------------------------------------------------------
def start(app, models: list[Path], go) -> str:
    """Ask how each model sits, one at a time; then go(the models to slice, turned as you chose)."""
    ASK.clear()
    ASK.update(queue=list(models), i=0, chosen=[], go=go, t=time.time())
    return _ask_current(app)


def _ask_current(app) -> str:
    model = ASK["queue"][ASK["i"]]
    mesh, opts = options(model)
    ASK.update(mesh=mesh, opts=opts, t=time.time())
    try:
        for k in range(len(opts)):  # the phone's slice card: one picture per option, to tap
            picture(model, mesh, opts, single=k)
        ASK["v"] = ASK.get("v", 0) + 1
        pic = picture(model, mesh, opts)
        if getattr(app, "overlay", None) is not None and app.overlay.enabled:
            app.overlay.image(pic, f"How should {model.stem} sit?")
    except Exception as e:
        log.warning("no options picture: %s", e)
    best = next(k for k, o in enumerate(opts) if o.get("best"))
    name = model.stem.replace("_", " ")
    if hasattr(app, "phone_event"):  # the phone goes to its slice card, where the options are buttons
        app.phone_event(f"How should the {name} sit before slicing? Tap one on the slice card.")
    lead = f"The {name}: " if len(ASK["queue"]) > 1 else f"How should {name} sit? "
    return f"{lead}I'd go with {best + 1}, {_short(opts[best]).split(' · ')[0].lower()}. Pick one on the screen."


def answer(app, n: str) -> str | None:
    """'2', 'the first one', 'as it is', 'you pick', 'stop'. None when nothing is being asked or it's not an answer."""
    if not ASK or time.time() - ASK.get("t", 0) > 900:
        return None
    if re.match(r"(?:stop|cancel|never ?mind|forget it|don'?t slice)\b", n):
        ASK.clear()
        if getattr(app, "overlay", None) is not None and app.overlay.enabled:
            app.overlay.hide()  # the options picture goes too
        return "Okay, I won't slice it."
    opts = ASK["opts"]
    m = re.search(r"\b(" + "|".join(PICKS) + r")\b", n)
    if KEEP.search(n):
        k = 0
    elif re.search(r"\b(?:best|you (?:pick|choose|decide)|your (?:pick|choice)|whatever|recommended|the one you said)\b", n):
        k = next(i for i, o in enumerate(opts) if o.get("best"))
    elif m and len(n.split()) <= 8:
        k = PICKS[m.group(1)]
    elif re.search(r"\b(?:upright|standing|stand(?: it)? up|head down|on its head|vertical(?:ly)?)\b", n) and \
            any("upright" in o["shape"] for o in opts):
        k = max((i for i, o in enumerate(opts) if "upright" in o["shape"]),  # "head down": the wide end
                key=lambda i: opts[i]["foot"] if not re.search(r"\b(?:tip|narrow|point)\b", n) else -opts[i]["foot"])
    elif re.search(r"\b(?:flat|lying flat|lie flat|face down|flat side down)\b", n) and any("flat" in o["shape"] for o in opts):
        k = next(i for i, o in enumerate(opts) if "flat" in o["shape"])
    elif re.search(r"\bon its side\b", n) and any("side" in o["shape"] for o in opts):
        k = next(i for i, o in enumerate(opts) if "side" in o["shape"])
    else:
        return None
    k = max(0, min(len(opts) - 1, k if k >= 0 else len(opts) - 1))
    ASK["chosen"].append(_apply(ASK["queue"][ASK["i"]], ASK["mesh"], opts[k]))
    ASK["i"] += 1
    if ASK["i"] < len(ASK["queue"]):
        return _ask_current(app)
    chosen, go = ASK["chosen"], ASK["go"]
    ASK.clear()
    app.overlay.hide() if getattr(app, "overlay", None) is not None and app.overlay.enabled else None
    return go(chosen)


def _apply(model: Path, mesh, opt: dict) -> Path:
    """The model as chosen: the file itself when it stays as sent, else a turned copy next to it."""
    if opt["first"]:
        return model
    from batsy import inbox, slicer

    out = model.with_name(f"{re.sub(r' (?:laid|turned)$', '', model.stem)} turned.stl")
    m = slicer._placed(mesh.copy().apply_transform(opt["t"]))
    inbox.made(out)  # ours, not something you sent
    m.export(str(out))
    return out


def again(app) -> list[Path]:
    """The models on the last plate, as you sent them (a turned copy -> its original): to choose positions again."""
    last = getattr(app, "slice_last", None) or {}
    listed = last.get("models")
    if not listed and last.get("model"):  # sliced before plates remembered their models: "bolt + nut - nicee.gcode"
        first = Path(last["model"])
        gname = Path((last.get("result") or {}).get("gcode") or "").name.split(" - ")[0]
        listed = [str(first.with_name(n + first.suffix)) for n in gname.split(" + ")] if " + " in gname else [str(first)]
    out = []
    for m in listed or []:
        p = Path(m)
        orig = p.with_name(re.sub(r" (?:laid|turned)$", "", p.stem) + p.suffix)
        out.append(orig if orig.exists() else p)
    return [m for m in out if m.exists() and m.suffix.lower() in (".stl", ".obj")]


def _short(o: dict) -> str:
    """A tile's caption: 'Head down · 29 mm', 'Lying flat · 5 mm', 'As sent · 20 mm'."""
    if o["first"]:
        word = "As sent"
    elif "wide end" in o["shape"]:
        word = "Head down"
    elif "narrow end" in o["shape"]:
        word = "Tip down"
    else:
        word = o["shape"].split(",")[0].replace("standing upright", "Upright").capitalize()
    return f"{word} · {o['height']:.0f} mm"


def state() -> dict:
    """For the phone's slice card: what's waiting, and the question being asked (with its options)."""
    out = {"waiting": [re.sub(r" (?:laid|turned)$", "", m.stem).replace("_", " ") for m in waiting()], "asking": None}
    if ASK and time.time() - ASK.get("t", 0) < 900 and ASK.get("opts"):
        model = ASK["queue"][ASK["i"]]
        out["asking"] = {"model": re.sub(r" (?:laid|turned)$", "", model.stem).replace("_", " "), "n": ASK["i"] + 1,
                         "of": len(ASK["queue"]),
                         "v": ASK.get("v", 0),
                         "options": [{"how": _short(o), "label": o["label"],
                                      "best": bool(o.get("best"))}
                                     for o in ASK["opts"]]}
    return out
