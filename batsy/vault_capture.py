"""Auto-capture: what happens in Batsy's day goes into the brain, summarised.

Each function is called from an existing hook (the reply path, the inbox, the printer, reminders, projects, music,
shopping) and returns at once: the writing happens on the vault's writer thread. Nothing here may ever break the
thing that called it, so every call is wrapped.
"""
import datetime as dt
import functools
import logging
import re
import shutil
import time
from pathlib import Path

from batsy import vault

log = logging.getLogger("batsy.vault")

# local replies not worth a line in the timeline (they'd drown out what mattered)
TRIVIAL = re.compile(r"^(?:(?:set |turn )?(?:the )?volume\b|mute|unmute|louder|quieter|pause|resume|play$|stop|"
                     r"next|previous|skip|what time|what'?s the time|time$|date$|what'?s the date|cancel|never ?mind|"
                     r"thanks?|thank you|ok(?:ay)?|yes|no|yeah|nope|hi|hello|hey|good (?:morning|night)|"
                     r"restart yourself|new conversation|close (?:it|that|the camera)|"
                     r"what (?:did|have) i (?:do|done)|what do (?:my )?notes|search my (?:notes|brain)|"
                     r"open (?:my |today'?s? )?(?:brain|note|daily note|obsidian)|sync my brain)\b", re.I)


FILLER = re.compile(r"(?:one sec|one moment|on it|sure|okay|ok|got it|let me (?:check|see|look)(?: that)?|"
                    r"checking|hmm|right|alright)[.!…]*", re.I)


def _bg(fn):
    """Queue the write; never raise into the caller."""
    @functools.wraps(fn)
    def run(*a, **kw):
        try:
            if vault.enabled() and vault.capture_level() != "off":
                vault.submit(_safe, fn, *a, **kw)
        except Exception:
            log.exception("vault capture %s", fn.__name__)
    return run


def _safe(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except Exception:
        log.exception("vault capture %s", fn.__name__)


def _clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _first_sentence(s: str, n: int = 140) -> str:
    s = re.sub(r"\[\[(?:show|card|pull)[^\]]*\]\]", "", str(s or ""))
    parts = [x for x in re.split(r"(?<=[.!?])\s+", s.strip()) if x and not FILLER.fullmatch(x)]
    return _clip(parts[0], n) if parts else ""


def _stamp(t: float | None = None) -> str:
    return time.strftime("%H:%M", time.localtime(t or time.time()))


def _timeline(line: str, t: float | None = None) -> None:
    """Add a line in time order (the day runs 5 AM to 5 AM, so 01:30 comes after 23:00)."""
    p = vault.daily(vault.today(t))
    new = f"- {_stamp(t)} · {line}"
    lines = [x for x in (vault.get_block(p, "timeline") or "").splitlines() if x.strip()]
    if new in lines:
        return

    def key(x: str) -> int:
        m = re.match(r"- (\d\d):(\d\d)", x)
        if not m:
            return 10 ** 6
        h, mi = int(m.group(1)), int(m.group(2))
        return (h + 24 if h < vault.DAY_STARTS_AT else h) * 60 + mi

    k = key(new)
    i = next((i for i, x in enumerate(lines) if key(x) > k), len(lines))
    lines.insert(i, new)
    vault.set_block(p, "timeline", "\n".join(lines), "Timeline")


def _all() -> bool:
    return vault.capture_level() == "all"


# --- conversation ------------------------------------------------------------------------------------------------------
@_bg
def exchange(text: str, reply: str, route: str = "", source: str = "voice") -> None:
    """One line per real request: what was asked → the gist of the answer."""
    if not _all() or not text or source == "alert":
        return
    asked = re.sub(r"^(?:batsy|hey batsy)[, ]+", "", text.strip(), flags=re.I)
    if TRIVIAL.match(asked) or len(asked) < 4:
        return
    if worth(asked, reply):
        _timeline(line(asked, reply, route))


def worth(asked: str, reply: str) -> bool:
    """Real requests only: not cut-off replies, mishearings of background talk, or one-word noises."""
    r = re.sub(r"\s*\(interrupted\)\s*$", "", reply or "").strip()
    if not r or not _first_sentence(r):
        return False
    if re.search(r"\b(?:mm-?hmm|uh-?huh|hmm+)\b", asked, re.I) or len(asked.split()) < 2:
        return False
    return True


def line(asked: str, reply: str, route: str = "") -> str:
    icon = {"claude": "🧠", "learn": "🧠", "cache": "💬"}.get(route, "🗣")
    gist = _first_sentence(re.sub(r"\s*\(interrupted\)\s*$", "", reply or ""))
    return f"{icon} {_clip(asked, 110)}" + (f" → {gist}" if gist else "")


@_bg
def note(text: str, source: str = "voice") -> Path | None:
    """'Note that…': a new note in the Inbox, titled from its first words, and a line in today's timeline."""
    text = text.strip().rstrip(".")
    if not text:
        return None
    text = text[:1].upper() + text[1:]
    title = _clip(re.sub(r"[.!?].*$", "", text), 60).rstrip("…") or "Note"
    p = vault.path("inbox", f"{title}")
    if p.exists():
        p = vault.path("inbox", f"{title} {dt.datetime.now():%H%M%S}")
    vault.write(p, vault.frontmatter(type="note", created=dt.datetime.now().isoformat(timespec="minutes"),
                                     source=source, tags=["inbox"]) +
                f"# {title}\n{text[0].upper() + text[1:]}.\n\nFrom {vault.link(vault.daily_name(vault.today()))}\n")
    _timeline(f"✏️ Noted {vault.link(p.stem)}")
    return p


@_bg
def fact_changed() -> None:
    from batsy import vault_index

    vault_index.write_facts()


# --- files ---------------------------------------------------------------------------------------------------------------
@_bg
def file_received(path: str, sender: str | None, via: str = "", kind: str = "", size: int = 0,
                  t: float | None = None) -> None:
    p = Path(path)
    sender = sender or "Jonathan"
    now = t or time.time()
    card = vault.path("files", p.name)
    if card.exists() and vault.props(card).get("path") not in (str(p), ""):
        card = vault.path("files", f"{p.stem} ({dt.datetime.fromtimestamp(now):%Y-%m-%d %H%M}){p.suffix}")
    day = vault.daily_name(vault.today(now))
    if not card.exists():
        from urllib.parse import quote

        pic = f"\n![]({'file://' + quote(str(p))})\n" if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp") else ""
        vault.write(card, vault.frontmatter(type="file", sender=f"[[{sender}]]", kind=kind or p.suffix.lstrip("."),
                                            received=dt.datetime.fromtimestamp(now).isoformat(timespec="minutes"),
                                            size=size or (p.stat().st_size if p.exists() else 0), via=via,
                                            path=str(p), tags=["file"]) +
                    f"# {p.name}\nFrom [[{sender}]] on [[{day}]] · [Open on the laptop](file://{quote(str(p))})\n{pic}\n"
                    f"## About it\n{vault.block('summary', '_Batsy adds a summary once it has read it._')}\n\n## Notes\n- \n")
    person = vault.path("people", sender)
    vault.create(person, vault.frontmatter(type="person", tags=["person"]) + f"# {sender}\n\n## Files\n{vault.block('files')}\n")
    vault.add_to_block(person, "files", f"- [[{day}]] {vault.link(card.stem)}", "Files", newest_first=True)
    who = "" if sender == "Jonathan" else f" from [[{sender}]]"
    vault.add_to_block(vault.daily(vault.today(now)), "files", f"- 📥 {vault.link(card.stem)}{who}", "Files")
    _timeline(f"📥 Received {vault.link(card.stem)}{who}", now)


@_bg
def file_summary(path: str, summary: str) -> None:
    """docs.learn read it: its card gets the summary."""
    for card in vault.path("files").glob("*.md"):
        if vault.props(card).get("path") == str(path):
            vault.set_block(card, "summary", _clip(summary, 1500), "About it")
            return


@_bg
def file_sent(name: str, device: str) -> None:
    _timeline(f"📤 Sent {name} to {device}")


# --- 3D printing ------------------------------------------------------------------------------------------------------
@_bg
def sliced(info: dict, model: str = "") -> None:
    name = info.get("name") or Path(model).stem
    line = (f"🧊 Sliced **{name}** with {info.get('process', '?')}: {info.get('time', '?')}, "
            f"{info.get('grams', '?')} g")
    _timeline(line)
    vault.add_to_block(vault.ROOT / "03 Areas" / "3D Printing.md", "log",
                       f"- [[{vault.daily_name(vault.today())}]] {line[2:]}", "Log")


def _print_note(job: str, started: float) -> Path:
    return vault.path("prints", f"{dt.datetime.fromtimestamp(started):%Y-%m-%d %H%M} {job}")


@_bg
def print_started(job: str, gcode: str, info: dict | None = None, started: float | None = None) -> None:
    started = started or time.time()
    info = info or {}
    p = _print_note(job, started)
    pic = ""
    preview = Path(gcode).with_suffix(".png")
    if preview.exists():
        att = vault.ROOT / vault.DIRS["attachments"] / f"{p.stem}.png"
        att.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(preview, att)
        pic = f"![[{att.name}|360]]\n"
    day = vault.daily_name(vault.today(started))
    vault.create(p, vault.frontmatter(type="print", job=job, result="printing", date=dt.datetime.fromtimestamp(started)
                                      .isoformat(timespec="minutes"), grams=info.get("grams"), time=info.get("total"),
                                      profile=info.get("profile"), gcode=gcode, tags=["print"]) +
                 f"# {job}\nStarted [[{day}]] {_stamp(started)} · [[3D Printing]]\n\n{pic}\n## How it came out\n- \n")
    vault.add_to_block(vault.daily(vault.today(started)), "prints", f"- 🖨 {vault.link(p.stem, job)}", "Prints")
    _timeline(f"🖨 Started printing {vault.link(p.stem, job)}", started)


@_bg
def print_ended(job: str, result: str, started: float | None = None, error: str = "") -> None:
    p = _print_note(job, started) if started else None
    if not p or not p.exists():
        cands = sorted(vault.path("prints").glob(f"* {vault.safe(job)}.md"))
        p = cands[-1] if cands else None
    if p:
        vault.set_props(p, result=result, ended=dt.datetime.now().isoformat(timespec="minutes"))
    icon = {"done": "✅", "failed": "❌", "cancelled": "⏹"}.get(result, "🖨")
    _timeline(f"{icon} Print {result}: {vault.link(p.stem, job) if p else job}" + (f" ({_clip(error, 80)})" if error else ""))
    vault.add_to_block(vault.ROOT / "03 Areas" / "3D Printing.md", "log",
                       f"- [[{vault.daily_name(vault.today())}]] {job}: {result}", "Log")


# --- reminders, plans, projects, music, shopping -------------------------------------------------------------------
@_bg
def reminder_set(what: str, due: float) -> None:
    if _all():
        _timeline(f"⏰ Reminder for {dt.datetime.fromtimestamp(due):%a %H:%M}: {_clip(what, 100)}")


@_bg
def reminder_fired(message: str) -> None:
    _timeline(f"🔔 {_clip(message, 120)}")


@_bg
def plan(day: dt.date, text: str) -> None:
    vault.add_to_block(vault.daily(day), "plan", f"- [ ] {_clip(text, 200)}", "Plan")


def project_path(title: str) -> Path:
    return vault.path("projects", title)


def _project(title: str, about: str = "", repo: str = "", folder: str = "", url: str = "") -> Path:
    p = project_path(title)
    vault.create(p, vault.frontmatter(type="project", status="active", created=dt.date.today().isoformat(),
                                      updated=dt.date.today().isoformat(), repo=url or repo, folder=folder,
                                      tags=["project"]) +
                 f"# {title}\n{about[:1].upper() + about[1:] if about else ''}\n\n## Next steps\n- [ ] \n\n"
                 f"## Notes\n- \n\n## Log\n{vault.block('log')}\n")
    return p


@_bg
def project_registered(title: str, about: str = "", repo: str = "", folder: str = "", url: str = "") -> None:
    p = _project(title, about, repo, folder, url)
    _timeline(f"🧩 New project {vault.link(p.stem)}")


@_bg
def project_update(title: str, text: str, kind: str = "update") -> None:
    p = _project(title)
    day = vault.daily_name(vault.today())
    vault.add_to_block(p, "log", f"- [[{day}]] {_stamp()} · {_clip(text, 300)}", "Log")
    vault.set_props(p, updated=dt.date.today().isoformat())
    _timeline(f"🧩 {vault.link(p.stem)}: {_clip(text, 120)}")


@_bg
def task_logged(title: str, kind: str, status: str, note: str = "") -> None:
    if kind == "project":
        if status in ("update", "done") and note:
            project_update.__wrapped__(title, note)
        return
    icon = {"done": "✅", "skipped": "⏭"}.get(status)
    if icon:
        _timeline(f"{icon} {title}" + (f": {_clip(note, 80)}" if note else ""))


@_bg
def shopping(query: str, summary: str, report: str = "") -> None:
    p = vault.path("shopping", query.capitalize())
    head = vault.frontmatter(type="shopping", query=query, date=dt.date.today().isoformat(), tags=["shopping"])
    vault.create(p, head + f"# {query.capitalize()}\n\n## Prices\n{vault.block('prices')}\n")
    vault.add_to_block(p, "prices", f"- [[{vault.daily_name(vault.today())}]] {_clip(summary, 400)}"
                       + (f" · [report](file://{report})" if report else ""), "Prices", newest_first=True)
    _timeline(f"🛒 Compared prices: {vault.link(p.stem)}")
