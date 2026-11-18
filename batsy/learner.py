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



NOISE = {"uh", "um", "umm", "hmm", "mm", "mmm", "mmhmm", "mhm", "ah", "oh", "okay", "ok", "so", "yeah", "yes", "no",
         "like", "hey", "huh", "right", "sorry", "what", "the", "a"}


def clear_request(text: str) -> bool:
    """Worth teaching a skill for? Not room noise or rambling: "Bluetooth. Mm-hmm." and "This fatal life can die.
    I'm filled in the corner" became skills before this. At least 3 real words, few fillers, one sentence-ish."""
    words = re.findall(r"[a-z0-9']+", text.lower().replace("mm-hmm", "mmhmm"))
    real = [w for w in words if w not in NOISE]
    sentences = len([s for s in re.split(r"[.?!]+", text) if s.strip()])
    return len(real) >= 3 and len(real) >= 0.7 * len(words) and len(words) <= 25 and sentences <= 2

BUDGET = DATA_DIR / "learn_budget.json"
LEARNS_PER_DAY = 3  # background skill-learning runs (each is a Claude run): only for real repeats


def repeated(norm: str, days: int = 7, need: int = 2) -> bool:
    """Has this request (or one worded almost the same) come up at least `need` times lately? One-offs ("i don't
    know", "the batsy folder") were being turned into skills after a single slow answer."""
    since = time.time() - days * 86400
    try:
        db = sqlite3.connect(str(DB), timeout=2)
        rows = [r[0] for r in db.execute("SELECT norm FROM requests WHERE t > ?", (since,))]
        db.close()
    except sqlite3.Error:
        return False
    same = sum(1 for r in rows if r == norm or difflib.SequenceMatcher(None, r, norm).ratio() >= 0.85)
    return same >= need


def spend_learn() -> bool:
    """Take one of today's background learning runs; False when today's are used up."""
    import json

    day = time.strftime("%Y-%m-%d")
    try:
        b = json.loads(BUDGET.read_text())
    except (OSError, ValueError):
        b = {}
    if b.get("day") != day:
        b = {"day": day, "n": 0}
    if b["n"] >= LEARNS_PER_DAY:
        log.info("skill learning: today's %d runs are used up", LEARNS_PER_DAY)
        return False
    b["n"] += 1
    BUDGET.write_text(json.dumps(b))
    return True


_FILL = {"uh", "um", "umm", "hmm", "mm", "mmm", "mmhmm", "mhm", "ah", "oh", "yeah", "okay", "ok", "like", "right", "just",
         "huh"}
_TRAIL = re.compile(r"(?:\b(?:mm+|mmhmm|mhm|hmm|uh|um|yeah|okay|ok|just|right|i'?m sorry|you know)\s*)+$")
_ADDRESS = re.compile(
    r"^(?:(?:hey|ok|okay|so|and|now|please|batsy|uh|um)\s+)*(?:what|whats|what's|who|whos|why|how|when|where|which|is|"
    r"are|can|could|would|will|do|does|did|should|tell|show|open|close|play|pause|stop|start|turn|set|remind|remember|"
    r"put|make|slice|print|send|find|search|check|give|take|read|call|add|go|skip|next|resume|volume|lights?|switch|"
    r"ask|note|jot|let's|lets|i want|i need|help)\b")


def chatter(norm: str) -> bool:
    """Talk in the room the mic caught after a false "Batsy", not a request: "such a thank you mmhmm", "this fatal
    life can die...". Half of what reached a model was this (each one 12-35 s of CPU and a confused reply). Only
    used once instant commands and skills have had their go, and only for speech."""
    w = norm.split()
    if not w or norm in ("yes", "yeah", "yep", "no", "nope"):  # an answer, with no question pending
        return True
    if len(w) == 1 and not _ADDRESS.match(norm) and norm not in ("weather", "time", "date", "battery", "news",
                                                                 "temperature", "help", "status"):
        return True  # one word that no command took ("somebody"): a fragment of talk, not a question
    if _ADDRESS.match(norm) and not _TRAIL.search(norm):
        return False
    if _TRAIL.search(norm) and not _ADDRESS.match(norm):
        return True
    return len(w) >= 4 and sum(x in _FILL for x in w) / len(w) > 0.25


_STOP = {"what", "whats", "what's", "is", "are", "was", "the", "a", "an", "of", "in", "on", "to", "for", "me", "tell",
         "about", "do", "does", "how", "who", "whos", "who's", "one", "sentence", "please", "and", "can", "you"}


def _same_subject(a: str, b: str) -> bool:
    """Worded differently, same question: "whats the capital of japan" / "what is the capital of japan". Not
    "what is a resistor" / "what is a transistor" (nearly the same letters, a different thing)."""
    def words(x):
        return {w.rstrip("s") for w in x.replace("'", "").split() if w not in _STOP}
    return words(a) == words(b)


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
            cols = [r[1] for r in self.db.execute("PRAGMA table_info(requests)")]
            if "first" not in cols:  # seconds until the first spoken word (what feels fast or slow)
                self.db.execute("ALTER TABLE requests ADD COLUMN first REAL")
        self.last_answer: str | None = None  # norm of the last remembered/remembering answer, for "that's wrong"

    # --- the log ------------------------------------------------------------------------------------------------
    def record(self, text: str, norm: str, route: str, secs: float, reply: str = "", first: float | None = None) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT INTO requests (t, text, norm, route, secs, reply, first) VALUES (?,?,?,?,?,?,?)",
                            (time.time(), text, norm, route, round(secs, 2), (reply or "")[:500],
                             None if first is None else round(first, 2)))
        log.info("request %r -> %s in %.1fs%s", norm[:60], route, secs,
                 "" if first is None else f" (first word {first:.1f}s)")

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
                close = [k for k in difflib.get_close_matches(norm, keys, n=3, cutoff=0.92) if _same_subject(k, norm)]
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
        if len(self.recent) >= 6 or self.learner.cacheable(norm) or not clear_request(text):
            return
        if not repeated(norm):  # once is a one-off; the second time it's worth making instant
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
            if skills.match(norm) or claude_code.over_budget(self.app.cfg) or not spend_learn():
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
                if not clear_request(text):
                    continue
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
