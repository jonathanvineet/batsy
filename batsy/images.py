"""Find and fetch a picture to show, without involving Claude (so it costs no tokens).

Sources: Wikipedia (best for people, places, things), then Openverse (openly licensed photos).
"""
import hashlib
import io
import json
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

from batsy.config import DATA_DIR

CACHE = DATA_DIR / "images"
UA = {"User-Agent": "Batsy/0.1 (personal voice assistant; https://github.com/)"}
MAX_BYTES = 15_000_000


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _wikipedia(query: str) -> list[tuple[str, str]]:
    q = urllib.parse.urlencode({
        "action": "query", "format": "json", "generator": "search", "gsrsearch": query, "gsrlimit": 5,
        "prop": "pageimages", "piprop": "thumbnail", "pithumbsize": 1600, "pilicense": "any",
    })
    pages = _get_json(f"https://en.wikipedia.org/w/api.php?{q}").get("query", {}).get("pages", {})
    ranked = sorted(pages.values(), key=lambda pg: pg.get("index", 99))
    return [(pg["thumbnail"]["source"], pg["title"]) for pg in ranked if pg.get("thumbnail")]


def _openverse(query: str) -> list[tuple[str, str]]:
    q = urllib.parse.urlencode({"q": query, "page_size": 6, "mature": "false"})
    res = _get_json(f"https://api.openverse.org/v1/images/?{q}").get("results", [])
    return [(r.get("url") or r.get("thumbnail"), r.get("title") or query) for r in res if r.get("url")]


def download(url: str) -> Path:
    """Fetch an image URL into the cache as JPEG (validated by Pillow)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    out = CACHE / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".jpg")
    if out.exists():
        return out
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("image too large")
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((2400, 2400))
    img.save(out, "JPEG", quality=90)
    return out


def find_image(query: str, source: str = "auto") -> tuple[Path, str]:
    """Return (local path, caption) for the best picture matching query."""
    sources = {"wikipedia": [_wikipedia], "openverse": [_openverse]}.get(source, [_wikipedia, _openverse])
    errors = []
    for fn in sources:
        try:
            for url, title in fn(query)[:3]:
                try:
                    return download(url), title
                except Exception as e:  # broken link or unsupported format: try the next one
                    errors.append(str(e))
        except Exception as e:
            errors.append(f"{fn.__name__}: {e}")
    raise LookupError(f"No picture found for '{query}'. {'; '.join(errors)[:200]}")
