"""Batsy reads its brain: every note is indexed (SQLite FTS5, in memory.db) so Batsy can search it, quote it and
use it as context; and [[About Jonathan]] is two-way with Batsy's memory.

  vault_notes   path, title, folder, mtime, text      (the index; refreshed by a 20 s watcher)
  vault_fts     FTS5 over title + text

About Jonathan: the facts block lists every remembered fact as `- text ^f-<id>`. Batsy's memory is the source of
truth, but when Jonathan edits that block in Obsidian (on any device): a changed line updates the fact, a removed
line forgets it, a new line (no ^id) becomes a new fact. Then the block is rewritten from memory.
"""
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path

from batsy import vault
from batsy.memory_store import _clip, _terms, get_store

log = logging.getLogger("batsy.vault")
FACT_LINE = re.compile(r"^\s*-\s+(?P<text>.*?)\s*(?:\^f-(?P<id>[\w]+))?\s*$")
SKIP_CONTEXT = ("06 Memory/", "Batsy/", "Home.md")  # facts are already in the context; these are scaffolding
_fact_mtime = 0.0
_written: set[str] = set()  # fact ids in the block as Batsy last wrote it (only those can be "deleted" by an edit)


def _db():
    st = get_store()
    with st.lock, st.db:
        st.db.executescript("""
            CREATE TABLE IF NOT EXISTS vault_notes (path TEXT PRIMARY KEY, title TEXT, folder TEXT, mtime REAL, text TEXT);
        """)
        try:
            st.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS vault_fts USING fts5(path UNINDEXED, title, text)")
        except sqlite3.OperationalError:
            pass
    return st


def _plain(md: str) -> str:
    """Note text for searching: no frontmatter, no Batsy markers, links as their words."""
    md = re.sub(r"^---\n.*?\n---\n", "", md, flags=re.S)
    md = re.sub(r"%% /?batsy(?::[\w-]+)? %%", "", md)
    md = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", md)
    md = re.sub(r"\[\[([^\]]+)\]\]", r"\1", md)
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)|\^f-\w+", "", md)
    md = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", md)  # [label](url) -> label
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def reindex(full: bool = False) -> int:
    """Index new and changed notes; drop deleted ones. Returns how many changed."""
    st = _db()
    root = vault.ROOT
    with st.lock:
        known = {r["path"]: r["mtime"] for r in st.db.execute("SELECT path, mtime FROM vault_notes")}
    seen, changed = set(), 0
    for p in root.rglob("*.md"):
        rel = p.relative_to(root).as_posix()
        if rel.startswith((".", "Batsy/Templates/")) or "/." in rel:
            continue
        seen.add(rel)
        m = p.stat().st_mtime
        if not full and known.get(rel) == m:
            continue
        text = _plain(vault.read(p))
        with st.lock, st.db:
            st.db.execute("INSERT OR REPLACE INTO vault_notes VALUES (?,?,?,?,?)", (rel, p.stem, rel.split("/")[0], m, text))
            try:
                st.db.execute("DELETE FROM vault_fts WHERE path = ?", (rel,))
                st.db.execute("INSERT INTO vault_fts (path, title, text) VALUES (?,?,?)", (rel, p.stem, text))
            except sqlite3.OperationalError:
                pass
        changed += 1
    gone = set(known) - seen
    if gone:
        with st.lock, st.db:
            for rel in gone:
                st.db.execute("DELETE FROM vault_notes WHERE path = ?", (rel,))
                try:
                    st.db.execute("DELETE FROM vault_fts WHERE path = ?", (rel,))
                except sqlite3.OperationalError:
                    pass
    return changed + len(gone)


def search(query: str, limit: int = 5, folder: str = "", skip: tuple = ()) -> list[dict]:
    """Best-matching notes: [{path, title, snippet}] (FTS5 bm25; titles count three times as much)."""
    terms = _terms(query)
    if not terms:
        return []
    st = _db()
    q = " OR ".join(f'"{t}"' for t in terms)
    with st.lock:
        try:
            rows = st.db.execute(
                "SELECT path, title, snippet(vault_fts, 2, '', '', ' … ', 24) AS snip, bm25(vault_fts, 0, 3, 1) AS score "
                "FROM vault_fts WHERE vault_fts MATCH ? ORDER BY score LIMIT ?", (q, limit * 4)).fetchall()
        except sqlite3.OperationalError:
            like = " OR ".join("text LIKE ?" for _ in terms)
            rows = st.db.execute(f"SELECT path, title, substr(text, 1, 200) AS snip, 0 AS score FROM vault_notes "
                                 f"WHERE {like} LIMIT ?", (*[f"%{t}%" for t in terms], limit * 4)).fetchall()
    out = []
    for r in rows:
        if folder and not r["path"].startswith(folder) or r["path"].startswith(skip):
            continue
        out.append({"path": r["path"], "title": r["title"], "snippet": _clip(r["snip"], 300), "score": r["score"]})
    return out[:limit]


def note_text(name_or_path: str) -> tuple[Path, str] | None:
    """A note by title (or path): its file and text."""
    st = _db()
    with st.lock:
        r = st.db.execute("SELECT path FROM vault_notes WHERE path = ? OR lower(title) = lower(?) "
                          "ORDER BY length(path) LIMIT 1", (name_or_path, name_or_path)).fetchone()
    if not r:
        hits = search(name_or_path, 1)
        if not hits:
            return None
        r = {"path": hits[0]["path"]}
    p = vault.ROOT / r["path"]
    return (p, vault.read(p)) if p.exists() else None


def context_block(text: str) -> str:
    """For build_context: one or two short snippets from Jonathan's notes, when they really match."""
    if len(_terms(text)) < 2:
        return ""
    hits = [h for h in search(text, 2, skip=SKIP_CONTEXT) if h["score"] < -2.0]
    return "[From your notes]\n" + "\n".join(f"{h['title']}: {h['snippet']}" for h in hits) if hits else ""


# --- About Jonathan <-> memory facts ---------------------------------------------------------------------------------
def _facts_note() -> Path:
    return vault.ROOT / "06 Memory" / "About Jonathan.md"


def write_facts() -> None:
    """Rewrite the facts block from Batsy's memory (newest last)."""
    global _fact_mtime, _written
    p = _facts_note()
    if not p.exists():
        return
    facts = get_store().all_facts()
    _written = {f["id"] for f in facts}
    lines = [f"- {f['fact']} ^f-{f['id']}" for f in facts]
    vault.set_block(p, "facts", "\n".join(lines))
    _fact_mtime = p.stat().st_mtime


def read_fact_edits() -> int:
    """Jonathan edited About Jonathan: bring the changes into memory. Returns how many changed."""
    global _fact_mtime
    p = _facts_note()
    if not p.exists() or vault.is_ours(p) or p.stat().st_mtime == _fact_mtime:
        return 0
    body = vault.get_block(p, "facts")
    if body is None:
        return 0
    st = get_store()
    have = {f["id"]: f["fact"] for f in st.all_facts()}
    seen, n = set(), 0
    for line in body.splitlines():
        m = FACT_LINE.match(line)
        if not m or not m.group("text"):
            continue
        text, fid = m.group("text").strip(), m.group("id")
        if fid and fid in have:
            seen.add(fid)
            if text != have[fid]:
                st.forget_fact(fid)
                st.add_fact(text, fid)
                n += 1
        else:
            seen.add(st.add_fact(text))
            n += 1
    for fid in (set(have) & _written) - seen:  # deleted by Jonathan (not just added to memory since)
        st.forget_fact(fid)
        n += 1
    if n:
        log.info("brain: %d memory change(s) from About Jonathan", n)
    write_facts()
    return n


# --- the watcher -----------------------------------------------------------------------------------------------------
def start(app) -> None:
    def loop():
        time.sleep(5)
        vault.submit(write_facts)
        while True:
            try:
                vault.submit(read_fact_edits)
                reindex()
            except Exception:
                log.exception("vault index")
            time.sleep(60)  # (a search reindexes first anyway, so answers are never stale)

    threading.Thread(target=loop, daemon=True, name="vault-index").start()
