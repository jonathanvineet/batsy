"""Your Gmail, read and understood (read-only): what's important, what's due, and drafts for what you have to hand in.

Setup, once:
  1. console.cloud.google.com: a project, "Gmail API" enabled, OAuth consent screen (External, testing, you as a test
     user), Credentials -> OAuth client ID -> Desktop app -> download the JSON.
  2. Send that JSON to Batsy (or save it as ~/.config/batsy/gmail_client.json), then `batsy mail login`: Google's
     page opens, you allow "read your email", and the token is kept in ~/.config/batsy/gmail_token.json.
Batsy only ever asks for gmail.readonly: it can't send, delete or change anything.

Every 10 minutes the newest mail (not promotions / social / forums) is read by the local AI:
  what kind it is, whether it matters, what's due and when, and whether you have to write something for it.
  - something due  -> a to-do under the clock for that day, and a reminder the evening before
  - important      -> said out loud and shown on the phone ("New email from Limsa Ma'am: …")
  - a report / assignment to write -> a starting draft in your brain (09 Mail/Drafts), from the email itself
Voice: "check my email", "any important emails", "what's due", "read the email from Karan",
       "prepare the report for that email" (add "ask Claude" for Claude to write it properly).
"""
import base64
import datetime as dt
import html
import json
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path

from batsy.config import CONFIG_DIR, DATA_DIR

log = logging.getLogger("batsy.mail")
CLIENT = CONFIG_DIR / "gmail_client.json"
TOKENS = CONFIG_DIR / "gmail"  # one sign-in per account: gmail/<address>.json
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.send"]
SEND = "https://www.googleapis.com/auth/gmail.send"  # only ever used after you say yes to "Send it?"
DB = DATA_DIR / "mail.db"
QUERY = "newer_than:3d -category:promotions -category:social -category:forums"
_lock = threading.Lock()
_last: dict = {}  # the email you were last told about ("prepare the report for that")
_backlog: list[str] = []  # due things found in older mail on a first look: said once, together

UNDERSTAND = """You read one email for Jonathan, an engineering student in India. Today is {today}. Answer in JSON only:
{{"kind": "assignment|deadline|exam|meeting|interview|bill|delivery|personal|work|info|newsletter|spam",
  "important": true or false (true only if he should know today: a deadline, a meeting, a teacher, an interview,
               money, something written to him personally by a person; NOT ads, digests, newsletters, job alerts,
               LinkedIn / social notifications, account or security notices, receipts or anything sent automatically),
  "when": "the email's own words for when it's due or happening, copied exactly and in full: with the date if the
          email gives one, e.g. 'Mon Nov 23, 2026 7:30pm', '28th November', 'this Friday 5 PM', 'tomorrow'
          (or "" if nothing is due or scheduled)",
  "what": "the thing to do or attend, 3-8 words, e.g. 'Submit DBMS lab report'" or "",
  "write": true or false (true if he has to write and hand in something: a report, an assignment, a document),
  "summary": "one sentence: who wants what"}}

From: {sender}
Subject: {subject}
Date: {date}

{body}"""


# --- Google -------------------------------------------------------------------------------------------------------------
def accounts() -> list[str]:
    return sorted(p.stem for p in TOKENS.glob("*.json") if not p.stem.startswith("_")) if TOKENS.is_dir() else []


def _identify(token: Path) -> str | None:
    """Whose sign-in this is (its address); it's renamed to that. None while Google refuses (the API still off)."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    try:
        creds = Credentials.from_authorized_user_file(str(token), json.loads(token.read_text()).get("scopes") or SCOPES[:1])
        who = build("gmail", "v1", credentials=creds, cache_discovery=False).users().getProfile(userId="me").execute()
    except Exception as e:
        log.info("mail: can't identify %s yet (%s)", token.name, str(e)[:120])
        return None
    dest = TOKENS / f"{who['emailAddress']}.json"
    dest.write_text(token.read_text())
    dest.chmod(0o600)
    token.unlink()
    return who["emailAddress"]


def _adopt() -> list[str]:
    """Sign-ins not identified yet (the API was off, or the old single-account file): finished now if Google lets us."""
    TOKENS.mkdir(parents=True, exist_ok=True)
    old = CONFIG_DIR / "gmail_token.json"
    if old.exists():
        old.rename(TOKENS / "_pending-legacy.json")
    return [w for w in (_identify(p) for p in TOKENS.glob("_pending*.json")) if w]


def ready() -> bool:
    return bool(accounts())


def login() -> str:
    """Google's sign-in in the browser (read-only Gmail), once per account; each token is kept for next time."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not CLIENT.exists():
        return ("I need your Google OAuth client file first: make a Desktop app client in console.cloud.google.com "
                f"(Gmail API on), download the JSON and save it as {CLIENT}.")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True, prompt="consent select_account",
                                  success_message="Batsy can read this Gmail now. You can close this tab.")
    TOKENS.mkdir(parents=True, exist_ok=True)
    TOKENS.chmod(0o700)
    pending = TOKENS / f"_pending-{int(time.time())}.json"  # kept first: a refused lookup mustn't lose the sign-in
    pending.write_text(creds.to_json())
    pending.chmod(0o600)
    who = _identify(pending)
    if not who:
        return ("You're signed in, but Google says the Gmail API is off in your Cloud project. Turn it on (APIs and "
                "services, Gmail API, Enable); I'll finish connecting by myself a few minutes after that.")
    f = TOKENS / f"{who}.json"
    n = len(accounts())
    return (f"Connected {who}: I can read it, and send only after you say yes. " + (f"That's {n} accounts; I read them all."
            if n > 1 else "Say connect my other Gmail to add your second one."))


def _service(account: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    f = TOKENS / f"{account}.json"
    creds = Credentials.from_authorized_user_file(str(f), json.loads(f.read_text()).get("scopes") or SCOPES[:1])
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        f.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _text(payload: dict) -> str:
    """The readable text of a message: text/plain if there is one, else the HTML with the tags taken out."""
    plain, rich = [], []

    def walk(p):
        mt, data = p.get("mimeType", ""), (p.get("body") or {}).get("data")
        if data and mt == "text/plain":
            plain.append(base64.urlsafe_b64decode(data).decode(errors="replace"))
        elif data and mt == "text/html":
            rich.append(base64.urlsafe_b64decode(data).decode(errors="replace"))
        for q in p.get("parts") or []:
            walk(q)

    walk(payload)
    if plain:
        return "\n".join(plain)
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", "\n".join(rich))
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t)))


def _files(payload: dict) -> list[str]:
    out = []

    def walk(p):
        if p.get("filename"):
            out.append(p["filename"])
        for q in p.get("parts") or []:
            walk(q)

    walk(payload)
    return out


# --- memory -------------------------------------------------------------------------------------------------------------
def _db() -> sqlite3.Connection:
    db = sqlite3.connect(DB, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS mail (id TEXT PRIMARY KEY, t REAL, sender TEXT, subject TEXT, body TEXT,
                  files TEXT, kind TEXT, important INTEGER, due TEXT, what TEXT, write INTEGER, summary TEXT,
                  acted REAL)""")
    if "account" not in {r[1] for r in db.execute("PRAGMA table_info(mail)")}:
        db.execute("ALTER TABLE mail ADD COLUMN account TEXT DEFAULT ''")
    return db


def recent(hours: float = 48, only_important: bool = False) -> list[sqlite3.Row]:
    with _lock:
        db = _db()
        rows = db.execute("SELECT * FROM mail WHERE t > ? " + ("AND important = 1 " if only_important else "")
                          + "ORDER BY t DESC", (time.time() - hours * 3600,)).fetchall()
        db.close()
    return rows


def due_items(days: int = 14) -> list[sqlite3.Row]:
    with _lock:
        db = _db()
        rows = db.execute("SELECT * FROM mail WHERE due != '' AND due >= ? AND due <= ? ORDER BY due",
                          (dt.date.today().isoformat(), (dt.date.today() + dt.timedelta(days=days)).isoformat() + " 99")
                          ).fetchall()
        db.close()
    return rows


# --- reading new mail ---------------------------------------------------------------------------------------------------
def check(app) -> int:
    """Every connected account: fetch and understand new mail, act on what matters. How many were new."""
    for who in _adopt():
        app.announce(f"Your Gmail {who} is connected now. I'll read it every 10 minutes.")
    total = 0
    for acct in accounts():
        try:
            total += _check(app, acct)
        except Exception as e:
            log.warning("mail %s: %s", acct, e)
    if _backlog:  # one summary for the older mail, not an announcement each
        items, _backlog[:] = list(_backlog), []
        app.announce(f"From your recent email, {len(items)} thing{'s' if len(items) != 1 else ''} "
                     f"{'are' if len(items) != 1 else 'is'} due, now under the clock: " + "; ".join(items[:5]) + ".")
    return total


def _check(app, account: str) -> int:
    from batsy import research

    svc = _service(account)
    ids = [m["id"] for m in svc.users().messages().list(userId="me", q=QUERY, maxResults=30).execute().get("messages", [])]
    with _lock:
        db = _db()
        seen = {r[0] for r in db.execute(f"SELECT id FROM mail WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else set()
        db.close()
    new = 0
    for mid in reversed(ids):  # oldest first
        if mid in seen:
            continue
        m = svc.users().messages().get(userId="me", id=mid, format="full").execute()
        hdr = {h["name"].lower(): h["value"] for h in m["payload"].get("headers", [])}
        sender = re.sub(r"\s*<[^>]+>", "", hdr.get("from", "")).strip('" ') or hdr.get("from", "")
        body = _text(m["payload"])[:6000]
        files = _files(m["payload"])
        try:
            j = json.loads(research._ask(UNDERSTAND.format(
                today=dt.date.today().strftime("%A %d %B %Y"), sender=hdr.get("from", ""),
                subject=hdr.get("subject", ""),
                date=hdr.get("date", ""), body=(body[:3500] + ("\nAttached: " + ", ".join(files) if files else ""))),
                300, json_mode=True))
        except (ValueError, TypeError):
            j = {}
        sent = dt.datetime.fromtimestamp(int(m.get("internalDate", "0")) / 1000).date()
        due = when_to_date(str(j.get("when") or ""), sent)  # the words, read by Batsy's own parser (not the model's maths)
        if not due:  # the subject often has the exact date ("Invitation: … @ Mon Nov 23, 2026 7:30pm")
            due = when_to_date(hdr.get("subject", ""), sent) if re.search(r"\d{4}", hdr.get("subject", "")) else ""
        row = (mid, int(m.get("internalDate", "0")) / 1000, sender, hdr.get("subject", ""), body, json.dumps(files),
               str(j.get("kind") or ""), int(bool(j.get("important"))), due, str(j.get("what") or "")[:80],
               int(bool(j.get("write"))), str(j.get("summary") or "")[:300], None)
        with _lock:
            db = _db()
            db.execute("INSERT OR IGNORE INTO mail VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*row, account))
            db.commit()
            db.close()
        new += 1
        try:
            _act(app, mid)
        except Exception:
            log.exception("acting on mail %s", mid)
    if new:
        log.info("mail: %d new", new)
    return new


def _act(app, mid: str) -> None:
    """Something due -> under the clock + a reminder the evening before; important -> told; to write -> a draft."""
    from batsy.memory_store import get_store
    from batsy.tools import monitor

    with _lock:
        db = _db()
        r = db.execute("SELECT * FROM mail WHERE id = ?", (mid,)).fetchone()
        db.close()
    if not r or r["acted"]:
        return
    said = []
    automated = bool(re.search(AUTOMATED, r["sender"] + " " + (r["body"][:300] if r["body"] else ""), re.I))
    actionable = bool(r["important"]) and r["kind"] in ("assignment", "deadline", "exam", "meeting", "interview", "bill",
                                                          "work", "personal") \
        and not automated
    try:  # anything about something you asked Batsy to keep track of is never junk
        from batsy import tracker

        tracked = tracker.match_mail(r)
        if tracked:
            actionable = True
            threading.Thread(target=tracker.refresh, args=(app, tracked["id"]), daemon=True).start()
    except Exception:
        pass
    if r["due"] and r["what"] and actionable:  # (newsletters and alerts "due today" used to land under the clock)
        day = dt.date.fromisoformat(r["due"][:10])
        if day >= dt.date.today():
            get_store().add_agenda(day.isoformat(), f"{r['what']} (email from {r['sender']})")
            eve = dt.datetime.combine(day - dt.timedelta(days=1), dt.time(18, 0))
            if eve > dt.datetime.now():
                monitor.add_reminder(eve.timestamp(), f"{r['what']}, due {_when(r['due'])} (from {r['sender']})")
            said.append(f"I've put it under the clock for {_when(r['due'])}")
    fresh = r["t"] > time.time() - 3600  # arrived in the last hour: news. Older (a first look at an inbox): quiet
    if not fresh:
        pass  # older mail (a first look at an inbox): read quietly; anything still due is under the clock already
    elif (r["important"] and not automated) or (r["due"] and actionable):  # (foundit "update your salary": not news)
        _last.update(id=mid, t=time.time())
        on = f" on {r['account'].split('@')[0]}" if len(accounts()) > 1 and r["account"] else ""
        app.announce(f"New email{on} from {r['sender']}: {r['summary'] or r['subject']}" + (". " + ", ".join(said) + "."
                                                                                       if said else ""))
    if r["write"] and fresh and actionable:  # never a draft for a job site's or a newsletter's "update your profile"  # (old mail: say "prepare the report for …" if you still need it)
        threading.Thread(target=draft, args=(app, mid, False), daemon=True, name="mail-draft").start()
    with _lock:
        db = _db()
        db.execute("UPDATE mail SET acted = ? WHERE id = ?", (time.time(), mid))
        db.commit()
        db.close()


# sites and robots, not people: never news, never to-dos, never a draft (unless it's about something tracked)
AUTOMATED = (r"no-?reply|do-?not-?reply|newsletter|jobalerts|job-?alerts|notifications?@|alerts?@|marketing|digest|"
             r"linkedin|naukri|unstop|devpost|foundit|monster|indeed|glassdoor|internshala|shine\.com|apna|instahyre|"
             r"wellfound|hirist|cutshort|updates?@|news@|info@|hello@|team@|promo|offers?@")


def when_to_date(words: str, sent: dt.date | None = None) -> str:
    """'this Friday 5 PM' -> '2026-11-27 17:00', 'Mon Nov 23, 2026 7:30pm' -> '2026-11-23 19:30', '' -> ''.
    A weekday or 'tomorrow' counts from the day the email was sent, not today; a date that has passed gives ''."""
    from batsy import instant

    sent = sent or dt.date.today()
    full = re.search(r"(?i)\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? (\d{1,2})(?:st|nd|rd|th)?,? (\d{4})|"
                     r"\b(\d{1,2})(?:st|nd|rd|th)? (jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,? (\d{4})", words)
    clock = re.search(r"(?i)\b(\d{1,2})(?:[:.](\d{2}))? ?([ap])\.?m\b", words)
    if full:  # a written date with its year: exact
        mon = (full.group(1) or full.group(5)).lower()[:3]
        num, year = int(full.group(2) or full.group(4)), int(full.group(3) or full.group(6))
        months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
        try:
            day = dt.date(year, months.index(mon) + 1, num)
        except ValueError:
            return ""
        if day < dt.date.today():
            return ""
        if clock:
            h = int(clock.group(1)) % 12 + (12 if clock.group(3).lower() == "p" else 0)
            return f"{day.isoformat()} {h:02d}:{int(clock.group(2) or 0):02d}"
        return day.isoformat()
    shift = (dt.date.today() - sent).days  # an older email's "Monday" is the Monday after it was sent

    w = re.sub(r"\b(?:this|by|before|on or before|no later than|until|due)\b", " ", words.lower())
    w = re.sub(r"[^a-z0-9: ]", " ", re.sub(r"(\d)\.(\d\d)", r"\1:\2", w))
    w = re.sub(r"\b(\d{1,2}) ?(?:p\.?m|pm)\b", r"\1 pm", re.sub(r"\b(\d{1,2}) ?(?:a\.?m|am)\b", r"\1 am", w))
    w = re.sub(r"\s+", " ", w).strip()
    if not w:
        return ""
    clock = re.search(r"\d{1,2}(?::\d{2})? ?(?:am|pm)|\d{1,2}:\d{2}", w)
    day, _ = instant._day_in(re.sub(r"\bnext\b", "", w).strip())  # a weekday: its next one
    if day is not None and shift > 0 and re.search(r"\b(?:mon|tues|wednes|thurs|fri|satur|sun)day|tomorrow|today|tonight\b", w):
        wd = re.search(r"\b(mon|tues|wednes|thurs|fri|satur|sun)day\b", w)
        if wd:
            k = ["mon", "tues", "wednes", "thurs", "fri", "satur", "sun"].index(wd.group(1))
            day = sent + dt.timedelta(days=(k - sent.weekday()) % 7 or 7)
        else:
            day = sent + dt.timedelta(days=1 if "tomorrow" in w else 0)
        if day < dt.date.today():
            return ""  # it was for a day that has passed
    if day is None:
        found = instant._when(w)
        return found[0].strftime("%Y-%m-%d %H:%M") if found else ""
    if clock:
        found = instant._when(f"on {day.day} {day.strftime('%B').lower()} at {clock.group(0).replace(':', ' ')}")
        if found:
            return found[0].strftime("%Y-%m-%d %H:%M")
    return day.isoformat()


def calendar() -> str:
    """'Tue 24 Nov = 2026-11-24 (today), Wed 25 Nov = 2026-11-25, …, this Friday = Fri 27 Nov, next Friday = Fri 4 Dec':
    the small model reads dates off this instead of counting days (it put "this Friday" on a Thursday)."""
    t = dt.date.today()
    days = [t + dt.timedelta(days=k) for k in range(14)]
    out = [f"{d:%a %-d %b} = {d.isoformat()}" + (" (today)" if k == 0 else " (tomorrow)" if k == 1 else "")
           for k, d in enumerate(days)]
    return "; ".join(out)


def _when(due: str) -> str:
    d = dt.date.fromisoformat(due[:10])
    n = (d - dt.date.today()).days
    day = "today" if n == 0 else "tomorrow" if n == 1 else d.strftime("%A") if n < 7 else d.strftime("%A, %B %-d")
    return day + (f" at {dt.datetime.strptime(due[11:16], '%H:%M').strftime('%-I:%M %p')}" if len(due) >= 16 else "")


def draft(app, mid: str, claude: bool) -> Path | None:
    """A starting draft of what the email asks you to write, in the brain (09 Mail/Drafts): the local AI by default,
    Claude when you ask for it. Opened, and announced."""
    import subprocess

    from batsy import claude_code, research, vault

    with _lock:
        db = _db()
        r = db.execute("SELECT * FROM mail WHERE id = ?", (mid,)).fetchone()
        db.close()
    if not r:
        return None
    task = r["what"] or r["subject"]
    brief = (f"Jonathan (an engineering student) got this email and has to write something for it: {task}"
             f"{' (due ' + _when(r['due']) + ')' if r['due'] else ''}.\n\nFrom: {r['sender']}\nSubject: {r['subject']}\n\n"
             f"{r['body'][:5000]}\n\nWrite a solid first draft in Markdown: first line '# <title>', then what's asked "
             f"for, section by section, with headings, real content where you can and clearly marked [TO FILL] where "
             f"only he can (his data, results, names). Keep to what the email asks; no preamble.")
    if claude:
        md = claude_code.run(brief, f"mail draft: {task[:30]}", timeout=900, cfg=app.cfg,
                             args=["--allowedTools", "WebSearch,WebFetch"])
        if claude_code.limited(md):
            app.announce(f"{md} I'll draft it when it's back.")
            return None
    else:
        md = research._ask(brief, 1200)
    md = (md or "").strip()
    if not md.startswith("#"):
        md = f"# {task}\n\n{md}"
    folder = vault.path("mail") / "Drafts"
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"{vault.safe(f'{dt.date.today():%Y-%m-%d} {task}')}.md"
    p.write_text(vault.frontmatter(type="draft", source="email", sender=r["sender"], subject=r["subject"],
                                   due=r["due"] or "", written_by="Claude" if claude else "Batsy (local AI)") + md + "\n")
    subprocess.Popen(["xdg-open", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    app.announce(f"I've drafted {task} from {r['sender']}'s email: it's open, and in your brain under Mail, Drafts."
                 + ("" if claude else " Say ask Claude to write it if you want a stronger version."))
    return p


def start(app) -> None:
    """Check every 10 minutes, once you've connected Gmail."""
    def loop():
        while True:
            if ready() or list(TOKENS.glob("_pending*.json")) or (CONFIG_DIR / "gmail_token.json").exists():
                try:
                    check(app)
                except Exception as e:
                    log.warning("mail check: %s", e)
            time.sleep(60 if list(TOKENS.glob("_pending*.json")) else 600)  # a sign-in waiting on Google: soon

    threading.Thread(target=loop, daemon=True, name="mail").start()


# --- voice ----------------------------------------------------------------------------------------------------------------
def handle(app, n: str, raw: str) -> str | None:
    if not re.search(r"\b(?:e ?mails?|mails?|inbox|gmail|due|deadlines?|sending)\b", n) and \
            not re.match(r"(?:please )?(?:reply|respond|write back|answer) (?:to .+? )?(?:saying|that|to say|and say|telling)\b", n):
        return None
    if re.search(r"\b(?:set ?up|connect|link|log ?in|sign in|add)\b.*\b(?:e ?mail|gmail)\b", n):
        if not CLIENT.exists():
            return (f"First I need your Google client file: make a Desktop app OAuth client in Google Cloud with the Gmail "
                    f"API on, and send me the JSON. Then say connect my Gmail again.")
        threading.Thread(target=lambda: app.announce(login()), daemon=True).start()
        return ("Opening Google's sign-in on the laptop: pick the account" + (" you haven't connected yet" if ready() else "")
                + " and allow read-only access.")
    if not ready():
        if re.search(r"\b(?:e ?mails?|mails?|inbox|gmail)\b", n):
            return "Your Gmail isn't connected yet. Say set up my email and I'll walk you through it."
        return None
    if re.search(r"\bwhat(?:'s| is| do i have)? (?:due|coming up)|\bdeadlines?\b|anything due", n):
        items = due_items()
        if not items:
            return "Nothing due from your email in the next two weeks."
        return "From your email: " + "; ".join(f"{x['what'] or x['subject']}, {_when(x['due'])}" for x in items[:6]) + "."
    if re.search(r"\b(?:let batsy send|allow sending|send permission)\b", n) or \
            re.search(r"\b(?:enable|turn on)\b.*\bsending\b", n):
        threading.Thread(target=lambda: app.announce(login()), daemon=True).start()
        return "Opening Google's sign-in: pick your account and allow sending email. I'll still ask before every email."
    m = re.match(r"(?:please )?(?:reply|respond|write back|answer)(?: to (?P<who>.+?))? (?:saying|that|to say|and say|"
                 r"telling (?:him|her|them)) (?P<what>.+)$", n)
    if m:
        row = _find(m.group("who")) if m.group("who") and m.group("who") not in ("it", "that", "this", "that email",
                                                                                "the email") else None
        target = row["id"] if row else (_last.get("id") if time.time() - _last.get("t", 0) < 3600 else None)
        if not target:
            return f"Which email? I don't see one from {m.group('who')}." if m.group("who") else "Which email should I reply to?"
        threading.Thread(target=compose_and_send, args=(app, m.group("what"), target), daemon=True, name="mail-send").start()
        return "Writing the reply. I'll read it to you before anything is sent."
    m = re.match(r"(?:please )?(?:send|write|draft)(?: an?)? e ?mail to (?P<who>.+?) (?:saying|that|about|telling (?:him|her|them)) "
                 r"(?P<what>.+)$", n)
    if m:
        who = m.group("who").strip()
        addr = re.search(r"[\w.+-]+ ?(?:@|at) ?[\w-]+(?:\.| dot )[\w.]+", raw or "")
        row = None if addr else _find(who)
        to = (addr.group(0).replace(" at ", "@").replace(" dot ", ".").replace(" ", "") if addr else
              row["sender"] if row else None)
        if row:  # their own address, from the email they sent you
            with _lock:
                db = _db()
                full = db.execute("SELECT id FROM mail WHERE id = ?", (row["id"],)).fetchone()
                db.close()
            hdr = _service(row["account"] or accounts()[0]).users().messages().get(
                userId="me", id=full["id"], format="metadata", metadataHeaders=["From"]).execute()
            to = next((h["value"] for h in hdr["payload"]["headers"] if h["name"] == "From"), to)
        if not to:
            return f"I don't have an email address for {who}. Say it, like: send an email to name at gmail dot com saying …"
        threading.Thread(target=compose_and_send, args=(app, m.group("what"), None, to), daemon=True,
                         name="mail-send").start()
        return "Writing it. I'll read it to you before anything is sent."
    m = re.search(r"\b(?:prepare|write|draft|make)\b.*\b(?:report|draft|assignment|document|reply)\b", n)
    if m:
        target = _last.get("id") if time.time() - _last.get("t", 0) < 3600 else None
        if not target:
            rows = [x for x in recent(72) if x["write"]] or recent(72, True)
            target = rows[0]["id"] if rows else None
        if not target:
            return "Which email? I don't see one asking you to write something."
        claude = bool(re.search(r"\b(?:ask|use|with|using) (?:claude|cloud|clod|lord)\b", n))
        threading.Thread(target=draft, args=(app, target, claude), daemon=True, name="mail-draft").start()
        return "Asking Claude to draft it. I'll open it when it's ready." if claude else "Drafting it now. I'll open it when it's ready."
    m = re.search(r"\b(?:read|open|what(?:'s| is| did))\b.*\b(?:e ?mail|mail|message)\b.*\b(?:from|about|by)\b (.+)", n)
    if m:
        who = m.group(1).strip()
        from rapidfuzz import fuzz

        rows = recent(24 * 14)
        best = max(rows, key=lambda x: fuzz.partial_ratio(who, f"{x['sender']} {x['subject']}".lower()), default=None)
        if not best or fuzz.partial_ratio(who, f"{best['sender']} {best['subject']}".lower()) < 70:
            return f"I don't see an email from or about {who} in the last two weeks."
        _last.update(id=best["id"], t=time.time())
        body = re.sub(r"\s+", " ", best["body"])[:500]
        return f"{best['sender']}, {best['subject']}: {best['summary'] or body}"
    if re.search(r"\b(?:check|any|new|read|what'?s in|anything in)\b.*\b(?:e ?mails?|mails?|inbox|gmail)\b", n):
        try:
            check(app)
        except Exception as e:
            log.warning("mail check: %s", e)
        rows = recent(24, True)
        if not rows:
            return f"Nothing important in the last day. {len(recent(24))} emails, all routine."
        _last.update(id=rows[0]["id"], t=time.time())
        return (f"{len(rows)} important email{'s' if len(rows) != 1 else ''}: "
                + "; ".join(f"{x['sender']}: {x['summary'] or x['subject']}" for x in rows[:4]) + ".")
    return None


def cli(argv: list[str]) -> None:
    """batsy mail login | check | list"""
    cmd = argv[0] if argv else "list"
    if cmd == "login":
        print(login())
    elif cmd == "check":
        from batsy import research
        from batsy.config import Config

        research.configure(Config.load())

        class Quiet:
            def announce(self, t):
                print("Batsy:", t)
        print(check(Quiet()), "new")
    else:
        for r in recent(72):
            print(f"{'!' if r['important'] else ' '} {r['due'] or '':16} {r['sender'][:24]:24} {r['subject'][:60]}")


# --- writing: drafted, read out, sent only after you say yes -------------------------------------------------------------
WRITE = """Write a short email for Jonathan (an engineering student in Chennai) in his own voice: polite, plain, no fluff,
no made-up facts. {task}
Answer in JSON only: {{"subject": "...", "body": "the email text, greeting to sign-off ('Regards,\\nJonathan')"}}"""


def can_send(account: str) -> bool:
    try:
        return SEND in (json.loads((TOKENS / f"{account}.json").read_text()).get("scopes") or [])
    except (OSError, ValueError):
        return False


def _find(who: str) -> sqlite3.Row | None:
    """The newest email from or about `who` (a name, part of an address, or a subject word)."""
    from rapidfuzz import fuzz

    rows = recent(24 * 30)
    best = max(rows, key=lambda x: fuzz.partial_ratio(who.lower(), f"{x['sender']} {x['subject']}".lower()), default=None)
    return best if best and fuzz.partial_ratio(who.lower(), f"{best['sender']} {best['subject']}".lower()) >= 75 else None


def compose_and_send(app, what: str, reply_to: str | None = None, to: str | None = None) -> None:
    """Write it (local AI), read it out, ask "Send it?", and send only on a yes. Runs in the background."""
    import email.utils
    from email.message import EmailMessage

    from batsy import research

    orig = None
    if reply_to:
        with _lock:
            db = _db()
            orig = db.execute("SELECT * FROM mail WHERE id = ?", (reply_to,)).fetchone()
            db.close()
    account = (orig["account"] if orig and orig["account"] else None) or (accounts() or [None])[0]
    if not account:
        app.announce("Your Gmail isn't connected yet.")
        return
    if not can_send(account):
        app.announce(f"I can only read {account} so far. Say let Batsy send email, allow sending in the browser, "
                     f"and ask me again.")
        return
    task = (f"It replies to this email from {orig['sender']} (subject: {orig['subject']}):\n{orig['body'][:2500]}\n\n"
            f"What he wants to say: {what}" if orig else f"It goes to {to}. What he wants to say: {what}")
    try:
        j = json.loads(research._ask(WRITE.format(task=task), 500, json_mode=True))
    except (ValueError, TypeError):
        j = {}
    body = str(j.get("body") or "").strip()
    if not body:
        app.announce("I couldn't write that email. Try saying it again?")
        return
    subject = ("Re: " + re.sub(r"^(?:re|fwd?):\s*", "", orig["subject"], flags=re.I)) if orig else \
        str(j.get("subject") or "Hello").strip()
    svc = _service(account)
    hdrs = {}
    if orig:
        meta = svc.users().messages().get(userId="me", id=orig["id"], format="metadata",
                                          metadataHeaders=["Message-ID", "References", "From", "Reply-To"]).execute()
        hdrs = {h["name"].lower(): h["value"] for h in meta["payload"].get("headers", [])}
        to = hdrs.get("reply-to") or hdrs.get("from")
    name = email.utils.parseaddr(to or "")[0] or email.utils.parseaddr(to or "")[1]
    spoken = re.sub(r"\s+", " ", body)
    ok = app.ask_from_background(f"Email to {name}, subject {subject}: {spoken[:600]}. Send it?")
    if not ok:
        app.announce("Okay, not sent.")
        return
    msg = EmailMessage()
    msg["To"], msg["From"], msg["Subject"] = to, account, subject
    if hdrs.get("message-id"):
        msg["In-Reply-To"] = hdrs["message-id"]
        msg["References"] = (hdrs.get("references", "") + " " + hdrs["message-id"]).strip()
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    payload = {"raw": raw}
    if orig:
        thread = svc.users().messages().get(userId="me", id=orig["id"], format="minimal").execute().get("threadId")
        if thread:
            payload["threadId"] = thread
    svc.users().messages().send(userId="me", body=payload).execute()
    log.info("mail: sent %r to %s from %s", subject, to, account)
    app.announce(f"Sent to {name}.")
