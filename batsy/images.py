"""Find the *right* picture and facts for something.

Not "top search result": candidates come from the live web (DuckDuckGo images: current photos, pinouts,
diagrams), the exact Wikipedia entity's editor-chosen image, and Wikimedia Commons. Each is scored on
match, source trust (e.g. Adafruit / Random Nerd Tutorials for pinouts; stock-photo sites penalised),
curated quality, resolution and shape. For technical images (pinouts, schematics...) Claude Code then
looks at the top few and picks the correct, legible one; the screen updates if it disagrees.
Facts come from the entity's encyclopedia summary, not a search snippet.
"""
import difflib
import functools
import hashlib
import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from batsy.config import DATA_DIR, WORKSPACE

CACHE = DATA_DIR / "images"
UA = {"User-Agent": "Batsy/0.2 (personal voice assistant)"}
MAX_BYTES = 20_000_000
WIKI = "https://en.wikipedia.org"
COMMONS = "https://commons.wikimedia.org/w/api.php"
STOP = {"a", "an", "the", "of", "in", "on", "at", "for", "and", "picture", "photo", "image", "pic", "me", "some"}


@functools.lru_cache(maxsize=256)
def _get_cached(full: str) -> str:
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(full, headers=UA), timeout=10) as r:
                return r.read().decode()
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 2:
                raise
            time.sleep(min(5.0, float(e.headers.get("Retry-After") or 1.5 * (attempt + 1))))
    raise RuntimeError("unreachable")


def _get(url: str, params: dict | None = None) -> dict:
    return json.loads(_get_cached(url + ("?" + urllib.parse.urlencode(params) if params else "")))


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOP}


def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


@dataclass
class Entity:
    title: str
    description: str
    summary: str
    lead_image: str | None  # the image Wikipedia editors chose for the article
    url: str


def resolve(query: str) -> Entity | None:
    """Pick the Wikipedia article the query most plausibly means (not simply hit #1)."""
    res = _get(f"{WIKI}/w/api.php", {
        "action": "query", "format": "json", "list": "search", "srsearch": query, "srlimit": 6,
        "srprop": "", "srwhat": "text",
    }).get("query", {}).get("search", [])
    if not res:
        return None
    qw = _words(query)
    best, best_score = None, -1.0
    for rank, hit in enumerate(res):
        title = hit["title"]
        if "disambiguation" in title.lower() or title.lower().startswith("list of"):
            continue
        tw = _words(title)
        overlap = len(qw & tw) / max(1, len(tw))
        score = 1.4 * _sim(query, title) + overlap - 0.12 * rank
        if score > best_score:
            best, best_score = title, score
    if best is None:
        return None
    s = _get(f"{WIKI}/api/rest_v1/page/summary/{urllib.parse.quote(best.replace(' ', '_'))}")
    if s.get("type") == "disambiguation":
        return None
    orig = (s.get("originalimage") or {}).get("source")
    thumb = (s.get("thumbnail") or {}).get("source")
    img = thumb if not orig or orig.lower().endswith(".svg") else orig  # svg logos: use the rendered png
    return Entity(s.get("title", best), s.get("description", ""), s.get("extract", ""), img,
                  s.get("content_urls", {}).get("desktop", {}).get("page", ""))


def _commons(query: str, limit: int = 12) -> list[dict]:
    data = _get(COMMONS, {
        "action": "query", "format": "json", "generator": "search", "gsrsearch": f"{query} filetype:bitmap",
        "gsrnamespace": 6, "gsrlimit": limit, "prop": "imageinfo|categories", "iiprop": "url|size|extmetadata",
        "iiurlwidth": 1800, "cllimit": 50, "clshow": "!hidden",
    })
    out = []
    for pg in data.get("query", {}).get("pages", {}).values():
        info = (pg.get("imageinfo") or [{}])[0]
        if not info.get("url"):
            continue
        cats = " ".join(c["title"].lower() for c in pg.get("categories", []))
        meta = info.get("extmetadata", {})
        desc = re.sub(r"<[^>]+>", "", meta.get("ImageDescription", {}).get("value", ""))[:300]
        out.append({
            "url": info.get("thumburl") or info["url"], "w": info.get("width", 0), "h": info.get("height", 0),
            "title": pg["title"].removeprefix("File:").rsplit(".", 1)[0], "desc": desc,
            "rank": pg.get("index", 99),
            "quality": 3 if "featured pictures" in cats else 2 if "quality images" in cats else 1.5 if "valued images" in cats else 0,
        })
    return out


def _score(c: dict, query: str, entity: Entity | None) -> float:
    qw = _words(query)
    text = _words(c["title"] + " " + c["desc"])
    match = len(qw & text) / max(1, len(qw))
    ent = 0.0
    if entity:
        ent = len(_words(entity.title) & text) / max(1, len(_words(entity.title)))
    w, h = c["w"], c["h"]
    res = min(1.0, (w * h) / (1600 * 1000)) if w and h else 0.3
    shape = 1.0 if w and h and 0.5 <= w / h <= 2.2 else 0.4  # skip panoramas, strips and tall scans
    return 2.5 * match + 1.5 * ent + 0.6 * c["quality"] + 0.8 * res + 0.5 * shape - 0.04 * c["rank"]


def _thumb(url: str, width: int = 1280) -> str | None:
    """Wikimedia thumbnail URL for an original upload URL (originals can be 6000px / 15 MB)."""
    m = re.match(r"(https://upload\.wikimedia\.org/wikipedia/[^/]+)/(?!thumb/)(.+/)([^/]+)$", url)
    return f"{m.group(1)}/thumb/{m.group(2)}{m.group(3)}/{width}px-{m.group(3)}" if m else None


def download(url: str) -> Path:
    """Fetch an image URL into the cache as JPEG (validated by Pillow)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    out = CACHE / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".jpg")
    if out.exists():
        return out
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("image too large")
    img = Image.open(io.BytesIO(data))
    img = (img.convert("RGBA") if img.mode in ("P", "LA") else img).convert("RGB")
    if min(img.size) < 200:
        raise ValueError("image too small")
    img.thumbnail((2400, 2400))
    img.save(out, "JPEG", quality=90)
    return out


TECHNICAL = re.compile(r"\b(pin ?outs?|pin diagram|pins|diagram|schematic|wiring|circuit|datasheet|footprint|"
                       r"block diagram|flow ?chart|chart|graph|infographic|map|layout|dimensions|blueprint|"
                       r"cheat ?sheet|logo|screenshot|interface|connections?|module|sensor|board|chip|ic)\b")
RECENT = re.compile(r"\b(latest|newest|new|current|recent|today|this year|20[2-3]\d)\b")
TRUSTED = re.compile(r"(randomnerdtutorials|lastminuteengineers|components101|electronicwings|adafruit|sparkfun|"
                     r"arduino\.cc|espressif|raspberrypi|pinout\.xyz|circuitdigest|microchip|st\.com|ti\.com|"
                     r"nordicsemi|seeedstudio|wokwi|instructables|hackster|electronicshub|theengineeringprojects|"
                     r"wikipedia|wikimedia|nasa\.gov|reuters|apnews|bbc|nationalgeographic|britannica)")
STOCK = re.compile(r"(shutterstock|alamy|istockphoto|gettyimages|dreamstime|depositphotos|123rf|vecteezy|"
                   r"freepik|aliexpress|alibaba|temu|pinimg|pinterest|ebay)")
# image-scraping farms that re-host other sites' pictures (often wrong or low quality)
JUNK = re.compile(r"(narodnatribuna|inspiredpencil|detailspin|wiringdiagram|wiring-diagram|diagramweb|schematron|"
                  r"/lists/|pictures-20\d\d|wallpaper|clipart|\.edu\.pe/post|blogspot|imgur)")


def _web(query: str, limit: int = 25) -> list[dict]:
    """Live web image search (no API key)."""
    try:
        from ddgs import DDGS
    except ImportError:
        return []
    out = []
    for i, r in enumerate(DDGS().images(query, max_results=limit) or []):
        if not r.get("image"):
            continue
        out.append({"url": r["image"], "alt": r.get("thumbnail"), "w": int(r.get("width") or 0),
                    "h": int(r.get("height") or 0), "title": r.get("title", ""), "desc": r.get("url", ""),
                    "source": (r.get("source") or "") + " " + (r.get("url") or ""), "rank": i, "quality": 0})
    return out


def _source_bonus(c: dict) -> float:
    src = (c.get("source") or "") + " " + c["url"]
    return (1.2 if TRUSTED.search(src) else 0.0) - (2.5 if STOCK.search(src) else 0.0) - \
        (2.0 if JUNK.search(src) else 0.0)


def best_image(query: str, entity: Entity | None = None, on_better=None) -> tuple[Path, str]:
    """The best picture for the query: (local path, caption). on_better(path, caption) is called later
    if Claude, looking at the top candidates, picks a better one (technical images only)."""
    technical = bool(TECHNICAL.search(query.lower()))
    try:
        entity = entity or (None if technical else resolve(query))
    except Exception:
        entity = None
    extra = _words(query) - (_words(entity.title) if entity else set())
    candidates = []
    try:
        candidates += _web(query)
    except Exception:
        pass
    # The article's own lead image is hand-picked by editors: best when the query is just the thing itself.
    if entity and entity.lead_image:
        candidates.append({"url": entity.lead_image, "alt": _thumb(entity.lead_image), "w": 1600, "h": 1100, "title": entity.title, "desc": entity.title,
                           "rank": 0, "quality": 2.2 if not extra else 0.5, "caption": entity.title})
    # Commons covers specifics ("at night", "interior", "1969"): search with the resolved name plus the extras.
    if not technical:
        search = f"{entity.title} {' '.join(sorted(extra))}".strip() if entity else query
        try:
            candidates += _commons(search)
        except Exception:
            pass
    if not candidates:
        raise LookupError(f"No good picture found for '{query}'.")
    ranked = sorted(candidates, key=lambda c: _score(c, query, entity) + _source_bonus(c), reverse=True)

    def caption_for(c: dict) -> str:
        if c.get("caption"):
            return c["caption"]
        if entity:
            return f"{entity.title} ({' '.join(sorted(extra))})" if extra else entity.title
        return query.strip().capitalize()

    def fetch(c: dict, thumb: bool) -> Path | None:
        if thumb:  # last resort: small preview image
            url = c.get("alt")
        elif c.get("source", "").strip() == "wikipedia":
            url = c.get("alt") or c["url"]  # a sensibly sized Wikimedia thumbnail
        else:
            url = c["url"]  # full image: thumbnails are too small to read a pinout
        try:
            return download(url) if url else None
        except Exception:
            return None

    top = ranked[:8]
    first = None
    for thumb in (False, True):  # every full-size candidate before any thumbnail
        for c in top:
            path = fetch(c, thumb)
            if path:
                first = (path, c)
                break
        if first:
            break
    if not first:
        raise LookupError(f"Couldn't download a picture for '{query}'.")

    if technical and on_better:
        import threading
        from concurrent.futures import ThreadPoolExecutor

        def verify():  # in the background: the first pick is already on screen
            rest = [c for c in top if c is not first[1]][:5]
            with ThreadPoolExecutor(4) as pool:
                got = [(p, c) for p, c in zip(pool.map(lambda c: fetch(c, False), rest), rest) if p][:3]
            options = [first] + got
            if len(options) < 2:
                return
            pick = claude_pick(query, [p for p, _ in options])
            if pick and 1 < pick <= len(options):
                path, c = options[pick - 1]
                on_better(path, caption_for(c))

        threading.Thread(target=verify, daemon=True, name="image-verify").start()
    return first[0], caption_for(first[1])


def claude_pick(query: str, paths: list[Path]) -> int | None:
    """Let Claude Code look at candidate images and choose the right one (1-based), or None."""
    from batsy import claude_code

    vis = WORKSPACE / ".vision"
    vis.mkdir(parents=True, exist_ok=True)
    local = []
    for i, p in enumerate(paths, 1):
        dst = vis / f"candidate{i}.jpg"
        dst.write_bytes(Path(p).read_bytes())
        local.append(dst)
    listing = "\n".join(f"{i}) {p}" for i, p in enumerate(local, 1))
    prompt = (f"Read each of these images:\n{listing}\nWhich one best shows: \"{query}\"? It must be correct "
              f"(right part/model), complete, legible, and not watermarked. Reply with only the number, or 0 if none.")
    try:
        out = claude_code.run(prompt, "checking pictures", cwd=WORKSPACE, timeout=120, args=["--allowedTools", "Read"])
        m = re.search(r"\b([0-9])\b", out)
        return int(m.group(1)) if m else None
    except Exception:
        return None


# Kept for callers that only want a picture.
def find_image(query: str, on_better=None) -> tuple[Path, str]:
    return best_image(query, on_better=on_better)


def pull_up(query: str, on_better=None) -> dict:
    """Everything for an on-screen panel: the resolved entity, its best picture and accurate facts."""
    technical = bool(TECHNICAL.search(query.lower()))
    try:
        entity = None if technical else resolve(query)
    except Exception:
        entity = None
    try:
        path, caption = best_image(query, entity, on_better)
    except LookupError:
        if entity is None:
            raise LookupError(f"I couldn't find anything for '{query}'.")
        path, caption = None, ""
    if entity is None:  # pinouts, diagrams...: the picture is the answer
        return {"title": caption or query, "description": "", "facts": "", "spoken": f"Here's {caption or query}.",
                "image": str(path), "caption": caption, "url": ""}
    sentences = re.split(r"(?<=[.!?])\s+", entity.summary)
    return {
        "title": entity.title,
        "description": entity.description,
        "facts": " ".join(sentences[:4]),
        "spoken": sentences[0] if sentences else entity.description,
        "image": str(path) if path else None,
        "caption": caption,
        "url": entity.url,
    }
