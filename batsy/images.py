"""Find the *right* picture and facts for something, without involving an AI (no tokens).

Not "top search result": the query is first resolved to the exact Wikipedia entity, then candidates
from that article and from Wikimedia Commons are scored on how well they match, curated quality
(featured / quality / valued images and the article's own lead image), resolution and shape.
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

from batsy.config import DATA_DIR

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
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((2400, 2400))
    img.save(out, "JPEG", quality=90)
    return out


def best_image(query: str, entity: Entity | None = None) -> tuple[Path, str]:
    """The best picture for the query: (local path, caption)."""
    entity = entity or resolve(query)
    extra = _words(query) - (_words(entity.title) if entity else set())
    candidates = []
    # The article's own lead image is hand-picked by editors: best when the query is just the thing itself.
    if entity and entity.lead_image:
        candidates.append({"url": entity.lead_image, "alt": _thumb(entity.lead_image), "w": 1600, "h": 1100, "title": entity.title, "desc": entity.title,
                           "rank": 0, "quality": 2.2 if not extra else 0.5, "caption": entity.title})
    # Commons covers specifics ("at night", "interior", "1969"): search with the resolved name plus the extras.
    search = f"{entity.title} {' '.join(sorted(extra))}".strip() if entity else query
    try:
        candidates += _commons(search)
    except Exception:
        pass
    if not candidates:
        raise LookupError(f"No good picture found for '{query}'.")
    ranked = sorted(candidates, key=lambda c: _score(c, query, entity), reverse=True)
    for c in ranked[:4]:
        caption = c.get("caption") or (entity.title if entity else c["title"])
        if extra and entity:
            caption = f"{entity.title} ({' '.join(sorted(extra))})"
        for url in filter(None, (c.get("alt"), c["url"])):  # a sensible-size thumbnail first, then the original
            try:
                return download(url), caption
            except Exception:
                continue
    raise LookupError(f"Couldn't download a picture for '{query}'.")


# Kept for callers that only want a picture.
def find_image(query: str, source: str = "auto") -> tuple[Path, str]:
    return best_image(query)


def pull_up(query: str) -> dict:
    """Everything for an on-screen panel: the resolved entity, its best picture and accurate facts."""
    entity = resolve(query)
    if entity is None:
        raise LookupError(f"I couldn't work out what '{query}' refers to.")
    try:
        path, caption = best_image(query, entity)
    except LookupError:
        path, caption = None, ""
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
