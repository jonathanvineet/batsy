"""Batsy's memory of every file it has received: what, from whom, when, how, and where every copy is.

Folders only say what's there now; this remembers. So "who sent the last file?", "what did Karan send yesterday?",
"open the audit report", "open the file Rehaan sent" and "open Karan's last PDF" are answered instantly, even after
a file was renamed or moved, and the AI can search it too (tools/files.py: received_files).

Stored in received.db: one row per file, with its laptop path and its copy on the PC (batsy/pc.py Forwarder).
On first start it fills itself from what is already in the inbox (loose files there came by Taildrop, so yours).
"""
import re
import sqlite3
import threading
import time
from pathlib import Path

from batsy.config import DATA_DIR

DB = DATA_DIR / "received.db"
_lock = threading.Lock()

KINDS = {"pdf": "PDF", "doc": "document", "docx": "document", "odt": "document", "txt": "text file", "md": "note",
         "ppt": "presentation", "pptx": "presentation", "key": "presentation", "xls": "spreadsheet",
         "xlsx": "spreadsheet", "csv": "spreadsheet", "jpg": "photo", "jpeg": "photo", "png": "picture",
         "heic": "photo", "gif": "picture", "webp": "picture", "mp4": "video", "mov": "video", "mkv": "video",
         "mp3": "song", "m4a": "audio", "wav": "audio", "zip": "zip", "stl": "3D model", "step": "3D model",
         "obj": "3D model", "py": "code", "ino": "Arduino sketch"}
KIND_WORDS = {"pdf": "PDF", "pdfs": "PDF", "photo": "photo", "photos": "photo", "picture": "picture",
              "pictures": "picture", "image": "picture", "images": "picture", "video": "video", "videos": "video",
              "document": "document", "documents": "document", "doc": "document", "presentation": "presentation",
              "slides": "presentation", "spreadsheet": "spreadsheet", "sheet": "spreadsheet", "model": "3D model",
              "3d model": "3D model", "zip": "zip", "song": "song", "audio": "audio"}


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY, name TEXT, path TEXT UNIQUE, sender TEXT, via TEXT, size INTEGER,
        kind TEXT, received REAL, pc_path TEXT)""")
    return c


def kind_of(name: str) -> str:
    return KINDS.get(Path(name).suffix.lower().lstrip("."), "file")


def title(name: str) -> str:
    """'TC_22ME082_ZUBAIR (3).pdf' -> 'tc 22me082 zubair': what you'd call it out loud (and match against)."""
    stem = re.sub(r"\s*\(\d+\)$", "", Path(name).stem)
    stem = re.sub(r"(?<=[a-z])(?=\d{4})|(?<=\d)(?=[a-z])", " ", stem, flags=re.I)  # "Report2025" -> "Report 2025"
    return " ".join(re.sub(r"[_\-.()]+", " ", stem).lower().split())


def spoken(name: str, words: int = 7) -> str:
    """A name short enough to say: 'Human Values and Ethics Assignment Answers…' -> the first few words."""
    t = title(name).split()
    return " ".join(t[:words]) + ("..." if len(t) > words else "")


def record(path: Path, sender: str | None, via: str = "") -> None:
    p = Path(path)
    try:
        size, when = p.stat().st_size, p.stat().st_mtime
    except OSError:
        size, when = 0, time.time()
    with _lock, _db() as c:
        c.execute("INSERT INTO files (name, path, sender, via, size, kind, received) VALUES (?,?,?,?,?,?,?) "
                  "ON CONFLICT(path) DO UPDATE SET sender=excluded.sender, via=excluded.via",
                  (p.name, str(p), sender, via, size, kind_of(p.name), max(when, 0) if via == "earlier" else time.time()))


def on_pc(path: Path, pc_path: str) -> None:
    with _lock, _db() as c:
        c.execute("UPDATE files SET pc_path=? WHERE path=?", (pc_path, str(path)))


def backfill(inbox: Path, mine: str, skip: set[str]) -> int:
    """Files already in the inbox that aren't recorded yet (e.g. from before this existed)."""
    with _db() as c:
        known = {r[0] for r in c.execute("SELECT path FROM files")}
    n = 0
    for p in inbox.rglob("*"):
        rel_parts = p.relative_to(inbox).parts
        if not p.is_file() or str(p) in known or any(part.startswith(".") or part == "sliced" for part in rel_parts):
            continue  # hidden, or Batsy's own slicer output: not something anyone sent
        rel = p.relative_to(inbox)
        sender = rel.parts[0] if len(rel.parts) > 1 else mine  # loose in the inbox: Taildrop, so yours
        if sender in skip:
            continue
        record(p, sender, "earlier")
        n += 1
    return n


def day_range(word: str) -> tuple[float, float]:
    """'today' / 'yesterday' / 'this week' / 'this month' -> (from, until) by the calendar."""
    lt = time.localtime()
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    return {"today": (midnight, time.time() + 60), "yesterday": (midnight - 86400, midnight),
            "this week": (midnight - 86400 * lt.tm_wday, time.time() + 60),
            "this month": (midnight - 86400 * (lt.tm_mday - 1), time.time() + 60)}.get(word, (0, time.time() + 60))


def all_rows(sender: str | None = None, since: float = 0, kind: str | None = None, limit: int = 500,
             until: float | None = None) -> list[dict]:
    q, args = "SELECT * FROM files WHERE received >= ? AND received < ?", [since, until or time.time() + 60]
    if sender:
        q += " AND lower(sender) = lower(?)"
        args.append(sender)
    if kind:
        q += " AND kind = ?"
        args.append(kind)
    with _db() as c:
        rows = [dict(r) for r in c.execute(q + " ORDER BY received DESC, id DESC LIMIT ?", (*args, limit))]
    for r in rows:  # moved or renamed since? find it again by name
        if not Path(r["path"]).exists():
            r["missing"] = True
    return rows


def last(sender: str | None = None, kind: str | None = None) -> dict | None:
    rows = all_rows(sender, kind=kind, limit=1)
    return rows[0] if rows else None


def search(words: str, sender: str | None = None, kind: str | None = None) -> list[tuple[float, dict]]:
    """Best matches for what you called it ("the audit report", "zubair's tc"), best first."""
    from rapidfuzz import fuzz

    words = " ".join(re.sub(r"[_\-.]+", " ", words.lower()).split())
    if not words:
        return []
    out = []
    for r in all_rows(sender, kind=kind):
        t = title(r["name"])
        score = max(fuzz.token_set_ratio(words, t), fuzz.partial_ratio(words, t) if len(words) >= 5 else 0)
        out.append((score, r))
    out.sort(key=lambda x: (-x[0], -x[1]["received"]))
    return out


def ago(t: float) -> str:
    s = time.time() - t
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} minutes ago"
    if s < 86400:
        h = int(s // 3600)
        return f"{h} hour{'s' if h > 1 else ''} ago"
    d = int(s // 86400)
    return "yesterday" if d == 1 else f"{d} days ago" if d < 14 else time.strftime("on %B %-d", time.localtime(t))
