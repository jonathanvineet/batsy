"""Batsy getting faster from use: a log of every request (how it was answered, how long it took), remembered
answers for general questions, and turning things you ask again and again into instant code.

- RequestLog: requests.db. Route is "instant", "command", "skill", "cache", "quick" (small local model, no tools),
  "full" (local model with tools), "claude", "look", "3d", "doc" or "learn".
- Answers: a question a model answered ("what's the capital of Japan") is answered from memory next time, in
  milliseconds. Never for time-sensitive, personal or action requests. "That's wrong" forgets it.
- Automator: every few hours (or "learn from what I ask"), requests that keep going to a model are handed to
  Claude to write a skill for, so next time they run as plain code. Claude says when something can't be
  automated (it needs thinking every time); that's remembered so it isn't asked again.
"""
import difflib
import logging
import re
import sqlite3
import threading
import time

from batsy.config import DATA_DIR

log = logging.getLogger("batsy.learner")
DB = DATA_DIR / "requests.db"
MODEL_ROUTES = ("quick", "full", "claude")

# Answers to these change, or are about you: never remember them.
VOLATILE = re.compile(
    r"\b(today|tonight|tomorrow|yesterday|now|right now|currently|current|latest|recent|news|weather|temperature|"
    r"price|stock|score|time|date|this (week|month|year)|my|me|mine|i|i'm|im|you|your|you're|open|close|play|set|"
    r"turn|start|stop|show|look|screen|camera|file|remind|timer|battery|cpu|laptop|computer|this|that|it|yourself|"
    r"batsy|git|update|restart|run|folder|install)\b")
# A real question ("do you..." is chat and "do a git pull" a command: neither is remembered).
QUESTION = re.compile(r"^(what|whats|what's|who|whos|who's|where|when|why|how|which|is|are|was|were|does|did|"
                      r"define|explain|tell me about)\b")


class Learner:
    def __init__(self):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(DB), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock, self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS requests (t REAL, text TEXT, norm TEXT, route TEXT, secs REAL, "
                            "reply TEXT)")
            self.db.execute("CREATE TABLE IF NOT EXISTS answers (norm TEXT PRIMARY KEY, text TEXT, reply TEXT, t REAL, "
                            "hits INTEGER DEFAULT 0)")
            self.db.execute("CREATE TABLE IF NOT EXISTS automated (norm TEXT PRIMARY KEY, t REAL, outcome TEXT)")
        self.last_answer: str | None = None  # norm of the last remembered/remembering answer, for "that's wrong"

    # --- the log ------------------------------------------------------------------------------------------------
    def record(self, text: str, norm: str, route: str, secs: float, reply: str = "") -> None:
        with self.lock, self.db:
            self.db.execute("INSERT INTO requests VALUES (?,?,?,?,?,?)",
                            (time.time(), text, norm, route, round(secs, 2), (reply or "")[:500]))
        log.info("request %r -> %s in %.1fs", norm[:60], route, secs)

    # --- remembered answers -------------------------------------------------------------------------------------
    @staticmethod
    def cacheable(norm: str) -> bool:
        return bool(QUESTION.match(norm)) and 3 <= len(norm.split()) <= 16 and not VOLATILE.search(norm)

    def recall(self, norm: str) -> str | None:
        if not self.cacheable(norm):
            return None
        with self.lock:
            row = self.db.execute("SELECT norm, reply FROM answers WHERE norm = ?", (norm,)).fetchone()
            if row is None:  # same question, slightly different words: "whats the capital of japan"
                keys = [r["norm"] for r in self.db.execute("SELECT norm FROM answers")]
                close = difflib.get_close_matches(norm, keys, n=1, cutoff=0.92)
                row = self.db.execute("SELECT norm, reply FROM answers WHERE norm = ?", (close[0],)).fetchone() \
                    if close else None
            if row is None:
                return None
            with self.db:
                self.db.execute("UPDATE answers SET hits = hits + 1 WHERE norm = ?", (row["norm"],))
        self.last_answer = row["norm"]
        return row["reply"]

    def remember(self, text: str, norm: str, reply: str) -> None:
        if not self.cacheable(norm) or not reply or len(reply) > 400 or "?" in reply:
            return
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO answers (norm, text, reply, t) VALUES (?,?,?,?)",
                            (norm, text, reply, time.time()))
        self.last_answer = norm

    def forget_last(self) -> bool:
        if not self.last_answer:
            return False
        with self.lock, self.db:
            gone = self.db.execute("DELETE FROM answers WHERE norm = ?", (self.last_answer,)).rowcount
        self.last_answer = None
        return bool(gone)

    # --- turning repeats into code ------------------------------------------------------------------------------
    def candidates(self, days: int = 14, min_count: int = 2, limit: int = 3) -> list[tuple[str, str, int]]:
        """(example text, norm, times asked) for requests that keep going to a model and could be code."""
        since = time.time() - days * 86400
        with self.lock:
            rows = self.db.execute(
                "SELECT norm, MAX(text) AS text, COUNT(*) AS n FROM requests WHERE t > ? AND route IN (?,?,?) "
                "AND norm NOT IN (SELECT norm FROM automated) GROUP BY norm HAVING n >= ? ORDER BY n DESC",
                (since, *MODEL_ROUTES, min_count)).fetchall()
        return [(r["text"], r["norm"], r["n"]) for r in rows if not self.cacheable(r["norm"])][:limit]

    def mark(self, norm: str, outcome: str) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO automated VALUES (?,?,?)", (norm, time.time(), outcome))

    def stats(self, days: int = 7) -> dict:
        since = time.time() - days * 86400
        with self.lock:
            rows = self.db.execute("SELECT route, COUNT(*) AS n, AVG(secs) AS secs FROM requests WHERE t > ? "
                                   "GROUP BY route", (since,)).fetchall()
            remembered = self.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0]
            automated = self.db.execute("SELECT COUNT(*) FROM automated WHERE outcome = 'skill'").fetchone()[0]
        return {"routes": {r["route"]: (r["n"], round(r["secs"] or 0, 1)) for r in rows},
                "remembered": remembered, "automated": automated}


class Automator:
    """Background: every few hours, while Batsy is idle, hand the most repeated model-bound requests to Claude
    to turn into skills (plain code). Also run on demand: "learn from what I ask"."""

    def __init__(self, app, learner: Learner, every_hours: float = 6):
        self.app, self.learner, self.every = app, learner, every_hours * 3600
        self.running = threading.Lock()

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="automator").start()

    def _loop(self) -> None:
        time.sleep(600)  # let Batsy settle after starting
        while True:
            if time.time() - getattr(self.app, "last_turn", 0) > 900:  # quiet for 15 minutes
                try:
                    self.run_once()
                except Exception:
                    log.exception("automation pass failed")
            time.sleep(self.every)

    def learn_soon(self, text: str, norm: str) -> None:
        """A request just needed a model and was slow: have Claude make it instant now, in the background,
        instead of waiting for it to repeat. One at a time, at most 6 an hour, within the Claude budget."""
        now = time.time()
        self.recent = [t for t in getattr(self, "recent", []) if now - t < 3600]
        if len(self.recent) >= 6 or self.learner.cacheable(norm):
            return
        with self.learner.lock:
            if self.learner.db.execute("SELECT 1 FROM automated WHERE norm = ?", (norm,)).fetchone():
                return
        self.recent.append(now)
        threading.Thread(target=self._learn_one, args=(text, norm), daemon=True, name="learn-now").start()

    def _learn_one(self, text: str, norm: str) -> None:
        time.sleep(20)  # let the reply finish, and the brain's own "learn from what I just did" go first
        skills = getattr(self.app, "skills", None)
        if skills is None or not self.running.acquire(blocking=False):
            return
        try:
            from batsy import claude_code

            skills.reload()
            if skills.match(norm) or claude_code.over_budget(self.app.cfg):
                return
            log.info("making %r instant", norm)
            out = skills.learn(text, lambda _q: False, execute=False)
            ok = out.get("kind") == "skill" and out.get("ok")
            self.learner.mark(norm, "skill" if ok else "needs thinking")
            if ok:
                import subprocess

                subprocess.Popen(["notify-send", "-a", "Batsy", "Batsy learned",
                                  f"Next time this is instant: {text.strip(' .?!')}"])
        except Exception:
            log.exception("learning %r failed", norm)
        finally:
            self.running.release()

    def run_once(self) -> list[str]:
        """Returns the requests that were turned into instant skills."""
        skills = getattr(self.app, "skills", None)
        if skills is None or not self.running.acquire(blocking=False):
            return []
        learned = []
        try:
            from batsy import claude_code

            for text, norm, n in self.learner.candidates():
                if claude_code.over_budget(self.app.cfg):
                    break
                log.info("automating %r (asked %d times)", norm, n)
                out = skills.learn(text, lambda _q: False, execute=False)  # write the code; first real use tests it
                if out.get("kind") == "skill" and out.get("ok"):
                    self.learner.mark(norm, "skill")
                    learned.append(text)
                else:
                    self.learner.mark(norm, "needs thinking")
        finally:
            self.running.release()
        if learned:
            import subprocess

            subprocess.Popen(["notify-send", "-a", "Batsy", "Batsy learned",
                              "Now instant: " + "; ".join(t.strip(" .?!") for t in learned)])
        return learned
