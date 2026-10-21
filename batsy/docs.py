"""Documents and pictures on this laptop: find "this file", read it, learn it, and mark it up.

Reading is local: PDF text via PyMuPDF, OCR via Tesseract for images and scans, docx and text files directly.
Marking up is precise: text to mark is located exactly (PDF text positions, OCR word boxes); shapes use
coordinates from `render` (the same pixels Claude looked at). Results go to ~/Batsy/docs/ and the original
is never modified. Also a CLI (`batsy doc ...`) so Claude Code can drive it and check its own work.
"""
import json
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from batsy.config import HOME, WORKSPACE

OUT_DIR = WORKSPACE / "docs"
PDF = {".pdf"}
IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
TEXT = {".txt", ".md", ".csv", ".json", ".py", ".ino", ".c", ".cpp", ".h", ".html", ".log"}
OFFICE = {".docx"}
SUPPORTED = PDF | IMAGES | TEXT | OFFICE
KIND_EXT = {"pdf": PDF, "image": IMAGES, "picture": IMAGES, "photo": IMAGES, "pic": IMAGES, "screenshot": IMAGES,
            "png": {".png"}, "jpg": {".jpg", ".jpeg"}, "jpeg": {".jpg", ".jpeg"}, "docx": OFFICE, "doc": PDF | OFFICE,
            "document": PDF | OFFICE | TEXT, "file": SUPPORTED, "paper": PDF | OFFICE, "notes": SUPPORTED,
            "slides": PDF, "slide": PDF, "worksheet": SUPPORTED, "assignment": SUPPORTED, "scan": PDF | IMAGES}
COLORS = {"yellow": (1, 0.9, 0), "red": (0.9, 0.1, 0.1), "green": (0.1, 0.7, 0.2), "blue": (0.1, 0.4, 0.95),
          "orange": (1, 0.55, 0), "purple": (0.6, 0.2, 0.85), "pink": (1, 0.3, 0.7), "black": (0, 0, 0)}
RENDER_ZOOM = 2.0  # PDF pages are rendered at 2x: plan coordinates for PDFs are in these pixels
SKIP_DIRS = {"node_modules", "venv", ".venv", "__pycache__", "site-packages", ".git", "snap", "flatpak"}

# --- which file ---------------------------------------------------------------------------------------------
DOC_VERBS = re.compile(
    r"\b(see|open|read|look|check|learn|study|go through|summari[sz]e|explain|mark|circle|highlight|underline|"
    r"box|strike|cross|annotate|note|label|point|arrow|redact|blur|hide|crop|rotate|resize|convert|extract|"
    r"translate|fill|sign|compress|merge|split|count|find|correct|grade|solve|answer|edit|fix|what|tell)\b")
EDIT_VERBS = re.compile(r"\b(mark|circle|highlight|underline|box|strike|cross|annotate|note|label|point|arrow|"
                        r"redact|blur|hide|crop|rotate|resize|convert|extract|fill|sign|compress|merge|split|"
                        r"correct|grade|solve|edit|fix|translate)\b")


KIND_WORD = re.compile(r"\b(pdfs?|documents?|docx?|files?|images?|pictures?|photos?|pics?|screenshots?|papers?|notes|"
                       r"slides?|worksheets?|assignments?|scans?|png|jpe?g)\b")
DETERMINER = {"this", "that", "the", "my", "these", "those", "our"}


def parse_request(text: str) -> tuple[str | None, str] | None:
    """'circle the dates in my physics notes pdf' -> ('physics notes', 'pdf'). None if it's not about a file."""
    t = re.sub(r"[^a-z0-9' ]", " ", text.lower())
    if not DOC_VERBS.search(t):
        return None
    words = t.split()
    kinds = [i for i, w in enumerate(words) if KIND_WORD.fullmatch(w)]
    for k in reversed(kinds):  # the last file word is the real one: "my physics notes pdf" -> pdf
        for j in range(k - 1, max(-1, k - 6), -1):  # nearest determiner before it
            if words[j] in DETERMINER:
                name = " ".join(words[j + 1:k]).strip()
                kind = words[k] if words[k] == "notes" else words[k].rstrip("s")
                if name in ("", "same", "last", "current", "open", "new") or KIND_WORD.fullmatch(name or "x"):
                    name = None
                return name, kind
    return None


def recent_files(exts: set[str] = SUPPORTED, limit: int = 30) -> list[Path]:
    """Files GNOME apps opened recently (Papers/Evince, Loupe, LibreOffice...), newest first."""
    xbel = HOME / ".local/share/recently-used.xbel"
    out = []
    if xbel.exists():
        try:
            for b in ET.parse(xbel).getroot().iter("bookmark"):
                href = b.get("href", "")
                if href.startswith("file://"):
                    p = Path(urllib.parse.unquote(href[7:]))
                    if p.suffix.lower() in exts and p.exists():
                        out.append((b.get("visited") or b.get("modified") or "", p))
        except ET.ParseError:
            pass
    out.sort(key=lambda x: x[0], reverse=True)
    seen, files = set(), []
    for _, p in out:
        if p not in seen:
            seen.add(p)
            files.append(p)
    # plus anything new in the usual drop folders
    for d in (HOME / "Downloads", HOME / "Desktop", HOME / "Pictures/Screenshots", HOME / "Documents"):
        if d.is_dir():
            for p in sorted(d.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)[:10]:
                if p.suffix.lower() in exts and p not in seen and time.time() - p.stat().st_mtime < 3 * 3600:
                    seen.add(p)
                    files.append(p)
    return files[:limit]


def search_files(name: str, exts: set[str], max_files: int = 40000) -> list[tuple[float, Path]]:
    import difflib

    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) > 1]
    scored, n = [], 0
    for root, dirs, files in os.walk(HOME):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
        for f in files:
            n += 1
            if n > max_files:
                break
            p = Path(root) / f
            if p.suffix.lower() not in exts:
                continue
            stem = re.sub(r"[_\-.]+", " ", p.stem.lower())
            hit = sum(1 for w in words if w in stem) / max(1, len(words))
            score = hit + 0.5 * difflib.SequenceMatcher(None, " ".join(words), stem).ratio()
            if hit > 0:
                scored.append((score, p))
    return sorted(scored, key=lambda x: (x[0], x[1].stat().st_mtime), reverse=True)


def resolve_file(name: str | None, kind: str | None) -> Path | None:
    exts = KIND_EXT.get(kind or "file", SUPPORTED)
    if name:
        recent = [p for p in recent_files(exts) if all(w in p.stem.lower().replace("_", " ")
                                                         for w in name.lower().split())]
        if recent:
            return recent[0]
        found = search_files(name, exts)
        return found[0][1] if found and found[0][0] >= 0.6 else None
    recent = recent_files(exts)
    return recent[0] if recent else None


# --- reading ------------------------------------------------------------------------------------------------
def _pdf(path: Path):
    import pymupdf

    return pymupdf.open(str(path))


def _ocr_data(img: Image.Image) -> list[dict]:
    """Word boxes from Tesseract: [{text, x0, y0, x1, y1, line}]."""
    try:
        import pytesseract
    except ImportError:
        return []
    d = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    words = []
    for i, w in enumerate(d["text"]):
        if w.strip() and float(d["conf"][i]) > 30:
            words.append({"text": w, "x0": d["left"][i], "y0": d["top"][i], "x1": d["left"][i] + d["width"][i],
                          "y1": d["top"][i] + d["height"][i],
                          "line": (d["block_num"][i], d["par_num"][i], d["line_num"][i])})
    return words


def info(path: Path) -> dict:
    ext = path.suffix.lower()
    out = {"path": str(path), "name": path.name, "type": ext.lstrip("."), "size_kb": round(path.stat().st_size / 1024)}
    if ext in PDF:
        doc = _pdf(path)
        out.update(pages=doc.page_count, title=(doc.metadata or {}).get("title") or "",
                   has_text=any(doc[i].get_text().strip() for i in range(min(3, doc.page_count))))
    elif ext in IMAGES:
        with Image.open(path) as im:
            out.update(width=im.width, height=im.height)
    return out


def extract_text(path: Path, max_chars: int = 60000) -> list[tuple[int, str]]:
    """[(page number, text)]. Images and scanned pages are OCR'd."""
    ext = path.suffix.lower()
    pages = []
    if ext in PDF:
        doc = _pdf(path)
        for i, page in enumerate(doc):
            t = page.get_text().strip()
            if not t:  # scanned page
                t = " ".join(w["text"] for w in _ocr_data(_page_image(page)))
            pages.append((i + 1, t))
            if sum(len(p[1]) for p in pages) > max_chars:
                break
    elif ext in IMAGES:
        with Image.open(path) as im:
            pages.append((1, " ".join(w["text"] for w in _ocr_data(im.convert("RGB")))))
    elif ext in OFFICE:
        import docx

        pages.append((1, "\n".join(p.text for p in docx.Document(str(path)).paragraphs)))
    else:
        pages.append((1, path.read_text(errors="replace")[:max_chars]))
    return pages


def _page_image(page, zoom: float = RENDER_ZOOM) -> Image.Image:
    import pymupdf

    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def render(path: Path, page: int = 1, grid: bool = False, out: Path | None = None) -> Path:
    """PNG of a PDF page (at 2x) or of an image, optionally with a labelled pixel grid for placing shapes."""
    ext = path.suffix.lower()
    img = _page_image(_pdf(path)[page - 1]) if ext in PDF else Image.open(path).convert("RGB")
    if grid:
        img = img.copy()
        d = ImageDraw.Draw(img)
        step = max(50, (max(img.size) // 12) // 50 * 50)
        font = _font(max(12, step // 5))
        for x in range(0, img.width, step):
            d.line([(x, 0), (x, img.height)], fill=(255, 0, 180), width=1)
            d.text((x + 2, 2), str(x), fill=(255, 0, 180), font=font)
        for y in range(0, img.height, step):
            d.line([(0, y), (img.width, y)], fill=(255, 0, 180), width=1)
            d.text((2, y + 2), str(y), fill=(255, 0, 180), font=font)
    out = out or OUT_DIR / ".render" / f"{path.stem}-p{page}{'-grid' if grid else ''}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def find(path: Path, text: str, page: int | None = None) -> list[dict]:
    """Exact boxes of `text` in render-pixel coordinates: [{page, bbox: [x0, y0, x1, y1]}]."""
    ext = path.suffix.lower()
    hits = []
    if ext in PDF:
        doc = _pdf(path)
        for i in ([page - 1] if page else range(doc.page_count)):
            pg = doc[i]
            rects = pg.search_for(text)
            if not rects and not pg.get_text().strip():  # scanned: OCR the page
                hits += [{"page": i + 1, "bbox": b} for b in _ocr_find(_page_image(pg), text)]
            for r in rects:
                hits.append({"page": i + 1, "bbox": [round(v * RENDER_ZOOM) for v in (r.x0, r.y0, r.x1, r.y1)]})
    elif ext in IMAGES:
        with Image.open(path) as im:
            hits = [{"page": 1, "bbox": b} for b in _ocr_find(im.convert("RGB"), text)]
    return hits


def _ocr_find(img: Image.Image, text: str) -> list[list[int]]:
    target = [re.sub(r"\W", "", w.lower()) for w in text.split() if re.sub(r"\W", "", w)]
    words = _ocr_data(img)
    norm = [re.sub(r"\W", "", w["text"].lower()) for w in words]
    boxes = []
    for i in range(len(words) - len(target) + 1):
        if norm[i:i + len(target)] == target:
            span = words[i:i + len(target)]
            boxes.append([min(w["x0"] for w in span), min(w["y0"] for w in span),
                          max(w["x1"] for w in span), max(w["y1"] for w in span)])
    return boxes


# --- marking up ---------------------------------------------------------------------------------------------
def _font(size: int):
    for f in ("/usr/share/fonts/google-noto/NotoSans-Bold.ttf", "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/liberation-sans/LiberationSans-Bold.ttf", "/System/Library/Fonts/Helvetica.ttc"):
        if os.path.exists(f):
            return ImageFont.truetype(f, size)
    return ImageFont.load_default()


def _out_path(path: Path, suffix: str | None = None) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ext = suffix or path.suffix.lower()
    base = OUT_DIR / f"{path.stem}-marked{ext}"
    i = 2
    while base.exists():
        base = OUT_DIR / f"{path.stem}-marked-{i}{ext}"
        i += 1
    return base


def _targets(path: Path, action: dict) -> tuple[list[dict], str | None]:
    """Boxes an action applies to: from its exact `text` (searched) or its `bbox`."""
    if action.get("text") and action["type"] not in ("note", "label"):
        hits = find(path, action["text"], action.get("page"))
        if action.get("occurrence"):
            hits = hits[action["occurrence"] - 1: action["occurrence"]]
        return hits, None if hits else f"text not found: {action['text']!r}"
    if action.get("bbox"):
        return [{"page": action.get("page", 1), "bbox": action["bbox"]}], None
    if action.get("at"):
        x, y = action["at"]
        return [{"page": action.get("page", 1), "bbox": [x, y, x + 1, y + 1]}], None
    return [], "needs text, bbox or at"


def annotate(path: Path, plan: dict | list) -> dict:
    """Apply a list of mark-up actions; returns {output, applied, problems}.

    Action: {"type": highlight|underline|strike|circle|box|note|label|arrow|redact,
             "text": exact words to find (or) "bbox": [x0,y0,x1,y1] in render pixels, "page": n,
             "color": yellow|red|green|blue|orange|purple|pink|black, "note"/"label" text, "from"/"to" for arrows,
             "occurrence": k (only the k-th match)}
    """
    actions = plan.get("actions", []) if isinstance(plan, dict) else plan
    ext = path.suffix.lower()
    applied, problems = 0, []
    if ext in PDF:
        import pymupdf

        doc = _pdf(path)
        s = 1 / RENDER_ZOOM
        for a in actions:
            color = COLORS.get(a.get("color", ""), COLORS["red"] if a["type"] in ("circle", "box", "arrow") else COLORS["yellow"])
            if a["type"] == "arrow":
                pg = doc[a.get("page", 1) - 1]
                an = pg.add_line_annot(pymupdf.Point(*[v * s for v in a["from"]]), pymupdf.Point(*[v * s for v in a["to"]]))
                an.set_line_ends(pymupdf.PDF_ANNOT_LE_NONE, pymupdf.PDF_ANNOT_LE_CLOSED_ARROW)
                an.set_colors(stroke=color, fill=color)
                an.set_border(width=2)
                an.update()
                applied += 1
                continue
            hits, err = _targets(path, a)
            if err:
                problems.append(err)
            for h in hits:
                pg = doc[h["page"] - 1]
                r = pymupdf.Rect(*[v * s for v in h["bbox"]])
                t = a["type"]
                if t in ("highlight", "underline", "strike"):
                    an = {"highlight": pg.add_highlight_annot, "underline": pg.add_underline_annot,
                          "strike": pg.add_strikeout_annot}[t](r)
                    an.set_colors(stroke=color)
                elif t == "circle":
                    an = pg.add_circle_annot(r + (-4, -4, 4, 4))
                    an.set_colors(stroke=color)
                    an.set_border(width=a.get("width", 2))
                elif t == "box":
                    an = pg.add_rect_annot(r + (-3, -3, 3, 3))
                    an.set_colors(stroke=color)
                    an.set_border(width=a.get("width", 2))
                elif t == "note":
                    an = pg.add_text_annot(r.tl, a.get("note") or a.get("text", ""))
                elif t == "label":
                    box = pymupdf.Rect(r.x0, r.y0, r.x0 + max(120, 7 * len(a.get("text", ""))), r.y0 + 22)
                    an = pg.add_freetext_annot(box, a.get("text", ""), fontsize=11, text_color=color,
                                               fill_color=(1, 1, 1))
                elif t == "redact":
                    pg.add_redact_annot(r, fill=(0, 0, 0))
                    pg.apply_redactions()
                    applied += 1
                    continue
                else:
                    problems.append(f"unknown action {t}")
                    continue
                an.update()
                applied += 1
        out = _out_path(path)
        doc.save(str(out), garbage=3, deflate=True)
    elif ext in IMAGES:
        img = Image.open(path).convert("RGB")
        over = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(over)
        lw = max(3, max(img.size) // 250)
        for a in actions:
            rgb = tuple(int(c * 255) for c in COLORS.get(a.get("color", ""), COLORS["red"] if a["type"] != "highlight" else COLORS["yellow"]))
            if a["type"] == "arrow":
                (x0, y0), (x1, y1) = a["from"], a["to"]
                d.line([(x0, y0), (x1, y1)], fill=rgb + (255,), width=lw)
                import math

                ang = math.atan2(y1 - y0, x1 - x0)
                head = lw * 5
                d.polygon([(x1, y1), (x1 - head * math.cos(ang - 0.4), y1 - head * math.sin(ang - 0.4)),
                           (x1 - head * math.cos(ang + 0.4), y1 - head * math.sin(ang + 0.4))], fill=rgb + (255,))
                applied += 1
                continue
            hits, err = _targets(path, a)
            if err:
                problems.append(err)
            for h in hits:
                x0, y0, x1, y1 = h["bbox"]
                t = a["type"]
                pad = lw * 2
                if t == "highlight":
                    d.rectangle([x0, y0, x1, y1], fill=rgb + (90,))
                elif t == "underline":
                    d.line([(x0, y1 + lw), (x1, y1 + lw)], fill=rgb + (255,), width=lw)
                elif t == "strike":
                    d.line([(x0, (y0 + y1) / 2), (x1, (y0 + y1) / 2)], fill=rgb + (255,), width=lw)
                elif t == "circle":
                    d.ellipse([x0 - pad * 2, y0 - pad * 2, x1 + pad * 2, y1 + pad * 2], outline=rgb + (255,), width=lw)
                elif t == "box":
                    d.rectangle([x0 - pad, y0 - pad, x1 + pad, y1 + pad], outline=rgb + (255,), width=lw)
                elif t == "redact":
                    d.rectangle([x0, y0, x1, y1], fill=(0, 0, 0, 255))
                elif t in ("note", "label"):
                    label = a.get("note") or a.get("text", "")
                    font = _font(max(16, max(img.size) // 45))
                    tb = d.textbbox((x0, y0), label, font=font)
                    d.rectangle([tb[0] - 6, tb[1] - 4, tb[2] + 6, tb[3] + 4], fill=(255, 255, 255, 230),
                                outline=rgb + (255,), width=2)
                    d.text((x0, y0), label, fill=rgb + (255,), font=font)
                else:
                    problems.append(f"unknown action {t}")
                    continue
                applied += 1
        out = _out_path(path, ".png" if path.suffix.lower() in (".gif", ".webp", ".bmp", ".tif", ".tiff") else None)
        Image.alpha_composite(img.convert("RGBA"), over).convert("RGB").save(out)
    else:
        return {"output": None, "applied": 0, "problems": ["mark-up works on PDFs and images; convert this file first"]}
    return {"output": str(out), "applied": applied, "problems": problems}


# --- learning (local memory) --------------------------------------------------------------------------------
def learn(path: Path) -> str:
    """Read the file and store its text in local memory so later questions can use it. Returns a short note."""
    from batsy.memory_store import get_store

    pages = extract_text(path)
    get_store().add_document(str(path), path.name, pages)
    words = sum(len(t.split()) for _, t in pages)
    return f"{len(pages)} page{'s' if len(pages) != 1 else ''}, about {words} words"


# --- CLI for Claude Code: `batsy doc <cmd> ...` -------------------------------------------------------------
def cli(argv: list[str]) -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="batsy doc")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("info", "text"):
        sub.add_parser(c).add_argument("file")
    p = sub.add_parser("render")
    p.add_argument("file")
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--grid", action="store_true")
    p = sub.add_parser("find")
    p.add_argument("file")
    p.add_argument("text")
    p.add_argument("--page", type=int)
    p = sub.add_parser("annotate")
    p.add_argument("file")
    p.add_argument("plan", help="JSON string, or @path/to/plan.json")
    a = ap.parse_args(argv)
    f = Path(os.path.expanduser(a.file))
    if a.cmd == "info":
        print(json.dumps(info(f), indent=1))
    elif a.cmd == "text":
        for n, t in extract_text(f):
            print(f"--- page {n} ---\n{t}")
    elif a.cmd == "render":
        print(render(f, a.page, a.grid))
    elif a.cmd == "find":
        print(json.dumps(find(f, a.text, a.page)))
    elif a.cmd == "annotate":
        plan = json.loads(Path(a.plan[1:]).read_text() if a.plan.startswith("@") else a.plan)
        print(json.dumps(annotate(f, plan), indent=1))
