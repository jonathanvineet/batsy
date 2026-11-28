"""The names in your life right now, so Batsy hears them right: what you're tracking (QoreChain, Scaler, Wispr), the
people and things in your open to-dos (Noel John), who sends you files (Karan, Rehaan), the models you've had sent,
and the sites you've been on.

  names()        those names, fresh every minute
  hotwords()     a short list to bias the speech recogniser toward while it decodes (fewer mishearings at all)
  correct(text)  afterwards: a phrase that *sounds* like one of them becomes it ("core chain" -> "QoreChain",
                 "scalar hackathon" -> "Scaler hackathon"). By sound (voice/topics.sound_key), and only to names you
                 have right now, so normal words are left alone.
"""
import datetime as dt
import logging
import re
import time
from pathlib import Path

log = logging.getLogger("batsy.context")
EDGE = {"the", "a", "an", "to", "my", "of", "in", "on", "for", "and", "is", "it", "i", "am", "at", "with", "from", "that"}
_cache: dict = {"t": 0.0, "names": []}
WORKSPACE = Path.home() / "Batsy"
# words that look like names (capitalised) in a to-do but aren't the point of it
PLAIN = {"Deliver", "Make", "Call", "Buy", "Send", "Print", "Slice", "Submit", "Finish", "Pay", "Book", "Email", "Round",
         "Registrations", "Registration", "Submission", "Event", "The", "Fix", "Check", "Reply", "Meet", "Ask", "IST",
         "Two", "Day", "Professional", "Development", "Programme", "From", "On", "For", "Get", "Clean", "Return"}


def _todo_names() -> list[str]:
    from batsy.memory_store import get_store

    st = get_store()
    out = []
    for k in range(-2, 8):
        for r in st.agenda_for((dt.date.today() + dt.timedelta(days=k)).isoformat()):
            if r["done"]:
                continue
            if re.search(r"\([^)]*\)\s*$", r["text"]):
                continue  # a tracked thing's date ("... (QoreChain Developer Fest)"): its name comes from the tracker
            text = r["text"]
            for m in re.finditer(r"\b[A-Z][\w]+(?:'s)?(?:\s+[A-Z][\w]+(?:'s)?)*", text):
                words = [w.removesuffix("'s") for w in m.group(0).split() if w.removesuffix("'s") not in PLAIN]
                if words:
                    out.append(" ".join(words))
    return out


HINTS: dict[str, set[str]] = {}  # name -> words that show you mean it ("hackathon" for Scaler)


def _tracking_names() -> list[str]:
    from batsy import tracker

    out = []
    for t in tracker.active():
        before = len(out)
        b = t.get("brand") or ""
        # the brand as written in its name (QoreChain, not qorechain); otherwise its unusual words, as written
        if b:
            out.append(next((w for w in t["name"].split() if w.lower() == b), b.capitalize()))
        for w in re.findall(r"[A-Za-z][\w]+", t["name"]):
            if w.lower() not in tracker.GENERIC | tracker.KINDS | tracker.STOP and len(w) >= 4:
                out.append(w)
        out += [v.capitalize() for k, v in tracker.HEARD.items() if v in t["keywords"]]
        hint = {w for w in re.findall(r"[a-z]+", t["name"].lower())} | tracker.KINDS | {"tracking", "deadline", "submission"}
        for n in out[before:]:
            HINTS[n] = hint
    return out


def _people() -> list[str]:
    inbox = WORKSPACE / "inbox"
    try:
        return [p.name for p in inbox.iterdir() if p.is_dir() and p.name[0].isalpha() and p.name != "Jonathan"
                and len(p.name) >= 3]
    except OSError:
        return []


def _models(limit: int = 10) -> list[str]:
    """Recently sent models, by the words in their names ("SingleFlute_F_Sharp" -> "Single Flute")."""
    try:
        files = sorted((p for p in (WORKSPACE / "inbox").rglob("*") if p.suffix.lower() in (".stl", ".3mf", ".step",
                                                                                              ".stp", ".obj")),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    except OSError:
        return []
    out = []
    for f in files:
        words = re.findall(r"[A-Z]?[a-z]{3,}", re.sub(r"[_\-.]", " ", f.stem))
        out += [w.capitalize() for w in words[:2]]
    return out


def _sites() -> list[str]:
    from batsy import browser, tracker

    try:
        return [b.capitalize() for b in dict.fromkeys(tracker.brand(r["url"]) for r in browser.recent(24 * 60, 40)) if b]
    except Exception:
        return []


_words: set[str] = set()


def _dictionary() -> set[str]:
    """Everyday English words (lower-case entries only, so names like Karan aren't in it)."""
    if not _words:
        try:
            _words.update(w for w in Path("/usr/share/dict/words").read_text(errors="ignore").split() if w.islower())
        except OSError:
            _words.add("")
    return _words


def names() -> list[str]:
    if time.time() - _cache["t"] < 60:
        return _cache["names"]
    out: list[str] = []
    for part in (_tracking_names, _todo_names, _people, _models, _sites):
        try:
            out += part()
        except Exception as e:
            log.debug("context %s: %s", part.__name__, e)
    seen, keep = set(), []
    plain = _dictionary()
    for n in out:
        if " " not in n and n.lower() in plain and n not in _people() and n not in HINTS:
            continue  # "Camera", "Workshop": everyday words, they'd swallow what you actually said
        if len(n) >= 3 and not n.isdigit() and n.lower() not in seen:
            seen.add(n.lower())
            keep.append(n)
    _cache.update(t=time.time(), names=keep)
    return keep


def hotwords(limit: int = 20) -> list[str]:
    return names()[:limit]


def correct(text: str) -> str:
    """Swap a misheard phrase for the name it sounds like, from the names you have right now."""
    from rapidfuzz import fuzz

    from batsy.voice.topics import sound_key

    if not text:
        return text
    cands = [(n, sound_key(n).replace(" ", "")) for n in names()]
    cands = [(n, k) for n, k in cands if len(k) >= 4]
    if not cands:
        return text
    words = text.split()
    out, i = [], 0
    while i < len(words):
        best = None
        for size in (3, 2, 1):  # the longest phrase that sounds like a name
            if i + size > len(words):
                continue
            chunk = words[i:i + size]
            if size > 1 and re.sub(r"\W", "", chunk[0].lower()) in EDGE or re.sub(r"\W", "", chunk[-1].lower()) in EDGE:
                continue  # never swallow "the", "to", "my" into a name
            core = re.sub(r"[^\w' ]", "", " ".join(chunk))
            key = sound_key(core).replace(" ", "")
            if len(key) < 4:
                continue
            lower = {w.lower().strip(".,?!") for w in words}
            for n, k in cands:
                if size == 1 and core.lower() in _dictionary() and HINTS.get(n) and not (HINTS[n] & lower):
                    continue  # "a scalar field" stays; "the scalar hackathon" is Scaler
                if core.lower().replace(" ", "") == n.lower().replace(" ", ""):
                    best = best or (n, size, 101)
                    continue
                score = fuzz.ratio(key, k)
                if score >= (80 if size == 1 and len(k) >= 5 else 92 if size == 1 else 86) and abs(len(key) - len(k)) <= 2 and (not best or score > best[2]):
                    best = (n, size, score)
            if best:
                break
        if best and best[2] <= 100:
            tail = re.search(r"[^\w']+$", words[i + best[1] - 1])
            out.append(best[0] + (tail.group(0) if tail else ""))
            i += best[1]
        else:
            out.append(words[i])
            i += 1
    fixed = " ".join(out)
    if fixed != text:
        log.info("heard (context): %s  ->  %s", text, fixed)
    return fixed
