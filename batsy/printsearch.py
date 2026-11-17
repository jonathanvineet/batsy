"""Find things to 3D print: Printables, MakerWorld (and Thingiverse with a free key), searched together.

  "find me a phone stand to print" · "search printables for a cable clip" · "find a 3d model of a headphone hook"
    -> the best models from every site, as numbered photo cards (laptop overlay + the phone app)
  "the second one" · "get number 3" · "the geared one"  (for 10 minutes after a search)
    -> Printables / Thingiverse: downloaded into ~/Batsy/inbox/<site>/, where the usual happens (it's sliced with
       your presets, shown in 3D, "print it" prints it, a card goes in the brain);
       MakerWorld: downloads need your Bambu login, so its page opens instead

No accounts needed for Printables and MakerWorld. Thingiverse: put an app token (thingiverse.com/developers) in
[models] thingiverse_token.
"""
import concurrent.futures as cf
import io
import json
import logging
import math
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

from batsy.config import WORKSPACE

log = logging.getLogger("batsy.printsearch")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Batsy", "Accept": "application/json"}
OUT = WORKSPACE / "models"
PRE = r"^(?:(?:can you|could you|please|hey|ok|okay|batsy)\s+)*"
FIND = [
    re.compile(PRE + r"(?:find|search(?: for)?|look for|get|show) (?:me )?(?:a |an |some )?(?:3d |printable )?"
                     r"(?:models?|stls?|prints?|files?|designs?) (?:of|for) (?:a |an |some )?(?P<q>.+)$"),
    re.compile(PRE + r"(?:find|search(?: for)?|look for|get) (?:me )?(?:a |an |some )?(?P<q>.+?) "
                     r"(?:to (?:3d )?print|i can (?:3d )?print|for (?:my|the) (?:3d )?printer|3d models?|stls?|"
                     r"printables?|to download and print)$"),
    re.compile(PRE + r"(?:search|check|look (?:on|in)) (?:on )?(?:printables|makerworld|maker world|thingiverse|"
                     r"the (?:model|print|3d) sites?) (?:for )?(?:a |an |some )?(?P<q>.+)$"),
    re.compile(PRE + r"(?:find|search for|look for) (?:a |an |some )?(?P<q>.+?) (?:on|from) (?:printables|makerworld|"
                     r"maker world|thingiverse)$"),
]
WORDS = {"one": 1, "first": 1, "two": 2, "second": 2, "three": 3, "third": 3, "four": 4, "fourth": 4, "five": 5,
         "fifth": 5, "six": 6, "sixth": 6, "seven": 7, "seventh": 7, "eight": 8, "eighth": 8, "nine": 9, "ninth": 9,
         "ten": 10, "tenth": 10, "eleven": 11, "twelve": 12, "last": -1}
PICK = re.compile(PRE + r"(?:(?:get|download|print|take|choose|pick|open|show me|i(?:'ll| will)? (?:take|want)|"
                        r"let'?s (?:do|go with|print)|go with) )?(?:me )?(?:the |number |no |model )*"
                        r"(?P<n>\d{1,2}|" + "|".join(WORDS) + r")(?: one)?(?: please)?$")
PICK_NAME = re.compile(PRE + r"(?:get|download|print|take|choose|pick|open) (?:me )?the (?P<name>.+?) one$")


def _get(url: str, data: dict | None = None, headers: dict | None = None, timeout: float = 15):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={**UA, **({"Content-Type": "application/json"} if body else {}),
                                                         **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw[:1] in (b"{", b"[") else raw


# --- the sites -----------------------------------------------------------------------------------------------------------
PRINTABLES = "https://api.printables.com/graphql/"


def printables(q: str, n: int = 12) -> list[dict]:
    d = _get(PRINTABLES, {"query": "query($q:String!,$n:Int){ searchPrints2(query:$q, limit:$n){ items { id name slug "
                                   "likesCount downloadCount user{ publicUsername } image { filePath } } } }",
                          "variables": {"q": q, "n": n}})
    out = []
    for it in ((d.get("data") or {}).get("searchPrints2") or {}).get("items") or []:
        img = (it.get("image") or {}).get("filePath")
        out.append({"site": "Printables", "id": it["id"], "name": it["name"], "likes": it.get("likesCount") or 0,
                    "downloads": it.get("downloadCount") or 0, "by": (it.get("user") or {}).get("publicUsername", ""),
                    "image": _printables_thumb(img) if img else "",
                    "image_full": f"https://media.printables.com/{img}" if img else "",
                    "url": f"https://www.printables.com/model/{it['id']}-{it.get('slug', '')}", "can_download": True})
    return out


def _printables_thumb(path: str) -> str:
    """A card-sized version (about 14 KB) instead of the full photo (often 2-4 MB)."""
    folder, _, name = path.rpartition("/")
    return f"https://media.printables.com/{folder}/thumbs/cover/640x480/jpg/{name.rsplit('.', 1)[0]}.jpg"


def makerworld(q: str, n: int = 12) -> list[dict]:
    d = _get("https://makerworld.com/api/v1/search-service/select/design2?" +
             urllib.parse.urlencode({"keyword": q, "limit": n, "offset": 0}))
    out = []
    for it in (d or {}).get("hits") or []:
        out.append({"site": "MakerWorld", "id": str(it["id"]), "name": it.get("titleTranslated") or it.get("title", ""),
                    "likes": it.get("likeCount") or 0, "downloads": it.get("downloadCount") or 0,
                    "prints": it.get("printCount") or 0, "by": (it.get("designCreator") or {}).get("name", ""),
                    "image": (it["cover"] + "?x-oss-process=image/resize,w_640") if it.get("cover") else "",  # card-sized
                    "url": f"https://makerworld.com/en/models/{it['id']}-{it.get('slug', '')}",
                    "can_download": False})
    return out


def thingiverse(q: str, token: str, n: int = 12) -> list[dict]:
    if not token:
        return []
    d = _get(f"https://api.thingiverse.com/search/{urllib.parse.quote(q)}?type=things&per_page={n}",
             headers={"Authorization": f"Bearer {token}"})
    out = []
    for it in (d or {}).get("hits") or []:
        out.append({"site": "Thingiverse", "id": str(it["id"]), "name": it.get("name", ""),
                    "likes": it.get("like_count") or 0, "downloads": it.get("collect_count") or 0,
                    "by": (it.get("creator") or {}).get("name", ""), "image": it.get("preview_image") or it.get("thumbnail", ""),
                    "url": it.get("public_url", ""), "can_download": True})
    return out


def search(q: str, cfg) -> list[dict]:
    """All sites at once; the best first (popularity, with each site's best near the top), at most 12."""
    token = cfg.get("models.thingiverse_token") if cfg else ""
    jobs = {"Printables": lambda: printables(q), "MakerWorld": lambda: makerworld(q),
            "Thingiverse": lambda: thingiverse(q, token)}
    results: dict[str, list] = {}
    with cf.ThreadPoolExecutor(3) as ex:
        futs = {ex.submit(fn): site for site, fn in jobs.items()}
        for f in cf.as_completed(futs):
            try:
                results[futs[f]] = f.result()
            except Exception as e:
                log.info("model search on %s failed: %s", futs[f], e)
                results[futs[f]] = []

    def score(it, rank):  # popular, but a site's own top hit stays near the top (its search knows relevance)
        return math.log10(1 + it["downloads"] + 5 * it["likes"]) - 0.35 * rank

    merged = [(score(it, i), it) for site in results.values() for i, it in enumerate(site)]
    return [it for _, it in sorted(merged, key=lambda x: -x[0])][:12]


# --- getting one -----------------------------------------------------------------------------------------------------
def files_of(item: dict, cfg) -> list[dict]:
    """[{name, size, url}] of the model's printable files (STL/3MF)."""
    if item["site"] == "Printables":
        d = _get(PRINTABLES, {"query": "query($id:ID!){ print(id:$id){ stls{ id name fileSize } } }",
                              "variables": {"id": item["id"]}})
        out = []
        for f in ((d.get("data") or {}).get("print") or {}).get("stls") or []:
            link = _get(PRINTABLES, {"query": "mutation($id:ID!,$p:ID!){ getDownloadLink(id:$id, printId:$p, fileType:stl, "
                                              "source:model_detail){ ok output{ link } } }",
                                     "variables": {"id": f["id"], "p": item["id"]}})
            url = (((link.get("data") or {}).get("getDownloadLink") or {}).get("output") or {}).get("link")
            if url:
                out.append({"name": f["name"], "size": f.get("fileSize") or 0, "url": url})
        return out
    if item["site"] == "Thingiverse":
        token = cfg.get("models.thingiverse_token")
        d = _get(f"https://api.thingiverse.com/things/{item['id']}/files", headers={"Authorization": f"Bearer {token}"})
        return [{"name": f["name"], "size": f.get("size") or 0, "url": f["download_url"] + f"?access_token={token}"}
                for f in d or [] if f["name"].lower().endswith((".stl", ".3mf", ".obj", ".step"))]
    return []


def download(item: dict, cfg, inbox: Path, limit: int = 4, folder: Path | None = None) -> list[Path]:
    """The model's files (at most `limit`) into `folder` (default: the inbox, the site as the 'sender')."""
    files = files_of(item, cfg)[:limit]
    folder = folder or inbox / item["site"]
    folder.mkdir(parents=True, exist_ok=True)
    got = []
    for f in files:
        name = re.sub(r'[\\/:*?"<>|]+', "_", f["name"]).strip() or f"{item['name']}.stl"
        tmp = folder / f".{name}.part"
        data = _get(f["url"], timeout=120)
        tmp.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
        dest = folder / name
        tmp.replace(dest)  # appears whole: the inbox watcher sees one finished file
        got.append(dest)
    return got


# --- showing them ------------------------------------------------------------------------------------------------------
def collage(items: list[dict], title: str) -> Path:
    """The top results as one numbered picture, for the laptop's overlay."""
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    W, H, cols = 1600, 900, 3
    rows = 2
    tiles = items[:cols * rows]
    img = Image.new("RGB", (W, H), (11, 12, 15))
    d = ImageDraw.Draw(img)

    def font(size, bold=True):
        """The system's sans font (fontconfig knows where it is), bold or regular."""
        import subprocess

        try:
            path = subprocess.run(["fc-match", "-f", "%{file}", "sans:bold" if bold else "sans"], capture_output=True,
                                  text=True, timeout=5).stdout.strip()
            f = ImageFont.truetype(path, size)
            try:  # a variable font (Noto Sans on Fedora): pick the weight
                f.set_variation_by_name("Bold" if bold else "Regular")
            except Exception:
                pass
            return f
        except Exception:
            return ImageFont.load_default(size)

    d.text((40, 24), title, font=font(40), fill=(255, 194, 14))
    tw, th, gap, top = (W - 80 - 2 * 24) // 3, (H - 110 - 24) // 2, 24, 90

    def thumb(it):
        for url in (it.get("image"), it.get("image_full")):  # the small one; the full photo if there isn't one
            try:
                raw = _get(url, timeout=15) if url else b""
                if raw:
                    return Image.open(io.BytesIO(raw)).convert("RGB")
            except Exception:
                continue
        return None

    with cf.ThreadPoolExecutor(6) as ex:
        pics = list(ex.map(thumb, tiles))
    for i, (it, pic) in enumerate(zip(tiles, pics)):
        x, y = 40 + (i % cols) * (tw + gap), top + (i // cols) * (th + gap)
        d.rounded_rectangle((x, y, x + tw, y + th), 18, fill=(28, 31, 38))
        ph = th - 92
        if pic:
            pic = ImageOps.fit(pic, (tw, ph))
            mask = Image.new("L", (tw, ph), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, tw, ph + 18), 18, fill=255)
            img.paste(pic, (x, y), mask)
        d.ellipse((x + 12, y + 12, x + 64, y + 64), fill=(255, 194, 14))
        n = str(i + 1)
        d.text((x + 38, y + 38), n, font=font(32), fill=(0, 0, 0), anchor="mm")
        name = it["name"] if len(it["name"]) <= 34 else it["name"][:33] + "…"
        d.text((x + 16, y + ph + 12), name, font=font(26), fill=(243, 238, 226))
        stats = f"{it['site']} · {_k(it['downloads'])} downloads · {_k(it['likes'])} likes"
        d.text((x + 16, y + ph + 50), stats, font=font(20, False), fill=(141, 149, 165))
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / "results.png"
    img.save(p)
    return p


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k".replace(".0k", "k") if n >= 1000 else str(n)


def _spoken(items: list[dict]) -> str:
    top = [f"{i + 1}, {it['name']} from {it['site']}" for i, it in enumerate(items[:3])]
    return "; ".join(top)


# --- talking -----------------------------------------------------------------------------------------------------------
def handle(app, n: str) -> str | None:
    import threading

    mp = getattr(app, "model_preview", None)  # "slice it" about a previewed model: that one, not the last slice
    if mp and mp.get("state") in ("skipped", "asking") and time.time() - mp["t"] < 1800 and \
            re.match(PRE + r"(?:slice|get|download|print) (?:it|that|that one|this)(?: after all| anyway| please)?$", n):
        req = getattr(app, "confirm_request", None)
        if mp["state"] == "asking" and req and not req["done"].is_set():
            app.answer_pending("yes")  # it's still asking "want me to slice it?": that's a yes
            return "Slicing it."
        return slice_it(app)

    last = getattr(app, "model_results", None)
    if last and time.time() - last["t"] < 600:
        m = PICK.match(n)
        num = None
        if m:
            w = m.group("n")
            num = int(w) if w.isdigit() else WORDS[w]
        else:
            m2 = PICK_NAME.match(n)
            if m2:
                from rapidfuzz import fuzz, process

                hit = process.extractOne(m2.group("name"), [it["name"] for it in last["items"]], scorer=fuzz.WRatio)
                num = hit[2] + 1 if hit and hit[1] >= 70 else None
        if num is not None:
            items = last["items"]
            if not items or abs(num) > len(items) or num == 0:
                return f"There are only {len(items)} results."
            last["t"] = 0  # one pick per search: a later "the second one" is about something else
            return pick(app, items[num - 1 if num > 0 else -1])
    q = next((m.group("q") for rx in FIND if (m := rx.match(n))), None)
    if not q:
        return None
    q = re.sub(r"^(?:a|an|some|the)\s+", "", q).strip()

    def go():
        try:
            items = search(q, app.cfg)
        except Exception as e:
            app.announce(f"I couldn't search the model sites: {e}")
            return
        if not items:
            app.announce(f"I couldn't find any {q} models.")
            return
        app.model_results = {"q": q, "items": items, "t": time.time()}
        try:  # the numbered photo cards, on the laptop's screen
            pic = collage(items, f"“{q}” · {len(items)} models · say the number")
            if app.overlay.enabled:
                app.overlay.image(pic, f"{q}: models to print")
            else:
                app._open(pic)
        except Exception:
            log.exception("model collage")
        app.announce(f"I found {len(items)} {q} models. The top ones: {_spoken(items)}. Say the number you want.")

    threading.Thread(target=go, daemon=True, name="model-search").start()
    return f"Searching Printables and MakerWorld for {q}."


BED = (220, 220, 300)  # Ender-5 Pro: X, Y, Z in mm


def measure(path: Path) -> dict:
    """Size (mm), triangles, solid volume, and whether it fits the bed (turned flat if need be)."""
    import trimesh

    m = trimesh.load(str(path), force="mesh")
    x, y, z = (float(v) for v in m.extents)
    fits = z <= BED[2] and ((x <= BED[0] and y <= BED[1]) or (y <= BED[0] and x <= BED[1]))
    return {"name": path.name, "x": round(x, 1), "y": round(y, 1), "z": round(z, 1), "tris": int(len(m.faces)),
            "cm3": round(abs(float(m.volume)) / 1000, 1) if m.is_volume else None, "fits": fits}


def outline(path: Path, info: dict, title: str) -> Path:
    """A light line drawing of the shape (its sharp edges and silhouette) with its measurements: quick to make,
    quick to send, enough to judge it before slicing."""
    import math

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import trimesh
    from mpl_toolkits.mplot3d.art3d import Line3DCollection

    m = trimesh.load(str(path), force="mesh")
    if len(m.faces) > 200_000:  # very fine meshes: a lighter version is plenty for an outline
        m = m.simplify_quadric_decimation(face_count=120_000) if hasattr(m, "simplify_quadric_decimation") else m
    m.apply_translation(-m.bounds[0])
    sharp = m.face_adjacency_edges[m.face_adjacency_angles > math.radians(28)]
    edges = m.vertices[sharp]
    fig = plt.figure(figsize=(12, 6.75), dpi=110, facecolor="#0b0c0f")
    ax = fig.add_axes([0, 0, .64, 1], projection="3d", facecolor="#0b0c0f")
    ax.add_collection3d(Line3DCollection(edges, colors="#ffc20e", linewidths=.7, alpha=.95))
    x, y, z = m.extents
    box = np.array([[0, 0, 0], [x, 0, 0], [x, y, 0], [0, y, 0], [0, 0, z], [x, 0, z], [x, y, z], [0, y, z]])
    for a, b in [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7)]:
        ax.plot(*zip(box[a], box[b]), color="#8d95a5", lw=.6, alpha=.35, ls="--")
    lab = dict(color="#f3eee2", fontsize=13, fontweight="bold", ha="center")
    ax.text(x / 2, -0.12 * max(x, y), 0, f"{info['x']:g} mm", **lab)
    ax.text(x + 0.12 * max(x, y), y / 2, 0, f"{info['y']:g} mm", **lab)
    ax.text(-0.08 * max(x, y), 0, z / 2, f"{info['z']:g} mm", **lab)
    span = max(x, y, z)
    ax.set_xlim(x / 2 - span / 2, x / 2 + span / 2); ax.set_ylim(y / 2 - span / 2, y / 2 + span / 2); ax.set_zlim(0, span)
    ax.set_box_aspect((1, 1, 1)); ax.view_init(elev=24, azim=-58); ax.set_axis_off()
    side = fig.add_axes([.64, 0, .36, 1]); side.set_axis_off()
    side.text(0, .86, title if len(title) < 34 else title[:33] + "…", color="#ffc20e", fontsize=17, fontweight="bold")
    rows = [("Size", f"{info['x']:g} × {info['y']:g} × {info['z']:g} mm"),
            ("Bed", "fits ✓" if info["fits"] else "too big ✗  (220 × 220 × 300)"),
            ("Solid volume", f"{info['cm3']} cm³" if info["cm3"] else "not a closed shape"),
            ("Parts", str(info.get("parts", 1))), ("Detail", f"{info['tris']:,} triangles")]
    for i, (k, v) in enumerate(rows):
        side.text(0, .72 - i * .1, k, color="#8d95a5", fontsize=12)
        side.text(0, .67 - i * .1, v, color="#f3eee2" if k != "Bed" else ("#3ccf6e" if info["fits"] else "#ff5c4d"),
                  fontsize=15, fontweight="bold")
    side.text(0, .12, "Say “slice it” (or tap Slice on the phone)", color="#8d95a5", fontsize=12)
    out = OUT / f"preview-{re.sub(r'[^a-z0-9]+', '-', title.lower())[:40]}.png"
    fig.savefig(out, facecolor=fig.get_facecolor())
    plt.close(fig)
    return out


def preview(path: Path, title: str, parts: int = 1) -> tuple[dict, Path]:
    """measure() + outline(), in a short-lived process: trimesh, matplotlib and the mesh don't stay in Batsy."""
    import subprocess
    import sys

    r = subprocess.run([sys.executable, "-m", "batsy.printsearch", "preview", str(path), title, str(parts)],
                       capture_output=True, text=True, timeout=180)
    if r.returncode:
        raise RuntimeError(r.stderr.strip()[-300:] or "couldn't read the model")
    out = json.loads(r.stdout.strip().splitlines()[-1])
    out["info"]["mesh"] = out.get("mesh", "")
    return out["info"], Path(out["image"])


def phone_mesh(path: Path, out: Path, faces: int = 120_000) -> Path:
    """The model as a binary STL light enough for a phone to turn smoothly (fewer triangles if it's very fine)."""
    import trimesh

    m = trimesh.load(str(path), force="mesh")
    if len(m.faces) > faces and hasattr(m, "simplify_quadric_decimation"):
        try:
            m = m.simplify_quadric_decimation(face_count=faces)
        except Exception:
            pass
    m.export(str(out), file_type="stl")
    return out


def pick(app, item: dict) -> str:
    """Picked: fetch just the file, show its size and outline (laptop + phone), and slice only if you say so."""
    import shutil
    import subprocess
    import threading

    try:
        from batsy import vault_capture

        vault_capture.model_picked(item)
    except Exception:
        pass
    if not item["can_download"]:
        subprocess.Popen(["xdg-open", item["url"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return (f"{item['name']} is on MakerWorld, which needs your Bambu login to download. I've opened its page: "
                f"download it there and send it to me, and I'll slice it.")

    def go():
        try:
            stage = OUT / f"{item['site']}-{item['id']}"
            files = download(item, app.cfg, stage.parent, folder=stage)
            if not files:
                app.announce(f"{item['name']} has no STL or 3MF files I can download.")
                return
            parts = [f for f in files if f.suffix.lower() in (".stl", ".3mf", ".obj")]
            if not parts:
                app.announce(f"{item['name']} has no STL or 3MF files I can download.")
                return
            info, pic = preview(parts[0], item["name"], len(parts))
            app.model_preview = {"t": time.time(), "item": item, "info": info, "image": str(pic),
                                 "files": [str(f) for f in files], "state": "asking"}
            try:  # on the laptop: the model itself, in Batsy's 3D viewer (turn it, "top view", "how big is it")
                from batsy import model3d

                model3d.VIEWER.open(parts[0])
            except Exception:
                if app.overlay.enabled:
                    app.overlay.image(pic, item["name"])
            size = f"{info['x']:g} by {info['y']:g} by {info['z']:g} millimetres"
            fit = "It fits your bed." if info["fits"] else "It's too big for your bed as it is."
            more = f" It comes in {len(parts)} parts." if len(parts) > 1 else ""
            yes = app.ask_from_background(f"{item['name']} is {size}.{more} {fit} Want me to slice it?")
            app.model_preview["state"] = "slicing" if yes else "skipped"
            if not yes:
                app.announce("Okay, it's saved if you change your mind: say slice it.")
                return
            slice_it(app)
        except Exception as e:
            log.exception("model preview")
            app.announce(f"I couldn't get {item['name']}: {e}")

    threading.Thread(target=go, daemon=True, name="model-preview").start()
    return f"Getting {item['name']}: I'll show you its size and shape first."


def slice_it(app) -> str:
    """Hand the previewed model to the inbox: sliced with your presets, shown in 3D, ready for "print it"."""
    import shutil

    mp = getattr(app, "model_preview", None)
    if not mp:
        return "There's no model to slice."
    inbox = Path(app.cfg.get("files.inbox")).expanduser() / mp["item"]["site"]
    inbox.mkdir(parents=True, exist_ok=True)
    for f in mp["files"]:
        tmp = inbox / f".{Path(f).name}.part"
        shutil.copy2(f, tmp)
        tmp.replace(inbox / Path(f).name)  # appears whole: the inbox picks it up and slices it
    mp["state"] = "slicing"
    return f"Slicing {mp['item']['name']}."


if __name__ == "__main__":  # python -m batsy.printsearch preview FILE TITLE PARTS  (see preview())
    import sys

    if sys.argv[1] == "preview":
        f, t, n = Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
        info = measure(f)
        info["parts"] = n
        img = outline(f, info, t)
        print(json.dumps({"info": info, "image": str(img), "mesh": str(phone_mesh(f, img.with_suffix(".stl")))}))

