"""Batsy on the web, over Tailscale only: http://<this laptop>:8765/web

Nobody signs in. Tailscale tells Batsy who each visitor is (`tailscale whois`), and that decides what they get:
- You (the laptop's own Tailscale login, or the paired phone app): everything, plus the Remote panel (Batsy's
  service, live log, doctor, restart/update, Claude Remote Control, and yes/no questions for risky actions when
  you're not at the laptop).
- Friends: the people in [files] senders (Tailscale login -> name; the same list that may send you files). Each
  gets their own space: a chat with Batsy, their own research with a map of it, their own reminders (sent to
  their browser), the files they've sent you, and the printer's status. Their chats and research run in Claude
  with web search only: no shell, no files, nothing on your machines, none of your memories or conversations.
- Anyone else on the tailnet: a page saying to ask you to be added.

Data: web.db (chats, reminders, research) and web/<person>/ in Batsy's data folder.
"""
import datetime as dt
import ipaddress
import json
import logging
import re
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from batsy.config import DATA_DIR, SOURCE_DIR

log = logging.getLogger("batsy.web")
UI = SOURCE_DIR / "web_ui"
DB = DATA_DIR / "web.db"
TAILNET = [ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48")]
_lock = threading.Lock()


# --- who's this -------------------------------------------------------------------------------------------------------
class Person:
    def __init__(self, login: str, name: str, owner: bool):
        self.login, self.name, self.owner = login, name, owner
        self.key = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "someone"

    def public(self) -> dict:
        return {"name": self.name, "owner": self.owner, "login": self.login}


_who: dict[str, tuple[float, Person | None]] = {}


def person(app, ip: str, has_token: bool, serve_login: str = "") -> Person | None:
    """The visitor: by the phone app's token (you), else by their Tailscale identity: their tailnet address, or,
    through `tailscale serve` (https://<laptop>.<tailnet>.ts.net, which reaches Batsy from 127.0.0.1), the
    Tailscale-User-Login header tailscaled adds. None = not allowed."""
    owner_name = app.cfg.get("user.name") or "Jonathan"
    if has_token:
        return Person("", owner_name, True)
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if addr.is_loopback and serve_login:
        return _by_login(app, serve_login.lower())
    if not any(addr in net for net in TAILNET):
        return None  # only over Tailscale: never the LAN or anything else
    hit = _who.get(ip)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    from batsy import inbox

    w = inbox.whois(ip)
    p = _by_login(app, w[0]) if w else None
    _who[ip] = (time.time(), p)
    return p


def _by_login(app, login: str) -> Person | None:
    from batsy import inbox

    owner_name = app.cfg.get("user.name") or "Jonathan"
    friends = {k.lower(): v for k, v in (app.cfg.get("files.senders") or {}).items()}
    if login == inbox.my_login():
        return Person(login, owner_name, True)
    if login in friends:
        return Person(login, friends[login], False)
    return None


def stranger_login(ip: str) -> str:
    from batsy import inbox

    w = inbox.whois(ip)
    return w[0] if w else ""


# --- storage ----------------------------------------------------------------------------------------------------------
def _db() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB), timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE IF NOT EXISTS chats (id INTEGER PRIMARY KEY, who TEXT, t REAL, role TEXT, text TEXT);
        CREATE INDEX IF NOT EXISTS chats_who ON chats(who, id);
        CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY, who TEXT, due REAL, text TEXT,
            fired INTEGER DEFAULT 0, created REAL);
        CREATE TABLE IF NOT EXISTS research (id INTEGER PRIMARY KEY, who TEXT, t REAL, topic TEXT, status TEXT,
            note TEXT, data TEXT, done REAL);
    """)
    return c


def _q(sql: str, args=(), one=False):
    with _lock, _db() as c:
        cur = c.execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()]
        return (rows[0] if rows else None) if one else rows


def _exec(sql: str, args=()) -> int:
    with _lock, _db() as c:
        return c.execute(sql, args).lastrowid


def _say(who: str, role: str, text: str) -> None:
    _exec("INSERT INTO chats (who, t, role, text) VALUES (?,?,?,?)", (who, time.time(), role, text))


# --- the friends' sandbox: Claude with web search, and nothing else --------------------------------------------------
FRIEND_BRIEF = """You are Batsy, {owner}'s assistant, chatting on a web page with {owner}'s friend {name}. You can
search the web. You have no access to {owner}'s computers, files, messages, calendar or memories, and you don't
know anything private about {owner}: if asked, say so kindly. Be friendly and direct; answer what was asked.
Markdown is fine. {name} can also start a research ("research <topic>") and set reminders ("remind me at 5 to ...")
on this page, which Batsy handles itself.

The conversation so far (oldest first):
{history}

{name}: {text}"""

RESEARCH_BRIEF = """Research this thoroughly for {name}: "{topic}"

Use web search and read the best sources (aim for 6-12 good ones: papers, documentation, reputable articles).
Then reply with ONLY a JSON object, no prose before or after:
{{"title": "short title",
  "summary": "a clear overview, 2-4 paragraphs, plain text",
  "findings": [{{"text": "one key finding", "sources": [0, 2]}}],
  "open_questions": ["..."],
  "next_steps": ["..."],
  "sources": [{{"title": "...", "url": "https://...", "kind": "paper|article|docs|video|other", "note": "why it matters"}}],
  "map": {{"nodes": [{{"id": "short-id", "label": "Concept", "kind": "core|concept|method|tool|question|source", "weight": 1}}],
          "edges": [{{"a": "id", "b": "id", "label": "relation, 1-3 words"}}]}}}}
The map is a concept map of the topic: 12-30 nodes, the topic itself as the one "core" node, the main ideas, methods
and tools around it, open questions, and the key sources linked to what they support. weight 1-3 = importance."""


def _claude(prompt: str, label: str, tools: str, timeout: int) -> str:
    from batsy import claude_code

    work = Path(tempfile.mkdtemp(prefix="batsy-web-"))  # an empty folder: nothing of yours to read even by accident
    none = work / "no-mcp.json"
    none.write_text('{"mcpServers": {}}')
    # Only the tools named: no shell or files (--tools), and no MCP servers at all, including your claude.ai
    # connectors (Gmail, Drive...): --strict-mcp-config with an empty list. Anything else is refused (dontAsk).
    return claude_code.run(prompt, label, cwd=work, timeout=timeout,
                           args=["--tools", tools, "--allowedTools", tools, "--permission-mode", "dontAsk",
                                 "--strict-mcp-config", "--mcp-config", str(none)])


def _over_daily(app, p: Person) -> bool:
    if p.owner:
        return False
    since = time.time() - 86400
    n = _q("SELECT COUNT(*) AS n FROM chats WHERE who = ? AND role = 'you' AND t > ?", (p.key, since), one=True)["n"]
    n += _q("SELECT COUNT(*) AS n FROM research WHERE who = ? AND t > ?", (p.key, since), one=True)["n"] * 5
    return n >= int(app.cfg.get("web.friend_daily"))


# --- chat ---------------------------------------------------------------------------------------------------------------
RESEARCH_ASK = re.compile(r"^(?:please |can you |could you )?(?:start (?:a |an |some )?)?(?:new )?research(?:ing)?"
                          r"(?: on| about| into| for)?\s+(?P<topic>.{3,})$", re.I)
REMIND_ASK = re.compile(r"^(?:please |can you )?remind me (?P<rest>.+)$", re.I)


def chat(app, p: Person, text: str) -> dict:
    text = text.strip()[:4000]
    if not text:
        return {"say": ""}
    _say(p.key, "you", text)
    reply = _local(app, p, text)
    if reply is None:
        if p.owner:  # you: the real Batsy, with everything it can do
            web_turn.active = True
            try:
                reply = app.hub.batsy_action({"action": "ask", "text": text}).get("say", "")
            finally:
                web_turn.active = False
        elif _over_daily(app, p):
            reply = "That's my limit for today. Try again tomorrow!"
        else:
            hist = _q("SELECT role, text FROM chats WHERE who = ? ORDER BY id DESC LIMIT 13", (p.key,))[::-1][:-1]
            lines = "\n".join(f"{p.name if h['role'] == 'you' else 'Batsy'}: {h['text'][:600]}" for h in hist) or "(new)"
            reply = _claude(FRIEND_BRIEF.format(owner=app.cfg.get("user.name") or "Jonathan", name=p.name,
                                                history=lines, text=text),
                            f"web chat: {p.name}", "WebSearch,WebFetch", 240)
    _say(p.key, "batsy", reply)
    return {"say": reply}


def _local(app, p: Person, text: str) -> str | None:
    """Research and reminders are handled here for everyone, without the AI."""
    m = RESEARCH_ASK.match(text)
    if m:
        r = start_research(app, p, m.group("topic").strip(" .?!"))
        return r.get("error") or f"Started researching “{r['topic']}”. It takes a few minutes; open Research to watch."
    m = REMIND_ASK.match(text)
    if m:
        r = add_reminder(p, m.group("rest"))
        return r.get("error") or f"Okay, I'll remind you {r['when']}."
    return None


class _WebTurn(threading.local):
    active = False


web_turn = _WebTurn()


def history(p: Person, limit: int = 80) -> list[dict]:
    return _q("SELECT id, t, role, text FROM chats WHERE who = ? ORDER BY id DESC LIMIT ?", (p.key, limit))[::-1]


# --- reminders ------------------------------------------------------------------------------------------------------------
def add_reminder(p: Person, rest: str, due: float | None = None) -> dict:
    from batsy import instant

    what = rest.strip()
    if due is None:
        rel = re.fullmatch(r"in (.+?) (?:to|about|that) (.+)", rest, re.I) or \
            re.fullmatch(r"(?:to |about )?(.+?) in (.+)", rest, re.I)
        if rel:
            when_s, what = (rel.group(1), rel.group(2)) if rest.lower().startswith("in ") else (rel.group(2), rel.group(1))
            secs = instant._duration(when_s.lower())
            due = time.time() + secs if secs else None
        if due is None:
            found = instant._when(rest.lower())
            if not found:
                return {"error": "I couldn't tell when. Try “remind me at 6 pm to …” or “in 20 minutes to …”."}
            when, spans = found
            low = rest.lower()
            for a, b in sorted(spans, reverse=True):
                low = low[:a] + " " + low[b:]
            what = re.sub(r"^\s*(?:to|about|that)\s+|\s+(?:to|at|on|by)\s*$", "", re.sub(r"\s+", " ", low)).strip()
            due = when.timestamp()
    what = re.sub(r"^(?:to|about)\s+", "", what).strip() or "your reminder"
    rid = _exec("INSERT INTO reminders (who, due, text, created) VALUES (?,?,?,?)", (p.key, due, what, time.time()))
    return {"id": rid, "text": what, "due": due, "when": _when_text(due) + f": {what}"}


def _when_text(due: float) -> str:
    d = dt.datetime.fromtimestamp(due)
    days = (d.date() - dt.date.today()).days
    t = d.strftime("%-I:%M %p").replace(":00", "")
    return f"at {t}" if days == 0 else f"tomorrow at {t}" if days == 1 else f"on {d.strftime('%a %-d %b')} at {t}"


def reminders(p: Person) -> list[dict]:
    rows = _q("SELECT id, due, text, fired FROM reminders WHERE who = ? AND (fired = 0 OR due > ?) ORDER BY due",
              (p.key, time.time() - 86400))
    for r in rows:
        r["when"] = _when_text(r["due"])
    return rows


def delete_reminder(p: Person, rid: int) -> None:
    _exec("DELETE FROM reminders WHERE id = ? AND who = ?", (rid, p.key))


def _reminder_loop(app) -> None:
    """Friends' reminders go to their own browsers (web push); yours also go through Batsy as usual."""
    from batsy import push

    owner_key = Person("", app.cfg.get("user.name") or "Jonathan", True).key
    while True:
        try:
            for r in _q("SELECT * FROM reminders WHERE fired = 0 AND due <= ?", (time.time(),)):
                _exec("UPDATE reminders SET fired = 1 WHERE id = ?", (r["id"],))
                if r["who"] == owner_key:
                    app.announce(f"Reminder: {r['text']}.")
                else:
                    push.notify("Reminder", r["text"], tag=f"rem-{r['id']}", url="/web/#reminders", user=r["who"])
        except Exception:
            log.exception("web reminders")
        time.sleep(15)


# --- research -------------------------------------------------------------------------------------------------------------
_running: set[int] = set()


def start_research(app, p: Person, topic: str) -> dict:
    topic = topic.strip()[:300]
    if len(topic) < 3:
        return {"error": "What should I research?"}
    if _over_daily(app, p):
        return {"error": "That's the research limit for today."}
    if len(_running) >= int(app.cfg.get("web.max_research")):
        return {"error": "I'm already researching a few things. Try again in a few minutes."}
    rid = _exec("INSERT INTO research (who, t, topic, status, note) VALUES (?,?,?,?,?)",
                (p.key, time.time(), topic, "running", "Searching the web…"))
    _running.add(rid)
    threading.Thread(target=_research, args=(rid, p, topic), daemon=True, name=f"research-{rid}").start()
    return {"id": rid, "topic": topic}


def _research(rid: int, p: Person, topic: str) -> None:
    try:
        reply = _claude(RESEARCH_BRIEF.format(name=p.name, topic=topic), f"web research: {topic[:30]}",
                        "WebSearch,WebFetch", 1200)
        m = re.search(r"\{.*\}", reply, re.S)
        data = json.loads(m.group(0)) if m else None
        if not isinstance(data, dict) or not data.get("summary"):
            raise ValueError(reply[:300] or "no answer")
        _exec("UPDATE research SET status = 'done', note = '', data = ?, done = ? WHERE id = ?",
              (json.dumps(data), time.time(), rid))
        from batsy import push

        push.notify("Research ready", data.get("title") or topic, tag=f"research-{rid}", url=f"/web/#research/{rid}",
                    user=None if p.owner else p.key)
    except Exception as e:
        log.warning("research %s failed: %s", rid, e)
        _exec("UPDATE research SET status = 'failed', note = ?, done = ? WHERE id = ?",
              (f"It didn't work out: {str(e)[:200]}", time.time(), rid))
    finally:
        _running.discard(rid)


def research_list(p: Person) -> list[dict]:
    rows = _q("SELECT id, t, topic, status, note, done, data FROM research WHERE who = ? ORDER BY id DESC LIMIT 50",
              (p.key,))
    for r in rows:
        d = json.loads(r.pop("data") or "{}")
        r["title"] = d.get("title") or r["topic"]
        r["sources"] = len(d.get("sources") or [])
    return rows


def research_get(p: Person, rid: int) -> dict | None:
    r = _q("SELECT * FROM research WHERE id = ? AND who = ?", (rid, p.key), one=True)
    if r:
        r["data"] = json.loads(r["data"] or "{}")
    return r


def research_delete(p: Person, rid: int) -> None:
    _exec("DELETE FROM research WHERE id = ? AND who = ?", (rid, p.key))


# --- files ------------------------------------------------------------------------------------------------------------------
def files(app, p: Person) -> list[dict]:
    from batsy import received

    rows = received.all_rows(sender=None if p.owner else p.name, limit=200)
    return [{"id": r["id"], "name": Path(r["path"]).name, "sender": r.get("sender"), "kind": r.get("kind"),
             "size": r.get("size"), "t": r.get("received"), "missing": r.get("missing", False)} for r in rows]


def file_path(app, p: Person, fid: int) -> Path | None:
    from batsy import received

    for r in received.all_rows(sender=None if p.owner else p.name, limit=2000):
        if r["id"] == fid and not r.get("missing"):
            return Path(r["path"])
    return None


# --- live: what's happening ---------------------------------------------------------------------------------------------
def live(app, p: Person) -> dict:
    s = app.hub.status()
    b = s.get("batsy") or {}
    out = {"t": s["t"], "batsy": {"state": b.get("state", "idle"),
                                  "busy": [a for a in (b.get("agents") or []) if p.owner or not a.startswith("ask")]},
           "printer": s.get("printer"), "machines": s.get("machines")}
    if p.owner:  # yours only: what you said and what it answered, Spotify, Claude, questions waiting
        out["batsy"].update(status=b.get("status", ""), you=b.get("you", ""), said=b.get("said", ""),
                            asks=b.get("asks", []), next_reminder=b.get("next_reminder"))
        out["spotify"], out["claude"] = s.get("spotify"), s.get("claude")
        out["confirm"] = CONFIRMS.pending()
    else:
        out["batsy"]["busy"] = ["working on something"] if b.get("agents") else []
    return out


# --- yes/no questions for risky actions, answered on the website when you're not at the laptop -------------------------
class Confirms:
    def __init__(self):
        self._items: dict[int, dict] = {}
        self._n = 0
        self.owner_seen = 0.0  # last time your page asked for anything: you're watching

    def pending(self) -> list[dict]:
        return [{"id": i, "question": c["question"], "t": c["t"]} for i, c in self._items.items() if c["answer"] is None]

    def ask(self, question: str, timeout: float = 180) -> bool:
        from batsy import push

        self._n += 1
        cid = self._n
        item = {"question": question, "t": time.time(), "answer": None, "done": threading.Event()}
        self._items[cid] = item
        push.notify("Batsy needs a yes or no", question, tag=f"confirm-{cid}", url="/web/#remote")
        item["done"].wait(timeout)
        self._items.pop(cid, None)
        return bool(item["answer"])

    def answer(self, cid: int, yes: bool) -> bool:
        item = self._items.get(cid)
        if not item:
            return False
        item["answer"] = bool(yes)
        item["done"].set()
        return True


CONFIRMS = Confirms()


def remote_confirm_wanted(app) -> bool:
    """Ask on the website instead of out loud: the request came from the website, or you're away from the laptop
    and your page is open."""
    if web_turn.active:
        return True
    if time.time() - CONFIRMS.owner_seen > 90:
        return False
    from batsy import idle

    away = idle.idle_ms()
    return away is None or away > 120_000


# --- the Remote panel -----------------------------------------------------------------------------------------------------
def remote_status(app) -> dict:
    from batsy import remote

    st = remote.status()
    st["uptime_s"] = int(time.time() - getattr(app, "started_at", time.time()))
    st["pending"] = CONFIRMS.pending()
    return st


def remote_action(app, body: dict) -> dict:
    from batsy import remote

    a = body.get("action")
    if a == "confirm":
        ok = CONFIRMS.answer(int(body.get("id", 0)), bool(body.get("yes")))
        return {"ok": ok, "say": ("Yes, go ahead." if body.get("yes") else "No.") if ok else "That question expired."}
    if a == "log":
        return {"ok": True, "text": remote.log_tail(int(body.get("lines", 150)))}
    if a == "doctor":
        return {"ok": True, "text": remote.doctor()}
    if a in ("rc_start", "rc_stop", "rc_restart"):
        return {"ok": True, "say": remote.rc(a.removeprefix("rc_"))}
    if a in ("restart", "update"):
        def later():
            time.sleep(1.5)  # let the page get its answer first
            if a == "update":
                app._self_command("update yourself")
            else:
                app.restart()

        threading.Thread(target=later, daemon=True).start()
        return {"ok": True, "say": "Updating and restarting…" if a == "update" else "Restarting…"}
    if a == "stop":
        app.end_conversation()
        return {"ok": True, "say": "Stopped."}
    return {"ok": False, "say": f"Unknown action {a}"}


def start(app) -> None:
    threading.Thread(target=_reminder_loop, args=(app,), daemon=True, name="web-reminders").start()
    with _lock, _db():
        pass
    # research left "running" by a restart can't finish any more
    _exec("UPDATE research SET status = 'failed', note = 'Batsy restarted while this was running: start it again.' "
          "WHERE status = 'running'")


# --- HTTP (called from the hub's handler in batsy/phone.py) ----------------------------------------------------------------
def serve(h, app, method: str) -> bool:
    """Handle /web and /api/web/... on the hub's server. True if it was ours."""
    import secrets as _secrets
    from urllib.parse import parse_qs, urlparse

    from batsy import phone

    url = urlparse(h.path)
    path = url.path
    if not (path == "/web" or path.startswith("/web/") or path.startswith("/api/web/")):
        return False
    if path == "/web":
        h.send_response(302)
        h.send_header("Location", "/web/")
        h.end_headers()
        return True
    if path.startswith("/web/"):  # the page itself: no data in it
        rel = path.removeprefix("/web/") or "index.html"
        f = (UI / rel).resolve()
        if UI.resolve() not in f.parents or not f.is_file():
            h._send(404, {"error": "no such page"})
            return True
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
                 ".svg": "image/svg+xml", ".json": "application/json", ".png": "image/png",
                 ".webmanifest": "application/manifest+json", ".woff2": "font/woff2"}.get(f.suffix, "application/octet-stream")
        h._send(200, body=f.read_bytes(), ctype=ctype)
        return True

    got = h.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    serve_login = h.headers.get("Tailscale-User-Login", "")
    p = person(app, h.client_address[0], bool(got) and _secrets.compare_digest(got, phone.token()), serve_login)
    if p is None:
        who = serve_login or stranger_login(h.client_address[0])
        h._send(403, {"error": "not allowed", "login": who,
                      "say": (f"You're {who} on Tailscale. Ask {app.cfg.get('user.name') or 'Jonathan'} to add you."
                              if who else "Open this over Tailscale.")})
        return True
    if p.owner:
        CONFIRMS.owner_seen = time.time()
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    body = h._body() if method == "POST" else {}
    owner_only = path.startswith("/api/web/remote")
    if owner_only and not p.owner:
        h._send(403, {"error": "only for " + (app.cfg.get("user.name") or "Jonathan")})
        return True
    try:
        r = _route(app, p, method, path, q, body, h)
    except Exception as e:
        log.exception("web %s %s", method, path)
        r = {"error": str(e)[:300]}
    if r is not None:
        h._send(200 if "error" not in r else 400, r)
    return True


def _route(app, p: Person, method: str, path: str, q: dict, body: dict, h):
    from batsy import push

    if path == "/api/web/me":
        return {**p.public(), "push_key": _safe(push.public_key)}
    if path == "/api/web/live":
        return live(app, p)
    if path == "/api/web/chat":
        return chat(app, p, body.get("text", "")) if method == "POST" else {"messages": history(p)}
    if path == "/api/web/reminders":
        if method == "POST":
            if body.get("action") == "delete":
                delete_reminder(p, int(body.get("id", 0)))
                return {"ok": True}
            return add_reminder(p, str(body.get("text", "")))
        return {"reminders": reminders(p)}
    if path == "/api/web/research":
        if method == "POST":
            if body.get("action") == "delete":
                research_delete(p, int(body.get("id", 0)))
                return {"ok": True}
            return start_research(app, p, str(body.get("topic", "")))
        if q.get("id"):
            return research_get(p, int(q["id"])) or {"error": "not found"}
        return {"research": research_list(p)}
    if path == "/api/web/files":
        return {"files": files(app, p)}
    if path == "/api/web/files/raw":
        f = file_path(app, p, int(q.get("id", 0)))
        if f is None or not f.exists():
            return {"error": "not found"}
        h.send_response(200)
        h.send_header("Content-Type", "application/octet-stream")
        h.send_header("Content-Disposition", f'attachment; filename="{f.name}"')
        h.send_header("Content-Length", str(f.stat().st_size))
        h.end_headers()
        with open(f, "rb") as fh:
            while chunk := fh.read(1 << 16):
                h.wfile.write(chunk)
        return None
    if path == "/api/web/push":
        push.subscribe(body.get("sub") or {}, f"web: {p.name}", user="" if p.owner else p.key)
        return {"ok": True}
    if path == "/api/web/remote":
        return remote_action(app, body) if method == "POST" else remote_status(app)
    return {"error": "no such thing"}


def _safe(fn):
    try:
        return fn()
    except Exception:
        return ""
