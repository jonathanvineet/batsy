"""What a song FEELS like to the person listening, so the endless radio carries on that feeling (and that era),
not just the same composer.

Spotify switched off moods for new apps (no audio-features, no recommendations), so Batsy keeps its own. Claude
tags every song in the library and the catalogue once, in batches, in the background: moods from MOODS, an energy
from 1 (still) to 5 (all-out), the decade it's from, and its language:

    music_moods.json    {"spotify:track:...": {"m": ["feel-good"], "e": 3, "y": 1990, "l": "ta"}, ...}

It tags them the way THIS listener hears them. The profile is a template, filled in from whoever's Spotify is
linked (build_profile): their history (recently played, top songs over weeks / months / years, saved songs, what
they asked Batsy to play) summarised by Claude into how they listen, plus every correction they make by voice:

    music_profile.json  {"summary": "...", "rules": ["Raja Raja Chozhan: an old 80s/90s feel-good song", ...],
                         "overrides": {"spotify:track:...": {...}}}

Overrides always win over Claude's tags; rules and the summary go into every tagging prompt.

  similar(seed, ...)   songs that feel like the seed: shared moods, close energy, the same era and language, at most
                       a couple per artist (so it isn't one composer's discography)
  wanted(text)         "play something sad", "old feel-good tamil songs", "some 90s kuthu" -> what to look for
  correct(...)         "this song is an old feel-good song": the override + a rule for the next tagging
"""
import json
import logging
import random
import re
import threading
import time

from batsy.config import DATA_DIR

log = logging.getLogger("batsy.moods")
FILE = DATA_DIR / "music_moods.json"
PROFILE = DATA_DIR / "music_profile.json"
MOODS = ["sad", "romantic", "chill", "feel-good", "dance", "hype", "emotional", "devotional"]
BATCH = 120
_lock = threading.Lock()
_busy = threading.Event()

# how you say a mood -> the tag
WORDS = {
    "sad": "sad", "heartbreak": "sad", "heartbroken": "sad", "breakup": "sad", "lonely": "sad", "depressing": "sad",
    "romantic": "romantic", "romance": "romantic", "love": "romantic", "melody": "romantic", "melodies": "romantic",
    "chill": "chill", "relaxing": "chill", "relax": "chill", "calm": "chill", "lofi": "chill", "lo-fi": "chill",
    "sleep": "chill", "study": "chill", "focus": "chill", "soft": "chill", "peaceful": "chill",
    "happy": "feel-good", "feel good": "feel-good", "feel-good": "feel-good", "feelgood": "feel-good",
    "cheerful": "feel-good", "upbeat": "feel-good", "fun": "feel-good",
    "dance": "dance", "kuthu": "dance", "party": "dance", "club": "dance", "dancing": "dance",
    "hype": "hype", "mass": "hype", "gym": "hype", "workout": "hype", "pump": "hype", "motivational": "hype",
    "energetic": "hype", "intense": "hype",
    "emotional": "emotional", "soulful": "emotional", "nostalgic": "emotional",
    "devotional": "devotional", "bhakti": "devotional", "god": "devotional", "spiritual": "devotional",
}
ENERGY = {"chill": 2, "sad": 2, "romantic": 2, "emotional": 2, "devotional": 2, "feel-good": 3, "dance": 4, "hype": 5}
LANGS = {"tamil": "ta", "english": "en", "hindi": "hi", "telugu": "te", "malayalam": "ml", "kannada": "kn"}
# how you say an era -> (from, to) decades
ERAS = {"70s": (1970, 1970), "80s": (1980, 1980), "90s": (1990, 1990), "2000s": (2000, 2000), "2010s": (2010, 2010),
        "seventies": (1970, 1970), "eighties": (1980, 1980), "nineties": (1990, 1990),
        "old": (1960, 1990), "retro": (1960, 1990), "classic": (1960, 1990), "vintage": (1960, 1980),
        "evergreen": (1960, 1990), "golden": (1960, 1990), "throwback": (1990, 2000),
        "new": (2020, 2020), "latest": (2020, 2020), "recent": (2020, 2020), "modern": (2015, 2020)}
_WORD_RX = re.compile(r"\b(" + "|".join(sorted(map(re.escape, WORDS), key=len, reverse=True)) + r")\b")
_LANG_RX = re.compile(r"\b(" + "|".join(LANGS) + r")\b")
_ERA_RX = re.compile(r"\b(" + "|".join(sorted(map(re.escape, ERAS), key=len, reverse=True)) + r")\b|\b(?:19|')?([6-9]0)'?s\b")

PROMPT = """You are tagging songs for one person's music player, so its radio keeps playing songs that FEEL the same
TO THEM. Tag each song the way this listener hears it:

{profile}
For each numbered song below (title | artist), give:
- moods: 1-3 of exactly these words: {moods}
  ("feel-good" = happy, light, smile-on-your-face; "dance" = kuthu / party / club; "hype" = mass / pump-up / workout;
   "chill" = calm, soft, lo-fi; "emotional" = moving, nostalgic, soulful)
- energy: 1 (very still) to 5 (all-out)
- decade the song was released: 1960, 1970, 1980, 1990, 2000, 2010 or 2020
- language: two-letter code (ta, en, hi, te, ml, kn, or other)
Use what you know about the actual song (an Ilaiyaraaja song from the 80s is not new because it was re-released).
If you don't know it, guess from the title, artist and film.

Answer ONLY with one line per song, in this exact form, nothing else:
<number>|<mood,mood>|<energy>|<decade>|<language>

Songs:
{songs}"""


# --- the listener's profile (the template) -------------------------------------------------------------------
def profile() -> dict:
    try:
        return json.loads(PROFILE.read_text())
    except (OSError, ValueError):
        return {"summary": "", "rules": [], "overrides": {}}


def _save_profile(p: dict) -> None:
    tmp = PROFILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(p, indent=1, ensure_ascii=False))
    tmp.replace(PROFILE)


def _profile_text() -> str:
    p = profile()
    parts = []
    if p.get("summary"):
        parts.append("How this listener hears music (from their listening history):\n" + p["summary"].strip())
    if p.get("rules"):
        parts.append("What they told me about songs (always follow these, and treat similar songs the same way):\n"
                     + "\n".join(f"- {r}" for r in p["rules"][-60:]))
    return ("\n\n".join(parts) + "\n") if parts else ""


PROFILE_PROMPT = """Here is one person's music listening history from Spotify, and what they asked their voice
assistant to play. Write a short profile (8-12 bullet points) of HOW THEY LISTEN, for tagging songs by mood for
their radio: which languages and eras they love, which artists and films, what "feel-good", "romantic", "sad",
"hype" etc. mean in their music (with example songs from the history), which old songs they treat as feel-good or
nostalgic, and anything that would make a radio feel right to them. Plain text bullets, nothing else.

{history}"""


def build_profile(history: dict, cfg=None) -> str:
    """history: {"Recently played": [...], "Top songs (years)": [...], ...} lists of "title by artist".
    Claude summarises it into how this listener hears music. Keeps their rules and overrides."""
    from batsy import claude_code

    text = "\n\n".join(f"{k}:\n" + "\n".join(f"- {s}" for s in v[:80]) for k, v in history.items() if v)
    summary = claude_code.run(PROFILE_PROMPT.format(history=text), "learning your music taste",
                              args=["--model", "sonnet"], timeout=240, cfg=cfg).strip()
    if not summary or summary.lower().startswith(("claude code isn't", "error")):
        return ""
    p = profile()
    p.update({"summary": summary, "built": time.time(), "sources": {k: len(v) for k, v in history.items()}})
    _save_profile(p)
    return summary


def correct(uri: str, title: str, artist: str, said: str) -> dict | None:
    """'an old 80s feel good song' about a song -> an override that always wins, and a rule for future tagging.
    Returns the new tags, or None if nothing in what was said is a mood or an era."""
    want = wanted(said) or {}
    era = want.get("y")
    if not want.get("m") and not era:
        return None
    current = load().get(uri, {})
    new = {"m": sorted(want.get("m") or current.get("m") or []), "e": want.get("e") or current.get("e", 3),
           "y": (era[0] if era and era[0] == era[1] else (era[1] if era else current.get("y"))),
           "l": current.get("l", "?")}
    p = profile()
    p.setdefault("overrides", {})[uri] = new
    rule = f"{title} by {artist}: {said.strip().rstrip('.')}"
    p.setdefault("rules", [])
    p["rules"] = [r for r in p["rules"] if not r.lower().startswith(f"{title} by {artist}:".lower())] + [rule]
    _save_profile(p)
    return new


# --- tags --------------------------------------------------------------------------------------------------
def load() -> dict:
    try:
        tags = json.loads(FILE.read_text())
    except (OSError, ValueError):
        tags = {}
    tags.update(profile().get("overrides", {}))  # what you said beats what Claude guessed
    return tags


def _load_raw() -> dict:
    try:
        return json.loads(FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save(tags: dict) -> None:
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(tags))
    tmp.replace(FILE)


def _parse(answer: str, batch: list) -> dict:
    out = {}
    for line in answer.splitlines():
        parts = [p.strip() for p in line.strip().strip("`").split("|")]
        if len(parts) < 5 or not parts[0].isdigit():
            continue
        i = int(parts[0]) - 1
        if not 0 <= i < len(batch):
            continue
        moods = [m.strip().lower() for m in parts[1].split(",") if m.strip().lower() in MOODS][:3]
        try:
            energy = max(1, min(5, int(re.sub(r"\D", "", parts[2]) or 3)))
        except ValueError:
            energy = 3
        year = re.sub(r"\D", "", parts[3])
        decade = int(year[:4]) // 10 * 10 if len(year) >= 4 and 1940 <= int(year[:4]) <= 2030 else None
        if moods:
            out[batch[i][2]] = {"m": moods, "e": energy, "y": decade, "l": parts[4].lower()[:2] or "?"}
    return out


def tag(songs: list, cfg=None) -> int:
    """Tag songs [(title, artist, uri), ...] with Claude, BATCH at a time, for this listener. Returns how many."""
    from batsy import claude_code

    done, prof = 0, _profile_text()
    for start in range(0, len(songs), BATCH):
        if cfg is not None and claude_code.over_budget(cfg):
            log.info("moods: Claude budget reached, the rest waits")
            break
        batch = songs[start:start + BATCH]
        listing = "\n".join(f"{i + 1}. {t} | {a}" for i, (t, a, _) in enumerate(batch))
        answer = claude_code.run(PROMPT.format(profile=prof, moods=", ".join(MOODS), songs=listing),
                                 "tagging song moods", args=["--model", "sonnet"], timeout=240, cfg=cfg)
        got = _parse(answer, batch)
        with _lock:
            tags = _load_raw()
            tags.update(got)
            _save(tags)
        done += len(got)
        log.info("moods: tagged %d of %d (batch %d)", len(got), len(batch), start // BATCH + 1)
    return done


def tag_missing(pool: list, cfg=None, everything: bool = False) -> None:
    """In the background: tag every song in the pool that has no tags (or no era) yet. everything: all of them
    again (after the profile changed)."""
    if _busy.is_set():
        return
    try:
        if time.time() - FILE.stat().st_mtime < 300 and not everything:  # a tagging run is going right now
            return
    except OSError:
        pass
    tags = load()
    seen, todo = set(), []
    for t in pool:
        u = t[2]
        if u not in seen and (everything or u not in tags or "y" not in tags[u]):
            seen.add(u)
            todo.append(tuple(t[:3]))
    if not todo:
        return

    def run():
        _busy.set()
        try:
            tag(todo, cfg)
        except Exception:
            log.exception("moods: tagging failed")
        finally:
            _busy.clear()

    threading.Thread(target=run, daemon=True, name="mood-tags").start()


def of(uri: str) -> dict | None:
    return load().get(uri)


def wanted(text: str) -> dict | None:
    """'play something sad' / 'old feel good tamil songs' / 'some 90s kuthu' -> {"m", "e", "y": (from, to), "l"}."""
    t = text.lower()
    moods = {WORDS[w] for w in _WORD_RX.findall(t)}
    eras = [ERAS[m.group(1)] if m.group(1) else (1900 + int(m.group(2)),) * 2 for m in _ERA_RX.finditer(t)]
    exact = [e for e in eras if e[0] == e[1]]
    era = exact[0] if exact else eras[0] if eras else None  # "old 80s": the 80s, not just "old"
    if not moods and not era:
        return None
    lang = _LANG_RX.search(t)
    return {"m": moods, "e": round(sum(ENERGY[x] for x in moods) / len(moods)) if moods else None, "y": era,
            "l": LANGS[lang.group(1)] if lang else None}


def _decades(x) -> tuple | None:
    y = x.get("y")
    if y is None:
        return None
    return tuple(y) if isinstance(y, (list, tuple)) else (y, y)


def _score(seed: dict, tag: dict) -> float:
    a, b = set(seed.get("m") or []), set(tag["m"])
    s = 2.0 * (len(a & b) / len(a | b)) if a else 1.0
    if seed.get("e"):
        s -= 0.45 * abs(seed["e"] - tag.get("e", 3))
    era, y = _decades(seed), tag.get("y")
    if era and y:  # the same era: a 90s feel-good song carries on with old songs, not this year's
        gap = 0 if era[0] <= y <= era[1] else min(abs(y - era[0]), abs(y - era[1])) // 10
        s -= 0.6 * gap
    return s


def _same_song(title: str) -> str:
    """'Koondukkulla (From "Chinna Gounder")' and 'Koondu Kulla' -> one key: a song isn't "more" twice."""
    t = re.sub(r"\(.*?\)|\[.*?\]| - .*$", "", title.lower())
    return re.sub(r"[^a-z0-9]", "", t)


def similar(seed: dict, pool: list, skip: set, n: int = 10, seed_artist: str = "") -> list[str]:
    """Up to n uris that feel like seed, from pool [(title, artist, uri), ...]: same language, shared mood, close
    energy and era; at most 2 per artist (1 by the seed's own artist), one copy of each song, shuffled within the
    best. A small corner (80s Tamil feel-good) is widened a little rather than letting the radio run dry."""
    tags = load()
    lang = seed.get("l")
    era = _decades(seed)
    titles = {uri: title for title, _, uri, *_ in pool}
    picked, per_artist, songs = [], {}, {_same_song(titles.get(u, "")) for u in skip}
    seed_key = (seed_artist or "").split(",")[0].strip().lower()
    for threshold, cap, energy in ((0.4, 2, True), (0.1, 3, False)):  # strict first, then a little wider
        scored = []
        for title, artist, uri, *_ in pool:
            t = tags.get(uri)
            if not t or uri in skip or uri in picked:
                continue
            if lang and lang != "?" and t.get("l") not in (lang, "?"):
                continue
            if era and t.get("y") and (t["y"] < era[0] - 10 or t["y"] > era[1] + 10):
                continue  # more than a decade off: a different time
            s = _score(seed if energy else dict(seed, e=None), t)
            if s > threshold:
                scored.append((s + random.uniform(0, 0.35), artist, uri, title))  # never quite the same order
        scored.sort(reverse=True)
        for _, artist, uri, title in scored:
            key = (artist or "").split(",")[0].strip().lower()
            limit = 1 if seed_key and key and (key in seed_key or seed_key in key) else cap
            song = _same_song(title)
            if per_artist.get(key, 0) >= limit or song in songs:
                continue
            per_artist[key] = per_artist.get(key, 0) + 1
            songs.add(song)
            picked.append(uri)
            if len(picked) >= n:
                return picked
    return picked


def tag_now(title: str, artist: str, uri: str, cfg=None) -> dict | None:
    """One song we have no tags for (just asked for): tag it right away, ~5 s."""
    tag([(title, artist, uri)], cfg)
    return of(uri)


def stats() -> dict:
    tags = load()
    count: dict[str, int] = {}
    for t in tags.values():
        for m in t["m"]:
            count[m] = count.get(m, 0) + 1
    return {"songs": len(tags), "moods": count, "with_era": sum(1 for t in tags.values() if t.get("y")), "t": time.time()}
