"""Talking to the brain, instantly (no AI): notes, project notes, "what did I do", "what do my notes say", opening
notes in Obsidian, syncing.

  "note that the bed needs levelling" · "jot down: buy PETG" · "add to my brain: …"   -> 00 Inbox
  "add to my robot notes: the driver overheats"                                      -> 02 Projects/<Robot> log
  "what did I do yesterday / today / this week / on Monday"                          -> that day's summary
  "what do my notes say about PLA" · "search my brain for supports"                  -> best notes, read out
  "open my brain" · "open today's note" · "open the robot note"                      -> Obsidian
  "sync my brain"                                                                     -> Git commit + push
  "summarise my day"                                                                  -> today's Summary now
"""
import datetime as dt
import re
import threading

from batsy import vault, vault_capture as cap

PRE = r"^(?:(?:can you|could you|please|hey|ok|okay|so)\s+)*"
NOTE = re.compile(PRE + r"(?:note|jot down|jot|write down|note down|make a note|take a note|add a note|put down|"
                        r"add (?:this )?to (?:my )?(?:brain|second brain|notes|inbox))(?: that| this| of| about)?[:,]?\s+"
                        r"(?P<t>.{3,})$")
PROJECT_NOTE = re.compile(PRE + r"add (?:this )?to (?:my |the )?(?P<p>.+?) (?:project )?notes?[:,]?\s+(?P<t>.{3,})$")
DAY = re.compile(PRE + r"what (?:did|have) i (?:do|done|get done|got done|work(?:ed)? on|accomplish(?:ed)?)"
                       r"(?: (?P<when>yesterday|today|this week|last week|(?:on |last )?(?:monday|tuesday|wednesday|"
                       r"thursday|friday|saturday|sunday)))?$|" + PRE +
                 r"(?:how was|summari[sz]e|recap) (?:my )?(?P<when2>day|yesterday|today|week|this week)$")
ASK = re.compile(PRE + r"(?:what do|what does) (?:my )?(?:notes|brain|vault) (?:say|have|know) (?:about|on) (?P<q>.+)$|"
                 + PRE + r"(?:search|check|look (?:in|through)) (?:my )?(?:notes|brain|vault|second brain) "
                         r"(?:for|about) (?P<q2>.+)$|"
                 + PRE + r"(?:do i have|did i write|find) (?:any )?notes? (?:about|on) (?P<q3>.+)$")
OPEN = re.compile(PRE + r"(?:open|show|pull up) (?:me )?(?:my |the )?(?:(?P<brain>brain|second brain|obsidian|vault|"
                        r"notes app)|(?P<today>today'?s? note|daily note|today in (?:my )?brain|journal)|"
                        r"(?P<which>.+?) (?:note|notes|page))$")
LISTENED = re.compile(PRE + r"what (?:did|have) i (?:listen(?:ed)? to|play(?:ed)?|hear(?:d)?)(?: (?P<when>yesterday|today|"
                             r"this week|last week|(?:on |last )?(?:monday|tuesday|wednesday|thursday|friday|saturday|"
                             r"sunday)))?$|" + PRE + r"what(?:'s| is| was) my (?:music|listening)(?: (?P<when2>today|"
                             r"yesterday|this week))?$")
SYNC = re.compile(PRE + r"(?:sync|save|back ?up|push|commit) (?:my )?(?:brain|vault|second brain|notes)(?: now)?$")
SUMMARY_NOW = re.compile(PRE + r"(?:write|make|do) (?:my |today'?s |the )?(?:summary|daily summary|journal)(?: now)?$")
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def handle(app, n: str, raw: str = "") -> str | None:
    if not vault.enabled():
        return None
    raw = (raw or n).strip()

    m = PROJECT_NOTE.match(n)
    if m and not re.match(r"(?:brain|second brain|inbox)$", m.group("p")):
        proj = _find("projects", m.group("p"))
        text = _original(raw, m.group("t"))
        if proj:
            cap.project_update(proj, text, "note")
            return f"Added to your {proj} notes."
        cap.note(f"{m.group('p')}: {text}")
        return f"I don't have a {m.group('p')} project, so I put it in your inbox."

    m = NOTE.match(n)
    if m and not re.match(r"(?:on|for) ", m.group("t")):
        cap.note(_original(raw, m.group("t")))
        return "Noted in your brain."

    m = DAY.match(n)
    if m:
        return _what_i_did(m.group("when") or m.group("when2") or "today")

    m = LISTENED.match(n)
    if m:
        return _listened(m.group("when") or m.group("when2") or "today")

    m = ASK.match(n)
    if m:
        app._reply("Checking your notes.")  # the local model takes a few seconds
        return _ask(m.group("q") or m.group("q2") or m.group("q3"))

    m = OPEN.match(n)
    if m and (m.group("brain") or m.group("today") or (m.group("which") and _find_any(m.group("which")))):
        if m.group("brain"):
            target, what = None, "your brain"
        elif m.group("today"):
            target, what = vault.daily(), "today's note"
        else:
            target = _find_any(m.group("which"))
            what = f"the {target.stem} note"
        vault.flush()
        return f"Opening {what} in Obsidian." if vault.open_note(target) else \
            "Obsidian isn't installed. Say 'install obsidian', or open ~/Batsy/Brain in any Markdown app."

    if SYNC.match(n):
        def go():
            app.announce(vault.sync())
        threading.Thread(target=go, daemon=True, name="vault-sync-now").start()
        return "Syncing your brain."

    if SUMMARY_NOW.match(n):
        from batsy import vault_daily

        def go():
            vault_daily.summarise(vault.today())
            vault.submit(vault_daily.refresh_home)
            app.announce("Today's summary is in your brain.")
        threading.Thread(target=go, daemon=True, name="vault-summary-now").start()
        return "Writing today's summary."
    return None


def _original(raw: str, said: str) -> str:
    """The note as it was typed (capitals, punctuation), when the normalised words can be found in it."""
    words = said.split()
    if not words:
        return said
    m = re.search(r"(?i)\b" + re.escape(words[0]) + r"\b.*$", raw)
    return m.group(0).strip() if m and len(m.group(0)) >= len(said) * 0.8 else said


def _find(kind: str, name: str) -> str | None:
    """A note title in that folder matching what was said (fuzzy)."""
    from rapidfuzz import fuzz, process

    names = [p.stem for p in vault.path(kind).glob("*.md")]
    hit = process.extractOne(name, names, scorer=fuzz.WRatio) if names else None
    return hit[0] if hit and hit[1] >= 75 else None


def _find_any(name: str):
    from batsy import vault_index

    for kind in ("projects", "areas", "people", "memory"):
        t = _find(kind, name)
        if t:
            return vault.path(kind, t)
    hit = vault_index.note_text(name)
    return hit[0] if hit else None


def _day_for(when: str) -> dt.date:
    today = vault.today()
    when = when.replace("last ", "").replace("on ", "").strip()
    if when in ("yesterday",):
        return today - dt.timedelta(days=1)
    if when in DAYS:
        back = (today.weekday() - DAYS.index(when)) % 7 or 7
        return today - dt.timedelta(days=back)
    return today


def _spoken(md: str) -> str:
    md = re.sub(r"\[\[(?:[^\]|]+\|)?([^\]]+)\]\]", r"\1", md)
    md = re.sub(r"[*_`#>]|^\s*-\s+|\s*\^f-\w+", "", md, flags=re.M)
    return re.sub(r"\s*\n\s*", ". ", md.strip()).replace("..", ".")


def _what_i_did(when: str) -> str:
    if "week" in when:
        d = vault.today()
        if when == "last week":
            d -= dt.timedelta(days=7)
        iso = d.isocalendar()
        p = vault.path("weekly", f"{iso.year}-W{iso.week:02d}")
        if p.exists():
            wins = re.search(r"## Wins\n(.*?)(?:\n## |\Z)", vault.read(p), re.S)
            return "This week: " + _spoken(wins.group(1) if wins else vault.read(p))[:600]
        days = [d - dt.timedelta(days=i) for i in range(d.weekday(), -1, -1)]
        lines = []
        for day in days:
            s = (vault.get_block(vault.daily_path(day), "summary") or "").strip()
            if s and not s.startswith("_"):
                lines.append(f"{day:%A}: " + _spoken(s.split("**Open loops")[0])[:220])
        return " ".join(lines) if lines else "I haven't summarised any day this week yet."
    day = _day_for(when)
    p = vault.daily_path(day)
    label = "Today" if day == vault.today() else "Yesterday" if day == vault.today() - dt.timedelta(days=1) else f"On {day:%A}"
    if not p.exists():
        return f"{label} there's nothing in your brain."
    s = (vault.get_block(p, "summary") or "").strip()
    if s and not s.startswith("_"):
        return f"{label}: " + _spoken(s)[:700]
    tl = [x for x in (vault.get_block(p, "timeline") or "").splitlines() if x.strip()]
    if not tl:
        return f"{label} there's nothing logged."
    items = [re.sub(r"^- \d\d:\d\d · \S+ ", "", x).split(" → ")[0] for x in tl]
    return f"{label}, {len(tl)} things. " + _spoken("; ".join(items[-6:]))[:500] + "."


def _listened(when: str) -> str:
    if "week" in when:
        body = vault.get_block(vault.ROOT / "03 Areas" / "Music.md", "listening") or ""
        week = re.search(r"\*\*This week:\*\* (.*?)(?:\n\n|\Z)", body, re.S)
        return "This week: " + _spoken(week.group(1))[:500] if week else "No music logged this week yet."
    day = _day_for(when)
    lines = [x for x in (vault.get_block(vault.daily_path(day), "music") or "").splitlines() if x.strip()]
    label = "Today" if day == vault.today() else "Yesterday" if day == vault.today() - dt.timedelta(days=1) else f"On {day:%A}"
    if not lines:
        return f"{label} you didn't play anything on Spotify."
    songs = [re.sub(r"^- \d\d:\d\d · ", "", x).split(" · ")[0] for x in lines[1:]]
    return f"{label}: {_spoken(lines[0])}. Including {', '.join(s.strip('*') for s in songs[-4:])}."


def _ask(q: str) -> str:
    from batsy import vault_index

    vault_index.reindex()
    hits = vault_index.search(q, 3, skip=("Batsy/", "Home.md"))
    if not hits:
        return f"Your notes don't mention {q}."
    top = hits[0]
    more = [h["title"] for h in hits[1:]]
    try:  # the free local model answers from the notes themselves, in a sentence or two
        from batsy import vault_daily

        notes = "\n\n".join(f"Note '{h['title']}':\n" + vault_index._plain(vault_index.note_text(h["path"])[1])[:1500]
                             for h in hits[:3] if vault_index.note_text(h["path"]))
        said = vault_daily.local_model(
            f"Jonathan asks: what do my notes say about {q}?\n\nHis notes:\n{notes}\n\nAnswer in one or two short "
            "spoken sentences using only these notes, and say which note it's in. If they don't really cover it, say "
            "so.", 120)
        if said:
            return re.sub(r"[*_#`]", "", said).strip()
    except Exception:
        pass
    return (f"In {top['title']}: {_spoken(top['snippet'])}." +
            (f" Also in {' and '.join(more)}." if more else ""))
