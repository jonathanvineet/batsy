"""Batsy as a research assistant: several research topics at once, each kept apart, all written into the Obsidian
brain by Batsy (Jonathan never writes there himself).

    ~/Batsy/Brain/08 Research/<Name>/
        <Name>.md                 the hub: question, living summary, findings, open questions, ideas, next steps,
                                  sources, a dated log (Batsy-owned blocks)
        Notes/<date> <title>.md   what he said (word for word) + a clean version
        Sources/<title>.md        papers, articles, his files: what it is, key points, how it relates, a link
        Conversations/<date> <topic>.md   each talk, summed up

Behind it, tables in memory.db (research, research_items + full-text search, and embeddings from the HP's
nomic-embed-text when it's up) so notes and files can be filed into the right research and found again.

Files he sends are read (docs.extract_text: PDF, docx, text, OCR for photos and scans), understood by the local AI
(what is it, is it research material, what about), filed into the matching research or offered as a new one, and
shown back to him: spoken, a card on the laptop, and the phone's full-screen card.
"""
import datetime as dt
import json
import logging
import re
import threading
import time
from pathlib import Path

from batsy import vault

log = logging.getLogger("batsy.research")

ACTIVE_HOURS = 2.0  # "start / switch to a research": follow-ups go to it for this long
EMBED_MODEL = "nomic-embed-text"
_cfg = None
_lock = threading.Lock()
_active: dict = {"rid": None, "t": 0.0}


def configure(cfg) -> None:
    global _cfg, ACTIVE_HOURS, EMBED_MODEL
    _cfg = cfg
    ACTIVE_HOURS = float(cfg.get("research.active_hours") or ACTIVE_HOURS)
    EMBED_MODEL = cfg.get("research.embed_model") or EMBED_MODEL
    _setup()


# --- storage ----------------------------------------------------------------------------------------------------------
def _store():
    from batsy.memory_store import get_store

    return get_store()


def _setup() -> None:
    st = _store()
    with st.lock, st.db:
        st.db.executescript("""
            CREATE TABLE IF NOT EXISTS research (id INTEGER PRIMARY KEY, name TEXT UNIQUE, question TEXT,
                status TEXT DEFAULT 'active', created REAL, last REAL, emb BLOB);
            CREATE TABLE IF NOT EXISTS research_items (id INTEGER PRIMARY KEY, rid INTEGER, kind TEXT, title TEXT,
                text TEXT, path TEXT, t REAL, emb BLOB);
            CREATE INDEX IF NOT EXISTS research_items_rid ON research_items(rid);
            CREATE VIRTUAL TABLE IF NOT EXISTS research_fts USING fts5(title, text, content='research_items',
                content_rowid='id');
            CREATE TRIGGER IF NOT EXISTS research_ai AFTER INSERT ON research_items BEGIN
                INSERT INTO research_fts(rowid, title, text) VALUES (new.id, new.title, new.text);
            END;
        """)
        row = st.db.execute("SELECT value FROM state WHERE key='research_active'").fetchone()
    if row:
        try:
            _active.update(json.loads(row[0]))
        except ValueError:
            pass
    threading.Thread(target=_warm, daemon=True, name="research-warm").start()


def _warm() -> None:
    """Read the research prompts into the HP's cache once, at start, in the background: the first "start a research"
    after a restart took two minutes while the AI read them cold."""
    from batsy import llm_hosts, research_talk
    from batsy.local_brain import who

    time.sleep(60)  # after Batsy's own start-up work
    for system in (research_talk.INTENT, research_talk.PARTNER.format(**who(_cfg))):
        try:
            llm_hosts.background_chat(_cfg, {"model": _cfg.get("local.model"), "keep_alive": _cfg.get("local.keep_alive"),
                                             "messages": [{"role": "system", "content": system},
                                                          {"role": "user", "content": "hi"}],
                                             "options": {"num_predict": 1, "num_ctx": 4096}})
        except Exception as e:
            log.debug("research warm-up: %s", e)


def all_research() -> list[dict]:
    st = _store()
    with st.lock:
        rows = st.db.execute("SELECT r.*, (SELECT COUNT(*) FROM research_items i WHERE i.rid = r.id AND kind='note') notes,"
                             " (SELECT COUNT(*) FROM research_items i WHERE i.rid = r.id AND kind='source') sources "
                             "FROM research r WHERE status != 'archived' ORDER BY last DESC").fetchall()
    return [dict(r) for r in rows]


def get(rid: int) -> dict | None:
    st = _store()
    with st.lock:
        r = st.db.execute("SELECT * FROM research WHERE id = ?", (rid,)).fetchone()
    return dict(r) if r else None


def find(name: str) -> dict | None:
    """'bat', 'the bat one', 'how bats navigate' -> that research (by words, then fuzzily)."""
    from rapidfuzz import fuzz, process

    rs = all_research()
    if not rs or not name:
        return None
    name = re.sub(r"\b(?:my|the|research|one|project|topic|on|about)\b", " ", name.lower()).strip()
    hit = process.extractOne(name, [f"{r['name']} {r['question'] or ''}".lower() for r in rs], scorer=fuzz.WRatio)
    return rs[hit[2]] if hit and hit[1] >= 70 else None


def create(name: str, question: str = "") -> dict:
    name = _title(name)
    st = _store()
    now = time.time()
    with st.lock, st.db:
        st.db.execute("INSERT OR IGNORE INTO research (name, question, created, last) VALUES (?,?,?,?)",
                      (name, question or name, now, now))
        r = dict(st.db.execute("SELECT * FROM research WHERE name = ?", (name,)).fetchone())
    vault.submit(_write_hub, r)
    threading.Thread(target=_embed_research, args=(r["id"],), daemon=True).start()
    set_active(r["id"])
    return r


def set_active(rid: int | None) -> None:
    _active.update(rid=rid, t=time.time())
    st = _store()
    with st.lock, st.db:
        st.db.execute("INSERT OR REPLACE INTO state VALUES ('research_active', ?)", (json.dumps(_active),))


def active() -> dict | None:
    """The research you're working on (for ACTIVE_HOURS after you last touched it), or None."""
    if not _active.get("rid") or time.time() - _active.get("t", 0) > ACTIVE_HOURS * 3600:
        return None
    return get(_active["rid"])


def touch(rid: int) -> None:
    _active.update(rid=rid, t=time.time())
    st = _store()
    with st.lock, st.db:
        st.db.execute("UPDATE research SET last = ? WHERE id = ?", (time.time(), rid))


def _add_item(rid: int, kind: str, title: str, text: str, path: str = "") -> int:
    st = _store()
    with st.lock, st.db:
        iid = st.db.execute("INSERT INTO research_items (rid, kind, title, text, path, t) VALUES (?,?,?,?,?,?)",
                            (rid, kind, title, text, path, time.time())).lastrowid
        st.db.execute("UPDATE research SET last = ? WHERE id = ?", (time.time(), rid))
    threading.Thread(target=_embed_item, args=(iid, f"{title}. {text}"), daemon=True).start()
    return iid


def items(rid: int, kind: str | None = None, limit: int = 50) -> list[dict]:
    st = _store()
    with st.lock:
        rows = st.db.execute("SELECT id, kind, title, text, path, t FROM research_items WHERE rid = ?"
                             + (" AND kind = ?" if kind else "") + " ORDER BY t DESC LIMIT ?",
                             (rid, kind, limit) if kind else (rid, limit)).fetchall()
    return [dict(r) for r in rows]


# --- the brain (Obsidian) --------------------------------------------------------------------------------------------
def folder(r: dict) -> Path:
    return vault.path("research") / vault.safe(r["name"])


def hub(r: dict) -> Path:
    return folder(r) / f"{vault.safe(r['name'])}.md"


HUB_BLOCKS = [("summary", "Summary"), ("findings", "Findings"), ("open-questions", "Open questions"),
              ("ideas", "Ideas"), ("next-steps", "Next steps"), ("sources", "Sources"), ("notes", "Notes"),
              ("conversations", "Conversations"), ("log", "Log")]


def _write_hub(r: dict) -> None:
    p = hub(r)
    if p.exists():
        return
    body = [vault.frontmatter(type="research", status=r.get("status") or "active",
                              created=dt.date.fromtimestamp(r["created"]).isoformat(), question=r.get("question"),
                              tags=["research"]),
            f"# {r['name']}\n", f"> **Question:** {r.get('question') or r['name']}\n"]
    first = {"summary": "_Nothing yet: talk to me about it, or send me papers and notes._"}
    for name, heading in HUB_BLOCKS:
        body.append(f"## {heading}\n{vault.block(name, first.get(name, ''))}\n")
    vault.write(p, "\n".join(body))


def _log(r: dict, line: str) -> None:
    vault.submit(lambda: (_write_hub(r), vault.add_to_block(hub(r), "log", f"- {dt.datetime.now():%Y-%m-%d %H:%M} {line}",
                                                            "Log", newest_first=True)))


def add_note(rid: int, raw: str, source: str = "voice") -> str:
    """A note: kept word for word, plus a clean version written by the local AI in the background."""
    r = get(rid)
    title = _short(raw)
    iid = _add_item(rid, "note", title, raw)
    touch(rid)
    name = f"{dt.datetime.now():%Y-%m-%d %H%M} {title}"
    p = folder(r) / "Notes" / f"{vault.safe(name)}.md"

    def write():
        _write_hub(r)
        vault.create(p, vault.frontmatter(type="research-note", research=f"[[{vault.safe(r['name'])}]]",
                                          created=dt.datetime.now().isoformat(timespec="minutes"), source=source)
                     + f"# {title}\n\n## In your words\n> {raw}\n\n## Clean\n{vault.block('clean', '_writing…_')}\n")
        vault.add_to_block(hub(r), "notes", f"- {vault.link(name, title)}", "Notes", newest_first=True)
    vault.submit(write)
    _log(r, f"note: {title}")

    def tidy():
        try:
            clean = _ask(f"Rewrite this research note as 1 to 3 short, clear bullet points ('- '), keeping every fact, "
                         f"name and number. Research: {r['name']}. Note (speech-to-text): \"{raw}\"", 160)
        except Exception as e:
            log.info("note tidy: %s", e)
            return
        clean = "\n".join(x for x in clean.splitlines() if x.strip().startswith(("-", "•", "*"))) or clean.strip()
        vault.submit(vault.set_block, p, "clean", clean.strip())  # just the bullets, not "Here are three…"
        st = _store()
        with st.lock, st.db:
            st.db.execute("UPDATE research_items SET text = ? WHERE id = ?", (f"{raw}\n{clean}", iid))
    threading.Thread(target=tidy, daemon=True, name="research-note").start()
    return title


def add_source(rid: int, meta: dict, text: str = "") -> str:
    """A paper, article or file of yours: citation, what it is, key points, how it relates, and a link."""
    r = get(rid)
    title = meta.get("title") or meta.get("what") or "Source"
    _add_item(rid, "source", title, "\n".join(x for x in (meta.get("what", ""), meta.get("abstract", ""),
                                                          "\n".join(meta.get("points") or []), text[:4000]) if x),
              meta.get("file") or meta.get("url") or "")
    touch(rid)
    p = folder(r) / "Sources" / f"{vault.safe(title)}.md"
    same = next((x for x in (folder(r) / "Sources").glob("*.md") if x.stem.lower() == vault.safe(title).lower()), None) \
        if (folder(r) / "Sources").exists() else None
    if same is not None:  # already saved (e.g. "keep the second one", then its PDF was read): add to that note
        points = "\n".join(f"- {x}" for x in (meta.get("points") or []))
        vault.submit(lambda: (points and vault.set_block(same, "points", points, "Key points (read by Batsy)"),
                              meta.get("relates") and vault.set_block(same, "relates", meta["relates"]),
                              meta.get("file") and vault.set_props(same, file=meta["file"])))
        return same.stem
    cite = ", ".join(x for x in (meta.get("authors", ""), str(meta.get("year") or ""), meta.get("venue", "")) if x)
    link = (f"[Open the file](<file://{meta['file']}>)" if meta.get("file") else "") + \
           (f"[{meta['url']}]({meta['url']})" if meta.get("url") else "")
    points = "\n".join(f"- {x}" for x in (meta.get("points") or []))

    def write():
        _write_hub(r)
        vault.create(p, vault.frontmatter(type="source", research=f"[[{vault.safe(r['name'])}]]", kind=meta.get("kind"),
                                          authors=meta.get("authors"), year=meta.get("year"), url=meta.get("url"),
                                          added=dt.date.today().isoformat())
                     + f"# {title}\n\n{('*' + cite + '*' + chr(10) + chr(10)) if cite else ''}{link}\n\n"
                     f"## What it is\n{meta.get('what') or ''}\n\n## Key points\n{points or '_—_'}\n\n"
                     + (f"## Abstract\n{meta['abstract']}\n\n" if meta.get("abstract") else "")
                     + f"## How it relates\n{vault.block('relates', meta.get('relates') or '')}\n")
        vault.add_to_block(hub(r), "sources", f"- {vault.link(title)}" + (f" ({cite})" if cite else ""), "Sources")
    vault.submit(write)
    _log(r, f"source: {title}")
    return title


def add_finding(rid: int, kind: str, text: str) -> None:
    """From a conversation or Claude's deep dive: a finding, open question, idea or next step on the hub."""
    r = get(rid)
    block = {"finding": "findings", "question": "open-questions", "idea": "ideas", "next": "next-steps"}[kind]
    heading = dict(HUB_BLOCKS)[block]
    _add_item(rid, kind, _short(text), text)
    vault.submit(lambda: (_write_hub(r), vault.add_to_block(hub(r), block, f"- {text.strip()}", heading)))


# --- finding things again, and filing ---------------------------------------------------------------------------------
def _embed(texts: list[str]) -> list[list[float]] | None:
    """Vectors from the HP's nomic-embed-text (None when it's off: full-text search does the work then)."""
    import urllib.request

    from batsy import llm_hosts

    if _cfg is None:
        return None
    try:
        base = llm_hosts.best(_cfg)
        body = json.dumps({"model": EMBED_MODEL, "input": [t[:6000] for t in texts], "keep_alive": -1}).encode()
        with urllib.request.urlopen(urllib.request.Request(base + "/api/embed", data=body,
                                    headers={"Content-Type": "application/json"}), timeout=60) as r:
            return json.loads(r.read()).get("embeddings")
    except Exception as e:
        log.debug("embed: %s", e)
        return None


def _pack(v: list[float]) -> bytes:
    import numpy as np

    a = np.asarray(v, dtype="float32")
    return (a / (np.linalg.norm(a) or 1)).tobytes()


def _embed_item(iid: int, text: str) -> None:
    e = _embed([text])
    if e:
        st = _store()
        with st.lock, st.db:
            st.db.execute("UPDATE research_items SET emb = ? WHERE id = ?", (_pack(e[0]), iid))


def _embed_research(rid: int) -> None:
    r = get(rid)
    if not r:
        return
    summary = vault.get_block(hub(r), "summary") or "" if hub(r).exists() else ""
    e = _embed([f"{r['name']}. {r.get('question') or ''}. {summary}"])
    if e:
        st = _store()
        with st.lock, st.db:
            st.db.execute("UPDATE research SET emb = ? WHERE id = ?", (_pack(e[0]), rid))


def _terms(text: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9]{3,}", text.lower()) if w not in _STOP][:24]
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))


_STOP = set("the and for that this with from have what about into your you are was were has had not but can will "
            "its our out how why who which when where also than then them they their there here just like".split())


def classify(text: str) -> tuple[dict | None, float, list[dict]]:
    """Which research is this about? (best, confidence 0..1, the close runners-up). Embeddings (the research's name,
    question and summary, and everything filed in it) when the HP is up; otherwise full-text matches."""
    import numpy as np

    rs = all_research()
    if not rs:
        return None, 0.0, []
    scores: dict[int, float] = {}
    e = _embed([text[:4000]])
    st = _store()
    if e:
        q = np.frombuffer(_pack(e[0]), dtype="float32")
        with st.lock:
            for r in rs:
                vecs = [np.frombuffer(r["emb"], dtype="float32")] if r.get("emb") else []
                vecs += [np.frombuffer(x[0], dtype="float32") for x in st.db.execute(
                    "SELECT emb FROM research_items WHERE rid = ? AND emb IS NOT NULL ORDER BY t DESC LIMIT 40", (r["id"],))]
                if vecs:
                    sims = sorted((float(q @ v) for v in vecs), reverse=True)
                    scores[r["id"]] = sum(sims[:3]) / len(sims[:3])
    if not scores:  # no embeddings: how many of its words show up in each research
        terms = _terms(text)
        if not terms:
            return None, 0.0, []
        with st.lock:
            hits = st.db.execute("SELECT i.rid, COUNT(*) n FROM research_fts f JOIN research_items i ON i.id = f.rowid "
                                 "WHERE research_fts MATCH ? GROUP BY i.rid", (terms,)).fetchall()
        for r in rs:
            name_hits = sum(1 for w in re.findall(r"[a-z0-9]{3,}", f"{r['name']} {r['question'] or ''}".lower())
                            if w in text.lower())
            scores[r["id"]] = min(1.0, 0.25 * name_hits + 0.1 * next((h["n"] for h in hits if h["rid"] == r["id"]), 0))
        top = sorted(scores.items(), key=lambda x: -x[1])
        best = next(r for r in rs if r["id"] == top[0][0])
        return (best if top[0][1] > 0 else None), top[0][1], [r for r in rs if r["id"] != best["id"]][:2]
    top = sorted(scores.items(), key=lambda x: -x[1])
    best = next(r for r in rs if r["id"] == top[0][0])
    # cosine for nomic-embed-text: ~0.75+ same topic, ~0.6 related field, below ~0.55 unrelated
    conf = max(0.0, min(1.0, (top[0][1] - 0.5) / 0.3))
    if len(top) > 1 and top[0][1] - top[1][1] < 0.03:
        conf = min(conf, 0.5)  # two researches about equally close: ask
    runners = [next(r for r in rs if r["id"] == rid) for rid, s in top[1:3] if s > 0.55]
    return best, conf, runners


def retrieve(rid: int, query: str, k: int = 6) -> list[dict]:
    """The notes and sources of this research that matter for the question (embeddings + full text)."""
    import numpy as np

    st = _store()
    found: dict[int, tuple[float, dict]] = {}
    e = _embed([query])
    with st.lock:
        if e:
            q = np.frombuffer(_pack(e[0]), dtype="float32")
            for row in st.db.execute("SELECT id, kind, title, text, path, t, emb FROM research_items WHERE rid = ? "
                                     "AND emb IS NOT NULL", (rid,)):
                s = float(q @ np.frombuffer(row["emb"], dtype="float32"))
                found[row["id"]] = (s, {k_: row[k_] for k_ in ("id", "kind", "title", "text", "path", "t")})
        terms = _terms(query)
        if terms:
            for row in st.db.execute("SELECT i.id, i.kind, i.title, i.text, i.path, i.t, bm25(research_fts) b FROM "
                                     "research_fts f JOIN research_items i ON i.id = f.rowid WHERE research_fts MATCH ? "
                                     "AND i.rid = ? ORDER BY b LIMIT ?", (terms, rid, k)):
                s = found.get(row["id"], (0.0, None))[0] + 0.2
                found[row["id"]] = (s, {k_: row[k_] for k_ in ("id", "kind", "title", "text", "path", "t")})
    return [d for _, d in sorted(found.values(), key=lambda x: -x[0])[:k]]


# --- files you send: understand them, file them, show them back -------------------------------------------------------
NOT_DOCS = {".stl", ".3mf", ".obj", ".step", ".stp", ".gcode", ".zip", ".creality_printer", ".orca_printer", ".mp3",
            ".m4a", ".wav", ".mp4", ".mov"}


def understand(path: Path) -> dict:
    """{kind: research|other, what, topic, title, authors, year, points} for one file, from its first pages."""
    from batsy import docs

    ext = path.suffix.lower()
    if ext in NOT_DOCS or ext not in docs.SUPPORTED:
        what = {".stl": "a 3D model", ".3mf": "a 3D model", ".obj": "a 3D model", ".gcode": "a print file",
                ".zip": "a zip file"}.get(ext, f"a {ext.lstrip('.') or 'file'}")
        return {"kind": "other", "what": what, "topic": "", "file": str(path)}
    try:
        pages = docs.extract_text(path, max_chars=5000)
    except Exception as e:
        log.info("reading %s: %s", path.name, e)
        pages = []
    text = "\n".join(t for _, t in pages).strip()
    if len(text) < 40:  # a photo with little or no text (a person, a place): not research material
        return {"kind": "other", "what": "a photo" if ext in docs.IMAGES else "a file with no readable text",
                "topic": "", "file": str(path)}
    out = {"kind": "other", "what": "", "topic": "", "file": str(path), "text": text}
    try:
        raw = _ask(
            "You are filing a file Jonathan (an engineering student) sent to his assistant. From its first part, "
            "answer in JSON only: {\"research\": true if it is study or research material (a paper, article, report, "
            "lecture notes, handwritten notes, slides, a book chapter, a dataset, a technical document) else false "
            "(a receipt, bill, ticket, ID, form, chat screenshot, ordinary photo), \"what\": \"a short description, "
            "e.g. 'a 2021 paper on bat echolocation' or 'your lecture notes on PID control' or 'a train ticket'\", "
            "\"topic\": \"its subject in 2-5 words\", \"title\": \"its title if it has one\", \"authors\": \"\", "
            "\"year\": \"\", \"points\": [\"up to 3 key points, one line each\"]}\n\n"
            f"File name: {path.name}\nStart of the file:\n{text[:2500]}", 260, json_mode=True)
        j = json.loads(raw)
        out.update(kind="research" if j.get("research") else "other", what=str(j.get("what") or "")[:120],
                   topic=str(j.get("topic") or "")[:60], title=str(j.get("title") or "")[:160],
                   authors=str(j.get("authors") or "")[:160], year=str(j.get("year") or "")[:4],
                   points=[str(x)[:200] for x in (j.get("points") or [])][:3])
    except Exception as e:
        log.info("understanding %s: %s", path.name, e)
        out["what"] = f"a {path.suffix.lstrip('.')} file"
    return out


def understand_batch(app, files: list[Path]) -> None:
    """Read every file that arrived, file the research ones, and show what was understood (spoken, a laptop card,
    the phone's full-screen card). Asks when it can't tell which research, or when it looks like a new topic."""
    from batsy import research_talk

    lines, cards, ask = [], [], None
    for p in files:
        u = understand(p)
        name = p.name
        if u["kind"] != "research":
            lines.append(f"{u['what'] or name}")
            cards.append(f"{name}: {u['what'] or 'not research'}")
            continue
        r, conf, others = classify(" ".join(x for x in (u.get("topic"), u.get("what"), u.get("title"),
                                                         (u.get("text") or "")[:1500]) if x))
        meta = {**u, "title": u.get("title") or p.stem.replace("_", " "), "kind": "file"}
        if r and conf >= 0.6:
            add_source(r["id"], meta, u.get("text", ""))
            lines.append(f"{u['what']}, filed under {r['name']}")
            cards.append(f"{name}: {u['what']} → {r['name']}")
        elif r and conf >= 0.3:
            lines.append(f"{u['what']}")
            cards.append(f"{name}: {u['what']} → {r['name']}?")
            ask = ask or ("which", meta, [r] + others)
        else:
            lines.append(f"{u['what']}, which looks new")
            cards.append(f"{name}: {u['what']} → new topic: {u.get('topic') or '?'}")
            ask = ask or ("new", meta, u.get("topic") or meta["title"])
    n = len(files)
    spoken = (f"You sent {n} files: " if n > 1 else "You sent ") + "; ".join(lines) + "."
    app.show_visual("card", "What you sent | " + "\n".join(cards[:8]))
    app.announce(spoken)
    if ask:
        kind, meta, x = ask
        if kind == "which":
            research_talk.pending.update(kind="which", meta=meta, options=[o["id"] for o in x], t=time.time())
            app.ask_user(f"Is {meta['what']} for {' or '.join(o['name'] for o in x)}?")
        else:
            research_talk.pending.update(kind="new", meta=meta, topic=x, t=time.time())
            app.ask_user(f"Want me to start a research on {x} with it?")


# --- documents Claude found: Batsy fetches and reads them itself -------------------------------------------------------
def _queue_file() -> Path:
    from batsy.config import DATA_DIR

    return DATA_DIR / "research_to_read.jsonl"


def fetch(r: dict, url: str, title: str = "", why: str = "") -> str:
    """Download a document Claude picked into the research's Files/ (PDF as is; a web page as its readable text),
    and queue it for Batsy to read. Returns where it went (for Claude's tool)."""
    import urllib.request

    d = folder(r) / "Files"
    d.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Batsy research assistant)"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        data, ctype = resp.read(25_000_000), resp.headers.get("Content-Type", "")
    name = vault.safe(title or Path(url.split("?")[0]).stem or "document")[:80]
    if data[:5] == b"%PDF-" or "pdf" in ctype:
        p = d / f"{name}.pdf"
        p.write_bytes(data)
    else:  # a web page: keep its readable text
        p = d / f"{name}.md"
        p.write_text(f"Source: {url}\n\n" + _readable(data.decode("utf-8", "replace")))
    with open(_queue_file(), "a") as f:
        f.write(json.dumps({"rid": r["id"], "path": str(p), "url": url, "title": title, "why": why}) + "\n")
    return str(p)


def _readable(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|nav|header|footer|aside|form|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</h\d>|</li>|</div>", "\n", html)
    text = re.sub(r"<[^>]+>", " ", html)
    import html as h

    text = h.unescape(text)
    lines = [re.sub(r"[ \t]+", " ", x).strip() for x in text.splitlines()]
    return "\n".join(x for x in lines if len(x) > 30)[:200_000]


def read_fetched(rid: int) -> list[str]:
    """Read every document queued for this research (local AI): a Source note each, the full text kept in pieces
    so later questions can use all of it. Returns what was read, in a few words each."""
    q = _queue_file()
    try:
        rows = [json.loads(x) for x in q.read_text().splitlines() if x.strip()]
    except OSError:
        return []
    mine, rest = [x for x in rows if x["rid"] == rid], [x for x in rows if x["rid"] != rid]
    q.write_text("".join(json.dumps(x) + "\n" for x in rest))
    r, done = get(rid), []
    for x in mine:
        p = Path(x["path"])
        try:
            done.append(read_document(r, p, url=x.get("url", ""), title=x.get("title", ""), why=x.get("why", "")))
        except Exception as e:
            log.warning("reading %s: %s", p.name, e)
    return done


def read_document(r: dict, p: Path, url: str = "", title: str = "", why: str = "") -> str:
    """Batsy reads a whole document and understands it against the research question (local AI)."""
    from batsy import docs

    pages = docs.extract_text(p, max_chars=60000) if p.suffix.lower() != ".md" else [(1, p.read_text()[:60000])]
    text = "\n".join(t for _, t in pages)
    raw = _ask(
        f"Jonathan's research: \"{r['name']}\" (question: {r.get('question')}). Read this document and answer in JSON only: "
        "{\"title\": \"its real title\", \"authors\": \"\", \"year\": \"\", \"what\": \"what it is in one line\", "
        "\"points\": [\"its 3-5 key points, one line each, with numbers when it gives them\"], "
        "\"relates\": \"1-2 sentences: how it helps or challenges his question\"}\n\n" + text[:7000], 500, json_mode=True)
    try:
        j = json.loads(raw)
    except ValueError:
        j = {}
    guess = str(j.get("title") or "").strip()
    if not guess or guess.lower() == r["name"].lower() or guess.lower() == (r.get("question") or "").lower():
        guess = title or p.stem  # the AI gave the research's own name: use the document's (Claude's title / file name)
    meta = {"kind": "document", "file": str(p), "url": url, "title": guess[:160],
            "authors": str(j.get("authors") or "")[:120], "year": str(j.get("year") or "")[:4],
            "what": str(j.get("what") or why or "")[:160], "points": [str(x)[:220] for x in (j.get("points") or [])][:5],
            "relates": str(j.get("relates") or why or "")[:400]}
    add_source(r["id"], meta, text)
    for i in range(0, min(len(text), 30000), 2500):  # the full text in pieces: "what do we know about…" finds it
        _add_item(r["id"], "excerpt", f"{meta['title'][:60]} (part {i // 2500 + 1})", text[i:i + 2500], str(p))
    return meta["what"] or meta["title"]


def cli(argv: list[str]) -> None:
    """`batsy research ...`: what Claude uses to hand documents and findings to Batsy (and handy for testing)."""
    import argparse

    from batsy.config import Config

    cfg = Config.load()
    vault.configure(cfg)
    configure(cfg)
    ap = argparse.ArgumentParser(prog="batsy research")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    s = sub.add_parser("show"); s.add_argument("name")
    s = sub.add_parser("fetch"); s.add_argument("name"); s.add_argument("url"); s.add_argument("--title", default="")
    s.add_argument("--why", default="")
    s = sub.add_parser("add-finding"); s.add_argument("name"); s.add_argument("text")
    s.add_argument("--kind", choices=["finding", "question", "idea", "next"], default="finding")
    s = sub.add_parser("add-note"); s.add_argument("name"); s.add_argument("text")
    s = sub.add_parser("search"); s.add_argument("name"); s.add_argument("query")
    s = sub.add_parser("papers"); s.add_argument("query")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        print(json.dumps([{k: r[k] for k in ("name", "question", "notes", "sources")} for r in all_research()], indent=1))
        return
    if a.cmd == "papers":
        from batsy import research_sources

        print(json.dumps(research_sources.papers(a.query), indent=1))
        return
    r = find(a.name)
    if not r:
        raise SystemExit(f"No research called {a.name!r}. Have: {[x['name'] for x in all_research()]}")
    if a.cmd == "show":
        print(vault.read(hub(r)))
    elif a.cmd == "fetch":
        print(f"saved for Batsy to read: {fetch(r, a.url, a.title, a.why)}")
    elif a.cmd == "add-finding":
        add_finding(r["id"], a.kind, a.text)
        print("added")
    elif a.cmd == "add-note":
        add_note(r["id"], a.text, "claude")
        print("added")
    elif a.cmd == "search":
        print(json.dumps([{k: h[k] for k in ("kind", "title", "text")} for h in retrieve(r["id"], a.query)], indent=1))
    vault.flush()


# --- the local AI -----------------------------------------------------------------------------------------------------
def _ask(prompt: str, max_tokens: int = 300, json_mode: bool = False) -> str:
    """The free local AI, as a background job (it steps aside while you talk to Batsy)."""
    from batsy import llm_hosts

    body = {"model": _cfg.get("local.model"), "keep_alive": _cfg.get("local.keep_alive"),
            "messages": [{"role": "user", "content": prompt}],
            "options": {"temperature": 0.2, "num_predict": max_tokens, "num_ctx": 4096}}
    if json_mode:
        body["format"] = "json"
    return llm_hosts.background_chat(_cfg, body)


def _short(text: str, n: int = 8) -> str:
    words = re.sub(r"\s+", " ", re.sub(r"[^\w\s'%.-]", " ", text)).strip().split()
    s = " ".join(words[:n]) + ("…" if len(words) > n else "")
    return s[:1].upper() + s[1:]


def _title(name: str) -> str:
    name = re.sub(r"^(?:a |the |my )", "", name.strip(" .?!"), flags=re.I)
    small = {"a", "an", "the", "of", "in", "on", "for", "to", "and", "or", "with", "at", "by", "from", "vs"}
    words = name.split()
    return " ".join(w if w.isupper() or (i and w.lower() in small) else w[:1].upper() + w[1:]
                    for i, w in enumerate(words))[:80]
