"""What you're doing in the browser: Brave's own history on this laptop (page titles and addresses only, never
passwords, cookies or what's on the pages), read-only and never sent anywhere by itself.

  current()            the page you're on (the newest visit)
  recent(minutes)      what you've been looking at, newest first, one row per page
  search(words, days)  pages whose title or address has those words ("scaler", "wispr")
  handle()             "what am I doing", "what was I looking at"

tracker.py uses it: "keep track of this hackathon" takes the page you're on; anything tracked also gathers its pages.
"""
import datetime as dt
import re
import sqlite3
import time
from pathlib import Path

HISTORY = [Path.home() / ".config/BraveSoftware/Brave-Browser/Default/History",
           Path.home() / ".config/google-chrome/Default/History", Path.home() / ".config/chromium/Default/History"]
SKIP = re.compile(r"^(?:chrome|brave|about|chrome-extension|file|data|blob|edge):|//(?:localhost|127\.0\.0\.1|accounts\.google|"
                  r"login\.|auth\.)|/(?:login|signin|oauth|callback)\b", re.I)
EPOCH = 11644473600  # Chromium counts microseconds from 1601


def _db() -> sqlite3.Connection | None:
    f = next((p for p in HISTORY if p.exists()), None)
    if not f:
        return None
    # immutable: read it while Brave holds it open, without copying or locking anything
    return sqlite3.connect(f"file:{f}?immutable=1", uri=True, timeout=5)


def _rows(sql: str, args=()) -> list[dict]:
    db = _db()
    if db is None:
        return []
    try:
        out = []
        for url, title, t in db.execute(sql, args):
            if SKIP.search(url or ""):
                continue
            out.append({"url": url, "title": (title or "").strip(), "t": t / 1e6 - EPOCH})
        return out
    except sqlite3.Error:
        return []
    finally:
        db.close()


def recent(minutes: float = 60, limit: int = 40) -> list[dict]:
    """Newest first: the extension's tabs (every computer it's on) and this laptop's Brave history, one row a page."""
    since = int((time.time() - minutes * 60 + EPOCH) * 1e6)
    rows = _rows("SELECT u.url, u.title, MAX(v.visit_time) FROM visits v JOIN urls u ON u.id = v.url "
                 "WHERE v.visit_time > ? GROUP BY u.id ORDER BY MAX(v.visit_time) DESC LIMIT ?", (since, limit * 2))
    seen, out = set(), []
    for r in sorted([*_extension_rows(time.time() - minutes * 60), *rows], key=lambda r: -r["t"]):
        if r["url"] not in seen:
            seen.add(r["url"])
            out.append(r)
    return out[:limit]


def current() -> dict | None:
    r = recent(30, 3)
    return r[0] if r else None


def search(words: list[str], days: float = 30, limit: int = 15) -> list[dict]:
    since = int((time.time() - days * 86400 + EPOCH) * 1e6)
    rows = _rows("SELECT u.url, u.title, u.last_visit_time FROM urls u WHERE u.last_visit_time > ? "
                 "ORDER BY u.last_visit_time DESC LIMIT 4000", (since,))
    rows = [*_extension_rows(time.time() - days * 86400)[::-1], *rows]
    out = []
    for r in rows:
        hay = f"{r['title']} {r['url']}".lower()
        if sum(1 for w in words if w in hay) >= min(2, len(words)):
            out.append(r)
    return out[:limit]


def _site(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/]+)", url or "")
    return m.group(1) if m else url


def handle(app, n: str) -> str | None:
    if not re.search(r"\bwhat (?:am i|was i|have i been) (?:doing|looking at|reading|browsing|working on)\b|"
                     r"\bwhat'?s (?:open )?(?:in|on) (?:my )?(?:browser|brave)\b", n):
        return None
    rows = recent(90, 30)
    if not rows:
        return "Nothing in the browser in the last hour and a half."
    sites: dict[str, list[str]] = {}
    for r in rows:
        sites.setdefault(_site(r["url"]), []).append(r["title"] or r["url"])
    top = sorted(sites.items(), key=lambda x: -len(x[1]))[:4]
    now = rows[0]
    bits = [f"Right now: {now['title'] or _site(now['url'])}."]
    others = [f"{site} ({titles[0][:50]})" for site, titles in top if site != _site(now["url"])][:3]
    if others:
        bits.append("Before that: " + "; ".join(others) + ".")
    return " ".join(bits)


# --- the Batsy browser extension (browser-extension/): every computer's active tab, and the page when you ask ---------
VISITS = Path.home() / ".local/share/batsy/browser_visits.jsonl"


def add_visit(url: str, title: str, device: str) -> None:
    """The tab you switched to (title + address only), from the extension on any of your computers."""
    import json

    if not url or SKIP.search(url):
        return
    row = {"url": url[:500], "title": (title or "")[:200], "t": time.time(), "device": (device or "")[:40]}
    try:
        lines = VISITS.read_text().splitlines()[-1999:] if VISITS.exists() else []
        if lines and json.loads(lines[-1]).get("url") == row["url"]:
            return  # the same page again (a reload)
        VISITS.write_text("\n".join([*lines, json.dumps(row)]) + "\n")
    except (OSError, ValueError):
        pass


def _extension_rows(since: float) -> list[dict]:
    import json

    try:
        rows = [json.loads(x) for x in VISITS.read_text().splitlines() if x.strip()]
    except (OSError, ValueError):
        return []
    return [r for r in rows if r.get("t", 0) >= since]


def _now(prompt: str, max_tokens: int = 300, json_mode: bool = False) -> str:
    """The local AI for a button you just pressed: straight away, ahead of the background jobs (mail backlog)."""
    import json

    from batsy import llm_hosts
    from batsy.config import Config

    cfg = Config.load()
    body = {"model": cfg.get("local.model"), "keep_alive": cfg.get("local.keep_alive"), "stream": False,
            "messages": [{"role": "user", "content": prompt}],
            "options": {"temperature": 0.2, "num_predict": max_tokens, "num_ctx": 4096}, **({"format": "json"} if json_mode else {})}
    with llm_hosts.live(), llm_hosts.open_chat(cfg, body, 120) as r:
        return json.loads(r.read()).get("message", {}).get("content", "").strip()


def action(app, b: dict) -> dict:
    """The extension's buttons: track / ask / save, on the page you're on."""
    url, title, text = str(b.get("url") or ""), str(b.get("title") or ""), str(b.get("text") or "")[:20000]
    a = b.get("action")
    if a == "track":
        from batsy import tracker

        return {"say": tracker.start(app, title or url, page={"url": url, "title": title, "text": text})}
    if a == "ask":
        q = str(b.get("question") or "What is this page about?")
        out = _now(f"Jonathan is looking at this web page and asks: {q}\nAnswer in 2-4 short sentences, from the "
                            f"page only.\n\nTITLE: {title}\nURL: {url}\n\n{text[:7000]}", 300)
        return {"say": (out or "I couldn't read that page.").strip()}
    if a == "save":
        from batsy import vault

        summary = _now(f"Summarise this web page for Jonathan's notes in 3-5 bullet points ('- '). Page only.\n\n"
                                f"TITLE: {title}\nURL: {url}\n\n{text[:7000]}", 300) if text else ""
        p = vault.path("inbox") / "Web" / f"{vault.safe(title or url)[:80]}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(vault.frontmatter(type="web", url=url, saved=dt.date.today().isoformat())
                     + f"# {title or url}\n\n<{url}>\n\n{(summary or '').strip()}\n")
        return {"say": f"Saved {title[:60] or 'the page'} to your brain."}
    return {"say": "Unknown action."}
