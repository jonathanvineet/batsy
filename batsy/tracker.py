"""Things you're doing that Batsy keeps track of: a hackathon, a course, an application, a competition.

  "batsy keep track, I'm doing the scaler hackathon"   (or "track the …", "I'm doing the …, keep track of it")

  1. its emails, from every inbox: the ones whose sender / subject / text name it
  2. its dates: read out of those emails first; if they don't give a deadline, Claude reads the event's own page
     (from the emails' links) and the web, once, and says the timeline
  3. each date under the clock on its day, a reminder the evening before
  4. its GitHub repo: watched for (a new repo whose name or description matches), then the last push and today's
     commits
  5. new mail about it is always "important" and refreshes the timeline
  6. "how's the hackathon going", "what's left for it", "stop tracking it"; and a Tracking panel in the cockpit
Kept in tracking.json.
"""
import datetime as dt
import json
import logging
import re
import threading
import time
import urllib.request
from pathlib import Path

from batsy.config import DATA_DIR

log = logging.getLogger("batsy.tracker")
FILE = DATA_DIR / "tracking.json"
GITHUB_USER = "jonathanvineet"
_lock = threading.RLock()
START = re.compile(r"\b(?:keep (?:a )?track|track|follow|watch)(?: of)?(?: that)?(?:,)? (?:i'?m |i am )?(?:doing |participating in |"
                   r"taking part in |working on |in |taking )?(?:the |a |my )?(?P<name>.+?)(?: for me)?$|"
                   r"\bi'?m (?:doing|participating in|taking part in|in|taking) (?:the |a )?(?P<name2>.+?)[,.]? "
                   r"(?:so )?(?:keep track|track it|keep (?:an )?eye)")
HEARD = {"scalar": "scaler", "whisper": "wispr", "wisper": "wispr", "wisprflow": "wispr", "whisperflow": "wispr"}
STOP = {"the", "and", "with", "from", "for", "about", "this", "that", "doing", "something", "similar", "keep", "track",
        "hackathons", "event", "thing", "program", "programme", "batsy", "also"}


def _load() -> list[dict]:
    try:
        return json.loads(FILE.read_text())
    except (OSError, ValueError):
        return []


def _save(items: list[dict]) -> None:
    FILE.write_text(json.dumps(items, indent=1))


def active() -> list[dict]:
    return [t for t in _load() if not t.get("stopped")]


def _keywords(name: str) -> list[str]:
    words = [HEARD.get(w, w) for w in re.findall(r"[a-z0-9]{3,}", name.lower())]
    return [w for w in dict.fromkeys(words) if w not in STOP]


def match_mail(row) -> dict | None:
    """The tracked thing this email is about, if any (two of its words, or one rare one, in sender/subject/text)."""
    hay = f"{row['sender']} {row['subject']} {(row['body'] or '')[:3000]}".lower()
    for t in active():
        hits = [k for k in t["keywords"] if k in hay]
        if len(hits) >= 2 or (len(t["keywords"]) == 1 and hits):  # two of its words: "Scaler" alone is their ads
            return t
    return None


# --- starting, and working out its timeline ------------------------------------------------------------------------------
def start(app, name: str, page: dict | None = None) -> str:
    if page:  # from the browser extension: the page itself (its text helps find the dates without Claude)
        name = re.split(r"\s+[|·–—-]\s+", page.get("title") or "")[0] or name
    elif re.fullmatch(r"(?:this|it|this one|this page|this site|this (?:hackathon|event|competition|course|program(?:me)?|"
                    r"contest|challenge))", name.strip(" .,")):
        from batsy import browser  # "keep track of this hackathon": the page you're on

        page = browser.current()
        if not page:
            return "Which one? I can't see a page open in the browser. Say its name."
    if page:
        name = re.split(r"\s+[|·–—-]\s+", page["title"] or "")[0] or page["url"]
    name = " ".join(HEARD.get(w, w) for w in name.strip(" .,").split())
    name = re.sub(r"\b(?:or something(?: similar)?|or so|and stuff)\b", "", name).strip(" ,")
    with _lock:
        items = _load()
        kw = _keywords(name)
        same = next((t for t in items if not t.get("stopped") and set(kw) & set(t["keywords"])), None)
        if same:
            return f"I'm already keeping track of {same['name']}."
        t = {"id": int(time.time()), "name": name[:1].upper() + name[1:], "keywords": kw, "started": time.time(),
             "timeline": [], "repo": None, "summary": "", "sources": [page["url"]] if page else [], "updated": 0,
             "page": re.sub(r"\s+", " ", (page or {}).get("text") or "")[:6000]}
        items.append(t)
        _save(items)
    threading.Thread(target=refresh, args=(app, t["id"], True), daemon=True, name="tracker").start()
    return f"Keeping track of {t['name']}. I'm reading its emails and finding the deadlines now."


def _mails(t: dict) -> list:
    from batsy import mail

    try:
        rows = mail.recent(24 * 60)
    except Exception:
        return []
    out = []
    for r in rows:
        hay = f"{r['sender']} {r['subject']} {(r['body'] or '')[:3000]}".lower()
        hits = [k for k in t["keywords"] if k in hay]
        if len(hits) >= 2 or (len(t["keywords"]) <= 2 and hits and len(hits[0]) >= 5):
            out.append(r)
    return out


def refresh(app, tid: int, first: bool = False) -> None:
    """Read its emails again; dates from them, else (once) from Claude reading its page and the web."""
    from batsy import mail, research

    t = next((x for x in _load() if x["id"] == tid), None)
    if not t:
        return
    rows = _mails(t)
    links = []
    for r in rows:
        for u in re.findall(r"https?://[^\s\"'<>\]\)]+", r["body"] or ""):
            base = re.sub(r"[?#].*$", "", u)  # never a personal invite token
            if base not in links and not re.search(r"unsubscribe|track|click|utm|mailto|google\.com/accounts", base):
                links.append(base)
    text = "\n\n".join(f"[{dt.datetime.fromtimestamp(r['t']):%d %b %Y}] {r['sender']}: {r['subject']}\n"
                       f"{re.sub(r'\s+', ' ', r['body'] or '')[:1200]}" for r in rows[:12])
    try:  # its pages in the browser (a hackathon that's only a website), and the page it was started from
        from batsy import browser

        for p in browser.search(t["keywords"]):
            base = re.sub(r"[?#].*$", "", p["url"])
            if base not in links:
                links.append(base)
    except Exception as e:
        log.debug("browser: %s", e)
    links = [*[u for u in t.get("sources", []) if u not in links], *links]
    if t.get("page"):  # the page it was started from (the extension sends its text): often the only source
        text = f"[its web page] {t['page']}\n\n{text}"
    timeline, summary = [], t.get("summary", "")
    if text:
        try:
            j = json.loads(research._ask(
                f"These emails / pages are about \"{t['name']}\", something Jonathan is taking part in. List every dated step "
                f"(registration, team formation, submission deadline, rounds, results, event days) as JSON: "
                f'{{"summary": "one line: what it is", "timeline": [{{"what": "short", "when": "the email\'s own words '
                f'for the date, with the date if given"}}]}}. Only dates they state.\n\n{text[:6000]}', 500,
                json_mode=True))
            summary = j.get("summary") or summary
            for x in j.get("timeline") or []:
                when = mail.when_to_date(str(x.get("when") or ""))
                if when:
                    timeline.append({"what": str(x.get("what") or "")[:80], "when": when, "source": "email"})
        except (ValueError, TypeError):
            pass
    future = [x for x in timeline if x["when"][:10] >= dt.date.today().isoformat()]
    if not any(re.search(r"submi|deadline|final|demo|close", x["what"], re.I) for x in future) \
            and (first or not t.get("asked_claude")) and (rows or links):
        web = _from_web(app, t, links)
        if web:
            timeline += web.get("timeline", [])
            summary = web.get("summary") or summary
            t["asked_claude"] = time.time()
    # never lose dates: a refresh that only finds the emails' (none) used to wipe the ones Claude had found
    seen, merged = set(), []
    for x in sorted([*t.get("timeline", []), *timeline], key=lambda x: x["when"]):
        key = (x["when"][:10], x["what"].lower()[:20])
        if key not in seen:
            seen.add(key)
            merged.append(x)
    with _lock:
        items = _load()
        cur = next(x for x in items if x["id"] == tid)
        old = {(x["when"][:10], x["what"]) for x in cur.get("timeline", [])}
        cur.update(timeline=merged, summary=summary, sources=links[:5], mails=len(rows), updated=time.time(),
                   asked_claude=t.get("asked_claude") or cur.get("asked_claude"))
        _save(items)
    new = [x for x in merged if (x["when"][:10], x["what"]) not in old and x["when"][:10] >= dt.date.today().isoformat()]
    _schedule(cur, merged)  # (only the key dates, each only once: see _schedule)
    _to_brain(cur)
    nxt = next((x for x in merged if x["when"][:10] >= dt.date.today().isoformat()), None)
    if first:
        app.announce(f"{cur['name']}: " + (f"next is {nxt['what']}, {_when(nxt['when'])}" if nxt else
                                         "I couldn't find its dates yet; I'll keep watching its emails")
                     + (f", and {len(merged) - 1} more dates under the clock." if len(merged) > 1 else "."))
    elif new:
        app.announce(f"New dates for {cur['name']}: " + "; ".join(f"{x['what']}, {_when(x['when'])}" for x in new[:3]))


def _from_web(app, t: dict, links: list[str]) -> dict | None:
    """Claude reads the event's page (and the web) for the official timeline. Once per tracked thing."""
    from batsy import claude_code

    brief = (f"Jonathan is taking part in \"{t['name']}\". Find its official timeline: registration, team formation, "
             f"submission deadline, rounds, demo / finale, results, with dates and times (IST). Start from these pages "
             f"if useful: {', '.join(links[:4]) or '(none)'}; then search the web. Reply with ONLY JSON: "
             f'{{"summary": "one line: what it is and the prize/theme", "timeline": [{{"what": "short", '
             f'"when": "YYYY-MM-DD HH:MM or YYYY-MM-DD"}}]}}. Leave out anything you can\'t confirm.')
    out = claude_code.run(brief, f"tracking: {t['name'][:30]}", timeout=600, cfg=app.cfg,
                          args=["--allowedTools", "WebSearch,WebFetch"])
    if claude_code.limited(out):
        return None
    m = re.search(r"\{.*\}", out or "", re.S)
    try:
        j = json.loads(m.group(0)) if m else {}
    except ValueError:
        return None
    tl = []
    for x in j.get("timeline") or []:
        w = str(x.get("when") or "")
        if re.match(r"\d{4}-\d{2}-\d{2}", w):
            tl.append({"what": str(x.get("what") or "")[:80], "when": w[:16], "source": "web"})
    return {"summary": str(j.get("summary") or "")[:200], "timeline": tl}


KEY = re.compile(r"submi|deadline|due|clos|ends?\b|final|demo|result|shortlist", re.I)


def key_dates(t: dict) -> list[dict]:
    """The dates that matter (deadlines, a round closing, the finale), not every step ("problem statement goes live")."""
    return [x for x in t.get("timeline", []) if KEY.search(x["what"])]


def _schedule(t: dict, items: list[dict]) -> None:
    """The key dates under the clock on their day, a reminder before each; each one only ever once."""
    from batsy.memory_store import get_store
    from batsy.tools import monitor

    done = set(t.get("scheduled", []))
    items = [x for x in items if KEY.search(x["what"]) and f"{x['when']}|{x['what']}" not in done
             and x["when"][:10] >= dt.date.today().isoformat()]
    if not items:
        return
    with _lock:
        all_ = _load()
        for y in all_:
            if y["id"] == t["id"]:
                y["scheduled"] = sorted(done | {f"{x['when']}|{x['what']}" for x in items})
        _save(all_)
    for x in items:
        day = x["when"][:10]
        get_store().add_agenda(day, f"{x['what']} ({t['name']})")
        when = dt.datetime.fromisoformat(x["when"]) if len(x["when"]) > 10 else None
        before = (when - dt.timedelta(hours=1)) if when else dt.datetime.combine(
            dt.date.fromisoformat(day) - dt.timedelta(days=1), dt.time(18, 0))
        if before > dt.datetime.now():
            monitor.add_reminder(before.timestamp(), f"{x['what']} for {t['name']}, {_when(x['when'])}")


def _to_brain(t: dict) -> None:
    """Everything about it in the brain: 02 Projects/Tracking/<name>.md (summary, every date, links, repo)."""
    try:
        from batsy import vault

        p = vault.path("projects") / "Tracking" / f"{vault.safe(t['name'])}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        rows = "\n".join(f"| {x['when'][:10]} | {x['when'][11:16] or ''} | {x['what']} | {x.get('source', '')} |"
                          for x in t.get("timeline", []))
        repo = t.get("repo") or {}
        md = (vault.frontmatter(type="tracking", started=dt.date.fromtimestamp(t["started"]).isoformat(),
                                status="stopped" if t.get("stopped") else "active")
              + f"# {t['name']}\n\n{t.get('summary', '')}\n\n## Dates\n\n| Date | Time | What | From |\n|---|---|---|---|\n"
              + (rows or "| | | none yet | |") + "\n\n## Links\n\n"
              + ("\n".join(f"- {u}" for u in t.get("sources", [])) or "- none") + "\n\n## Repo\n\n"
              + (f"[{repo['name']}]({repo['url']})" if repo else "not created yet") + "\n")
        p.write_text(md)
    except Exception as e:
        log.warning("tracking note: %s", e)


def _when(w: str) -> str:
    from batsy import mail

    return mail._when(w)


# --- GitHub: the repo, once it exists ----------------------------------------------------------------------------------
def _github(path: str):
    req = urllib.request.Request(f"https://api.github.com{path}", headers={"User-Agent": "batsy",
                                                                       "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def check_repos() -> None:
    items = _load()
    changed = False
    try:
        repos = _github(f"/users/{GITHUB_USER}/repos?sort=created&per_page=30")
    except Exception as e:
        log.debug("github: %s", e)
        return
    for t in items:
        if t.get("stopped"):
            continue
        if not t.get("repo"):
            hit = next((r for r in repos if r.get("created_at", "") >= dt.datetime.fromtimestamp(t["started"] - 7 * 86400)
                        .strftime("%Y-%m-%d") and any(k in f"{r['name']} {r.get('description') or ''}".lower()
                                                     for k in t["keywords"])), None)
            if hit:
                t["repo"] = {"name": hit["full_name"], "url": hit["html_url"]}
                changed = True
        if t.get("repo"):
            try:
                r = _github(f"/repos/{t['repo']['name']}")
                since = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0).isoformat()
                commits = _github(f"/repos/{t['repo']['name']}/commits?since={since}&per_page=100")
                t["repo"].update(pushed=r.get("pushed_at"), today=len(commits) if isinstance(commits, list) else 0)
                changed = True
            except Exception as e:
                log.debug("github repo: %s", e)
    if changed:
        with _lock:
            cur = {x["id"]: x for x in _load()}
            for t in items:
                if t["id"] in cur:
                    cur[t["id"]]["repo"] = t.get("repo")
            _save(list(cur.values()))


def start_watching(app) -> None:
    def loop():
        n = 0
        while True:
            try:
                if active():
                    check_repos()
                    if n % 6 == 0:  # every 3 hours: the emails again (new ones also refresh straight away)
                        for t in active():
                            refresh(app, t["id"])
            except Exception:
                log.exception("tracker")
            n += 1
            time.sleep(1800)

    threading.Thread(target=loop, daemon=True, name="tracker-watch").start()


# --- what it shows -------------------------------------------------------------------------------------------------------
def status(t: dict) -> dict:
    today = dt.date.today().isoformat()
    future = [x for x in t.get("timeline", []) if x["when"][:10] >= today]
    nxt = future[0] if future else None
    final = next((x for x in reversed(t.get("timeline", [])) if re.search(r"submi|deadline|final|demo", x["what"], re.I)),
                 (t.get("timeline") or [None])[-1])
    left = None
    if nxt:
        target = dt.datetime.fromisoformat(nxt["when"] if len(nxt["when"]) > 10 else nxt["when"] + "T23:59")
        left = int((target - dt.datetime.now()).total_seconds())
    keys = [x for x in key_dates(t) if x["when"][:10] >= today]
    rnd = keys[0] if keys else None  # the nearest deadline: this round's end
    end = (key_dates(t) or t.get("timeline") or [None])[-1]  # the whole thing's last key date
    if rnd:
        target = dt.datetime.fromisoformat(rnd["when"] if len(rnd["when"]) > 10 else rnd["when"] + "T23:59")
        left = int((target - dt.datetime.now()).total_seconds())
    return {"name": t["name"], "round": rnd, "end": end, "left_s": left if rnd else None, "repo": t.get("repo"),
            "next": nxt, "final": final}


def handle(app, n: str) -> str | None:
    m = START.search(n)
    if m and not re.match(r"(?:how|what|when|is)\b", n):
        name = (m.group("name") or m.group("name2") or "").strip()
        if name and not re.match(r"(?:it|this|that|of it|my (?:email|print|printer))$", name):
            return start(app, name)
    items = active()
    if not items:
        return None
    t = next((x for x in items if any(k in n for k in x["keywords"])), None) or \
        (items[0] if len(items) == 1 and re.search(r"\b(?:hackathon|it|the event|tracking)\b", n) else None)
    if not t:
        return None
    m = re.search(r"\b(?:its |the |my )?(?:repo|repository)(?: for it)? (?:is|called|named) (?P<repo>[\w.-]+)", n)
    if m:  # "the hackathon repo is voice-india": named, when its name doesn't say what it's for
        name = m.group("repo")
        full = name if "/" in name else f"{GITHUB_USER}/{name}"
        with _lock:
            all_ = _load()
            for x in all_:
                if x["id"] == t["id"]:
                    x["repo"] = {"name": full, "url": f"https://github.com/{full}"}
            _save(all_)
        threading.Thread(target=check_repos, daemon=True).start()
        return f"Linked {name} to {t['name']}. I'll show its commits in the cockpit."
    if re.search(r"\b(?:stop|don'?t|no longer) (?:keep(?:ing)? )?track", n):
        with _lock:
            all_ = _load()
            for x in all_:
                if x["id"] == t["id"]:
                    x["stopped"] = time.time()
            _save(all_)
        return f"Stopped keeping track of {t['name']}."
    if re.search(r"\b(?:how'?s|how is|what'?s left|status|update|when'?s|deadline|due)\b", n):
        s = status(t)
        bits = [f"{t['name']}:"]
        if s["next"]:
            bits.append(f"next is {s['next']['what']}, {_when(s['next']['when'])}.")
        if s["final"] and s["final"] is not s["next"]:
            bits.append(f"The {s['final']['what'].lower()} is {_when(s['final']['when'])}.")
        r = s["repo"]
        bits.append(f"Your repo {r['name'].split('/')[-1]} has {r.get('today', 0)} commits today." if r else
                    "I'll link your GitHub repo as soon as you create it.")
        return " ".join(bits)
    return None
