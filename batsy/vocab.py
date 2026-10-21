"""Phrases that mean "show me a picture of X" or "pull up X", matched locally (instant, no AI).

Starts with the user's usual words. When they phrase it differently and the AI decides it was a show /
pull-up request, the new phrasing is learned as a template ("gimme a look at {x}") so next time it
skips the AI entirely. `batsy vocab` lists and removes learned phrases.
"""
import re
import sqlite3
import threading
import time

from batsy.config import DATA_DIR

DB = DATA_DIR / "vocab.db"

SEEDS = {
    "picture": [
        "show me a picture of {x}", "show me a photo of {x}", "show me an image of {x}", "show me a pic of {x}",
        "show me pictures of {x}", "show me photos of {x}", "show me images of {x}", "picture of {x}",
        "show me what {x} looks like", "show me how {x} looks", "what does {x} look like", "what do {x} look like",
        "let me see {x}", "i want to see {x}",
    ],
    "pullup": [
        "pull up connections for {x}", "pull up connections on {x}", "pull up connections of {x}",
        "pull up info on {x}", "pull up information on {x}", "pull up details on {x}", "pull up {x}",
        "bring up {x}",
    ],
}
# Subjects that aren't "a thing to look up" (the user's own files, the screen, the weather...): leave to the AI.
NOT_A_THING = re.compile(r"^(my|your|our|the screen|screen|this|that|it|how|what|where|when|why|which|"
                         r"the weather|weather|me|us)\b")
# Phrasings too generic to ever learn: they'd hijack ordinary questions.
QUESTION_WORDS = {"what", "what's", "whats", "who", "who's", "whos", "how", "why", "when", "where", "which", "is",
                  "are", "do", "does", "can", "could", "tell", "explain"}
ARTICLES = {"the", "a", "an", "some"}
POLITE = r"(?:(?:hey |ok )?(?:can|could|would) you |please |just )?"


def compile_template(template: str) -> re.Pattern:
    """'set brightness to {level}' -> regex with a named group per {slot}. A slot swallows a leading article,
    and polite openers ("can you", "please") and a trailing "please" are always allowed."""
    parts = re.split(r"\{(\w+)\}", template)
    rx = "^" + POLITE
    for i, part in enumerate(parts):
        rx += re.escape(part) if i % 2 == 0 else rf"(?:(?:the|a|an|some) )?(?P<{part}>.+?)"
    return re.compile(rx + r"(?: please)?$")


class Vocab:
    def __init__(self):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(DB), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock, self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS phrases (template TEXT PRIMARY KEY, intent TEXT, "
                            "learned INTEGER, uses INTEGER DEFAULT 0, t REAL)")
            for intent, templates in SEEDS.items():
                for t in templates:
                    self.db.execute("INSERT OR IGNORE INTO phrases VALUES (?,?,0,0,?)", (t, intent, time.time()))
        self._compile()

    def _compile(self) -> None:
        with self.lock:
            rows = self.db.execute("SELECT template, intent FROM phrases").fetchall()
        # longest templates first so "pull up connections for {x}" beats "pull up {x}"
        rows = sorted(rows, key=lambda r: len(r["template"]), reverse=True)
        self.patterns = []
        for r in rows:
            self.patterns.append((compile_template(r["template"]), r["intent"], r["template"]))

    def match(self, n: str) -> tuple[str, str] | None:
        """Normalised utterance -> (intent, subject) or None."""
        for rx, intent, template in self.patterns:
            m = rx.match(n)
            if m and m.group("x").strip() and not NOT_A_THING.match(m.group("x")):
                with self.lock, self.db:
                    self.db.execute("UPDATE phrases SET uses = uses + 1 WHERE template = ?", (template,))
                return intent, m.group("x").strip()
        return None

    def learn(self, n: str, intent: str, subject: str) -> str | None:
        """Learn the phrasing of utterance n around the subject the AI showed. Returns the new template."""
        words = n.split()
        subj = set(re.findall(r"[a-z0-9]+", subject.lower()))
        hits = [i for i, w in enumerate(words) if w.strip("'s") in subj or w in subj]
        if not hits:
            return None  # the AI paraphrased the subject entirely; nothing reliable to learn
        start, end = min(hits), max(hits)
        while start > 0 and words[start - 1] in ARTICLES:  # "...look at the {x}" -> "...look at {x}"
            start -= 1
        template = " ".join(words[:start] + ["{x}"] + words[end + 1:]).strip()
        fixed = [w for w in template.replace("{x}", "").split() if w not in ARTICLES]
        # needs real "show" wording, not a plain question like "what is {x}" / "who's {x}"
        if len(fixed) < 2 or (fixed[0] in QUESTION_WORDS and len(fixed) <= 3):
            return None
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO phrases VALUES (?,?,1,1,?)", (template, intent, time.time()))
        self._compile()
        return template

    def list(self) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute("SELECT * FROM phrases ORDER BY learned DESC, intent, template").fetchall()

    def forget(self, template: str) -> bool:
        with self.lock, self.db:
            gone = self.db.execute("DELETE FROM phrases WHERE template = ?", (template,)).rowcount > 0
        self._compile()
        return gone


_vocab: Vocab | None = None


def get_vocab() -> Vocab:
    global _vocab
    if _vocab is None:
        _vocab = Vocab()
    return _vocab
