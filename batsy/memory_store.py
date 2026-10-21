"""Local memory: every exchange and remembered fact lives in SQLite on this laptop.

Instead of sending the whole conversation to Claude each time, Batsy sends only a short recent
transcript plus the few memories a local full-text search (FTS5) finds relevant. That keeps each
request small no matter how long Batsy has been running.
"""
import json
import re
import sqlite3
import threading
import time
import uuid

from batsy.config import DATA_DIR

DB_PATH = DATA_DIR / "memory.db"
LEGACY_FACTS = DATA_DIR / "memory.json"

_STOP = set("""a an and are as at be but by can could did do does for from get got have he her him his how i if
in into is it its just let like me my no not now of on or our please she so that the their them then there
these they this to too up us was we were what when where which who why will with would you your yes ok okay
hey batsy tell show make want need know think going something thing""".split())


def _terms(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return list(dict.fromkeys(w for w in words if len(w) > 2 and w not in _STOP))[:12]


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


class MemoryStore:
    def __init__(self, path=DB_PATH):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.fts = True
        with self.lock, self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS exchanges (id INTEGER PRIMARY KEY, t REAL, source TEXT,
                    user TEXT, reply TEXT, actions TEXT);
                CREATE TABLE IF NOT EXISTS facts (id TEXT PRIMARY KEY, t REAL, fact TEXT);
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
            """)
            try:
                self.db.executescript("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS exchanges_fts USING fts5(user, reply, actions,
                        content='exchanges', content_rowid='id');
                    CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(fact, content='facts', content_rowid='rowid');
                    CREATE TRIGGER IF NOT EXISTS ex_ai AFTER INSERT ON exchanges BEGIN
                        INSERT INTO exchanges_fts(rowid, user, reply, actions) VALUES (new.id, new.user, new.reply, new.actions);
                    END;
                    CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
                        INSERT INTO facts_fts(rowid, fact) VALUES (new.rowid, new.fact);
                    END;
                    CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
                        INSERT INTO facts_fts(facts_fts, rowid, fact) VALUES ('delete', old.rowid, old.fact);
                    END;
                """)
            except sqlite3.OperationalError:
                self.fts = False  # sqlite built without FTS5: fall back to LIKE searches
        self._import_legacy()

    def _import_legacy(self) -> None:
        if not LEGACY_FACTS.exists():
            return
        try:
            for f in json.loads(LEGACY_FACTS.read_text()):
                self.add_fact(f["fact"], fid=f.get("id"))
            LEGACY_FACTS.rename(LEGACY_FACTS.with_suffix(".json.imported"))
        except (ValueError, KeyError, OSError):
            pass

    # --- conversation -----------------------------------------------------
    def add_exchange(self, source: str, user: str, reply: str, actions: list[str]) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT INTO exchanges (t, source, user, reply, actions) VALUES (?,?,?,?,?)",
                            (time.time(), source, user, reply, "; ".join(actions)))

    def session_start(self) -> float:
        row = self.db.execute("SELECT value FROM state WHERE key='session_start'").fetchone()
        return float(row["value"]) if row else 0.0

    def new_session(self) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES ('session_start', ?)", (str(time.time()),))

    def recent(self, limit: int, max_age_s: float) -> list[sqlite3.Row]:
        since = max(self.session_start(), time.time() - max_age_s)
        with self.lock:
            rows = self.db.execute("SELECT * FROM exchanges WHERE t >= ? ORDER BY id DESC LIMIT ?",
                                   (since, limit)).fetchall()
        return rows[::-1]

    def search_exchanges(self, text: str, limit: int, before_id: int | None) -> list[sqlite3.Row]:
        terms = _terms(text)
        if not terms:
            return []
        cap = before_id if before_id is not None else 1 << 62
        with self.lock:
            if self.fts:
                q = " OR ".join(f'"{t}"' for t in terms)
                return self.db.execute(
                    "SELECT e.* FROM exchanges_fts f JOIN exchanges e ON e.id = f.rowid "
                    "WHERE exchanges_fts MATCH ? AND e.id < ? ORDER BY bm25(exchanges_fts) LIMIT ?",
                    (q, cap, limit)).fetchall()
            like = " OR ".join("(user LIKE ? OR reply LIKE ?)" for _ in terms)
            args = [a for t in terms for a in (f"%{t}%", f"%{t}%")]
            return self.db.execute(f"SELECT * FROM exchanges WHERE ({like}) AND id < ? ORDER BY id DESC LIMIT ?",
                                   (*args, cap, limit)).fetchall()

    # --- facts ------------------------------------------------------------
    def add_fact(self, fact: str, fid: str | None = None) -> str:
        fid = fid or uuid.uuid4().hex[:5]
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO facts (id, t, fact) VALUES (?,?,?)", (fid, time.time(), fact))
        return fid

    def forget_fact(self, fid: str) -> bool:
        with self.lock, self.db:
            return self.db.execute("DELETE FROM facts WHERE id = ?", (fid,)).rowcount > 0

    def all_facts(self) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute("SELECT * FROM facts ORDER BY t").fetchall()

    def search_facts(self, text: str, limit: int) -> list[sqlite3.Row]:
        terms = _terms(text)
        if not terms:
            return []
        with self.lock:
            if self.fts:
                q = " OR ".join(f'"{t}"' for t in terms)
                return self.db.execute(
                    "SELECT facts.* FROM facts_fts JOIN facts ON facts.rowid = facts_fts.rowid "
                    "WHERE facts_fts MATCH ? ORDER BY bm25(facts_fts) LIMIT ?", (q, limit)).fetchall()
            like = " OR ".join("fact LIKE ?" for _ in terms)
            return self.db.execute(f"SELECT * FROM facts WHERE {like} LIMIT ?",
                                   (*[f"%{t}%" for t in terms], limit)).fetchall()

    # --- the context block sent with each request -------------------------
    def build_context(self, text: str, cfg) -> str:
        m = cfg.get("memory")
        parts = []

        facts = self.all_facts()
        if len(facts) > m["all_facts_below"]:
            facts = self.search_facts(text, m["max_facts"])
        if facts:
            parts.append("[Things you remember]\n" + "\n".join(f"- ({f['id']}) {f['fact']}" for f in facts))

        recent = self.recent(m["recent_exchanges"], m["recent_minutes"] * 60)
        oldest = recent[0]["id"] if recent else None
        older = self.search_exchanges(text, m["related_exchanges"], oldest)
        if older:
            older = [r for r in older if r["source"] != "alert"]
        if older:
            parts.append("[Possibly related older conversation]\n" + "\n".join(
                f"{time.strftime('%b %d', time.localtime(r['t']))} user: {_clip(r['user'], 160)} | "
                f"you: {_clip(r['reply'], 200)}" for r in older))
        if recent:
            lines = []
            for r in recent:
                stamp = time.strftime("%H:%M", time.localtime(r["t"]))
                if r["source"] == "alert":
                    lines.append(f"{stamp} you announced: {_clip(r['reply'], 200)}")
                    continue
                lines.append(f"{stamp} user: {_clip(r['user'], 300)}")
                did = f" [did: {_clip(r['actions'], 300)}]" if r["actions"] else ""
                lines.append(f"{stamp} you: {_clip(r['reply'], 400)}{did}")
            parts.append("[Recent conversation, oldest first]\n" + "\n".join(lines))
        return "\n\n".join(parts)


_store: MemoryStore | None = None


def get_store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store
