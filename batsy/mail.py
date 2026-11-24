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
TOKEN = CONFIG_DIR / "gmail_token.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
DB = DATA_DIR / "mail.db"
QUERY = "newer_than:3d -category:promotions -category:social -category:forums"
_lock = threading.Lock()
_last: dict = {}  # the email you were last told about ("prepare the report for that")

UNDERSTAND = """You read one email for Jonathan, an engineering student in India. Today is {today}. Answer in JSON only:
{{"kind": "assignment|deadline|exam|meeting|interview|bill|delivery|personal|work|info|newsletter|spam",
  "important": true or false (true only if he should know today: a deadline, a meeting, a teacher, an interview,
               money, something addressed to him personally; not ads, digests or automatic notices),
  "when": "the email's own words for when it's due or happening, copied exactly, e.g. 'this Friday 5 PM',
          'next Monday at 11 AM', '28th November', 'tomorrow' (or "" if nothing is due or scheduled)",
  "what": "the thing to do or attend, 3-8 words, e.g. 'Submit DBMS lab report'" or "",
  "write": true or false (true if he has to write and hand in something: a report, an assignment, a document),
  "summary": "one sentence: who wants what"}}

From: {sender}
Subject: {subject}
Date: {date}

{body}"""


# --- Google -------------------------------------------------------------------------------------------------------------
def ready() -> bool:
    return TOKEN.exists()


def login() -> str:
    """Google's sign-in in the browser (read-only Gmail); the token is kept for next time."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not CLIENT.exists():
        return ("I need your Google OAuth client file first: make a Desktop app client in console.cloud.google.com "
                f"(Gmail API on), download the JSON and save it as {CLIENT}.")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True, prompt="consent",
                                  success_message="Batsy can read your Gmail now. You can close this tab.")
    TOKEN.write_text(creds.to_json())
    TOKEN.chmod(0o600)
    return "Connected: I can read your Gmail now (read-only)."


def _service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        TOKEN.write_text(creds.to_json())
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
    """Fetch and understand new mail; act on what matters. Returns how many new messages there were."""
    from batsy import research

    svc = _service()
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
        due = when_to_date(str(j.get("when") or ""))  # the words, read by Batsy's own date parser (not the model's maths)
        row = (mid, int(m.get("internalDate", "0")) / 1000, sender, hdr.get("subject", ""), body, json.dumps(files),
               str(j.get("kind") or ""), int(bool(j.get("important"))), due, str(j.get("what") or "")[:80],
               int(bool(j.get("write"))), str(j.get("summary") or "")[:300], None)
        with _lock:
            db = _db()
            db.execute("INSERT OR IGNORE INTO mail VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
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
    if r["due"] and r["what"]:
        day = dt.date.fromisoformat(r["due"][:10])
        if day >= dt.date.today():
            get_store().add_agenda(day.isoformat(), f"{r['what']} (email from {r['sender']})")
            eve = dt.datetime.combine(day - dt.timedelta(days=1), dt.time(18, 0))
            if eve > dt.datetime.now():
                monitor.add_reminder(eve.timestamp(), f"{r['what']}, due {_when(r['due'])} (from {r['sender']})")
            said.append(f"I've put it under the clock for {_when(r['due'])}")
    if r["important"] or r["due"]:
        _last.update(id=mid, t=time.time())
        app.announce(f"New email from {r['sender']}: {r['summary'] or r['subject']}" + (". " + ", ".join(said) + "."
                                                                                       if said else ""))
    if r["write"]:
        threading.Thread(target=draft, args=(app, mid, False), daemon=True, name="mail-draft").start()
    with _lock:
        db = _db()
        db.execute("UPDATE mail SET acted = ? WHERE id = ?", (time.time(), mid))
        db.commit()
        db.close()


def when_to_date(words: str) -> str:
    """'this Friday 5 PM' -> '2026-11-27 17:00', '28th November' -> '2026-11-28', '' -> ''. The reminders' parser."""
    from batsy import instant

    w = re.sub(r"\b(?:this|by|before|on or before|no later than|until|due)\b", " ", words.lower())
    w = re.sub(r"[^a-z0-9: ]", " ", re.sub(r"(\d)\.(\d\d)", r"\1:\2", w))
    w = re.sub(r"\b(\d{1,2}) ?(?:p\.?m|pm)\b", r"\1 pm", re.sub(r"\b(\d{1,2}) ?(?:a\.?m|am)\b", r"\1 am", w))
    w = re.sub(r"\s+", " ", w).strip()
    if not w:
        return ""
    clock = re.search(r"\d{1,2}(?::\d{2})? ?(?:am|pm)|\d{1,2}:\d{2}", w)
    day, _ = instant._day_in(re.sub(r"\bnext\b", "", w).strip())  # a weekday: its next one
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
            if ready():
                try:
                    check(app)
                except Exception as e:
                    log.warning("mail check: %s", e)
            time.sleep(600)

    threading.Thread(target=loop, daemon=True, name="mail").start()


# --- voice ----------------------------------------------------------------------------------------------------------------
def handle(app, n: str, raw: str) -> str | None:
    if not re.search(r"\b(?:e ?mails?|mails?|inbox|gmail|due|deadlines?)\b", n):
        return None
    if re.search(r"\b(?:set ?up|connect|link|log ?in|sign in)\b.*\b(?:e ?mail|gmail)\b", n):
        if not CLIENT.exists():
            return (f"First I need your Google client file: make a Desktop app OAuth client in Google Cloud with the Gmail "
                    f"API on, and send me the JSON. Then say connect my Gmail again.")
        threading.Thread(target=lambda: app.announce(login()), daemon=True).start()
        return "Opening Google's sign-in in the browser: allow read-only access to your Gmail."
    if not ready():
        if re.search(r"\b(?:e ?mails?|mails?|inbox|gmail)\b", n):
            return "Your Gmail isn't connected yet. Say set up my email and I'll walk you through it."
        return None
    if re.search(r"\bwhat(?:'s| is| do i have)? (?:due|coming up)|\bdeadlines?\b|anything due", n):
        items = due_items()
        if not items:
            return "Nothing due from your email in the next two weeks."
        return "From your email: " + "; ".join(f"{x['what'] or x['subject']}, {_when(x['due'])}" for x in items[:6]) + "."
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
