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


# words every event's title has: alone they'd match half the inbox ("build", "developer", "day")
GENERIC = {"build", "before", "after", "day", "days", "developer", "developers", "dev", "fest", "festival", "challenge",
           "contest", "summit", "conference", "week", "online", "global", "edition", "season", "hack", "start", "here",
           "home", "welcome", "official", "register", "registration", "2025", "2026", "2027", "india", "world", "series"}
HOSTS = {"devpost", "github", "google", "notion", "lu", "luma", "unstop", "devfolio", "hackerearth", "eventbrite",
         "medium", "substack", "linktr", "forms", "docs", "sites", "vercel", "netlify", "framer", "wix", "typeform"}


def _keywords(name: str) -> list[str]:
    words = [HEARD.get(w, w) for w in re.findall(r"[a-z0-9]{3,}", name.lower())]
    return [w for w in dict.fromkeys(words) if w not in STOP]


def brand(url: str) -> str:
    """The name in a site's address: build.qorechain.io -> qorechain; deckthepixels.devpost.com -> deckthepixels."""
    host = re.sub(r"^www\.", "", re.sub(r"^https?://", "", url or "").split("/")[0].split(":")[0].lower())
    parts = [x for x in host.split(".") if x]
    if len(parts) < 2:
        return ""
    name = parts[-2] if parts[-2] not in ("co", "com", "ac", "org", "gov") or len(parts) < 3 else parts[-3]
    if name in HOSTS and len(parts) >= 3:  # a page on a shared host: the event's own subdomain
        name = parts[0]
    return "" if name in HOSTS or len(name) < 3 else name


def _page_name(title: str, url: str) -> str:
    """A short name from a page title: the part that has the site's name in it, not the slogan."""
    b = brand(url)
    pieces = [x.strip() for x in re.split(r"\s+[|·–—-]\s+|:\s+", title or "") if x.strip()]
    pick = next((x for x in pieces if b and b in x.lower().replace(" ", "")), pieces[0] if pieces else b or url)
    return re.sub(r"^(?:the|welcome to)\s+", "", pick, flags=re.I)


def match_mail(row) -> dict | None:
    """The tracked thing this email is about, if any (two of its words, or one rare one, in sender/subject/text)."""
    hay = f"{row['sender']} {row['subject']} {(row['body'] or '')[:3000]}".lower()
    for t in active():
        if t.get("brand"):  # started from its website: its own name has to be there ("build" + "developer" isn't it)
            if t["brand"] in hay:
                return t
            continue
        hits = [k for k in t["keywords"] if k in hay]
        if len(hits) >= 2 or (len(t["keywords"]) == 1 and hits):  # two of its words: "Scaler" alone is their ads
            return t
    return None


# --- starting, and working out its timeline ------------------------------------------------------------------------------
def start(app, name: str, page: dict | None = None) -> str:
    if page:  # from the browser extension: the page itself (its text helps find the dates without Claude)
        name = _page_name(page.get("title") or "", page.get("url") or "") or name
    elif re.fullmatch(r"(?:this|it|this one|this page|this site|this (?:hackathon|event|competition|course|program(?:me)?|"
                    r"contest|challenge))", name.strip(" .,")):
        from batsy import browser  # "keep track of this hackathon": the page you're on

        page = browser.current()
        if not page:
            return "Which one? I can't see a page open in the browser. Say its name."
    if page:
        name = _page_name(page.get("title") or "", page.get("url") or "")
    name = " ".join(HEARD.get(w, w) for w in name.strip(" .,").split())
    name = re.sub(r"\b(?:or something(?: similar)?|or so|and stuff)\b", "", name).strip(" ,")
    with _lock:
        items = _load()
        b = brand(page["url"]) if page else ""
        mine = next((t for t in items if not t.get("stopped") and b and (
            b == t.get("brand") or b in t["keywords"] or any(brand(u) == b for u in t.get("sources", [])))), None)
        if mine:  # another page of something already tracked (its dashboard, its rules): add it, don't start over
            if not mine.get("brand"):
                mine["brand"] = b
            if page["url"] not in mine.setdefault("sources", []):
                mine["sources"].insert(0, page["url"])
            text = re.sub(r"\s+", " ", page.get("text") or "")[:6000]
            if text and text[:200] not in (mine.get("page") or ""):
                mine["page"] = (text + "\n\n" + (mine.get("page") or ""))[:12000]
            _save(items)
            threading.Thread(target=refresh, args=(app, mine["id"]), kwargs={"first": False}, daemon=True).start()
            return f"That's part of {mine['name']}. I've added this page to it and I'm checking it for new dates."
        kw = list(dict.fromkeys([*([b] if b else []), *(w for w in _keywords(name) if w not in GENERIC)])) or _keywords(name)
        same = next((t for t in items if not t.get("stopped") and
                     ((b and b == t.get("brand")) or (not b and set(kw) & set(t["keywords"]) - GENERIC))), None)
        if same:
            return f"I'm already keeping track of {same['name']}."
        t = {"id": int(time.time()), "name": name[:1].upper() + name[1:], "keywords": kw, "started": time.time(),
             "timeline": [], "repo": None, "summary": "", "sources": [page["url"]] if page else [], "updated": 0,
             "page": re.sub(r"\s+", " ", (page or {}).get("text") or "")[:6000], "brand": b}
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
            if base not in links and len(base) < 150 and not re.search(
                    r"unsubscribe|track|click|utm|mailto|accounts\.google|google\.com/accounts|//c\.gle/|//t\.co/|"
                    r"bit\.ly|lnkd\.in|list-manage|sendgrid|mailchimp", base):
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
            from batsy import browser

            ask = browser._now if first and t.get("page") else research._ask
            j = json.loads(ask(
                f"These emails / pages are about \"{t['name']}\", something Jonathan is taking part in. List every dated step "
                f"(registration, team formation, submission deadline, rounds, results, event days; a range like '4 November-2 December' is two steps, its start and its end) as JSON: "
                f'{{"summary": "one line: what it is", "timeline": [{{"what": "short", "when": "the email\'s own words '
                f'for the date, with the date if given"}}]}}. Only dates they state.\n\n{text[:6000]}', 500,
                json_mode=True))
            summary = j.get("summary") or summary
            for x in j.get("timeline") or []:
                words = str(x.get("when") or "")
                if not re.search(r"\d|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|monday|tuesday|wednesday|thursday|"
                                 r"friday|saturday|sunday|tomorrow", words, re.I):
                    continue  # "now", "weekly", "soon": not a date (they all became "today")
                when = mail.when_to_date(words)
                if when:
                    timeline.append({"what": str(x.get("what") or "")[:80], "when": when,
                                     "source": "page" if t.get("page") and not rows else "email",
                                     "changed": bool(re.search(r"extend|postpon|reschedul|new deadline|moved to|"
                                                               r"changed|delay", text, re.I))})
        except (ValueError, TypeError, OSError):
            pass
    future = [x for x in timeline if x["when"][:10] >= dt.date.today().isoformat()]
    if not any(re.search(r"submi|deadline|final|demo|close", x["what"], re.I) for x in future) \
            and (first or not t.get("asked_claude")) and (rows or links):
        web = _from_web(app, t, links)
        if web:
            timeline += web.get("timeline", [])
            summary = web.get("summary") or summary
            t["asked_claude"] = time.time()
    merged = _merge(t.get("timeline", []), timeline)
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


RANK = {"web": 0, "page": 1, "email": 2}


def _same(a: dict, b: dict) -> bool:
    """The same step, worded differently ("Submission deadline" / "Submission deadline for projects"), within a day."""
    from rapidfuzz import fuzz

    days = abs((dt.date.fromisoformat(a["when"][:10]) - dt.date.fromisoformat(b["when"][:10])).days)
    kind = lambda x: next((k for k in ("submi", "regist", "workshop", "result", "winner", "final", "demo", "judg", "start",
                                        "open", "clos", "end") if k in x["what"].lower()), "")  # noqa: E731
    return days <= 1 and (fuzz.token_set_ratio(a["what"].lower(), b["what"].lower()) >= 70 or
                          (kind(a) and kind(a) == kind(b) and days == 0))


def _merge(old: list[dict], new: list[dict]) -> list[dict]:
    """Never lose dates (a refresh finding none used to wipe them), never the same step twice. The official site
    (Claude read it: "web") wins; the small AI's reading of emails only adds what the site doesn't have, and never a
    date that's suspiciously far off (it put weekly workshops in 2027)."""
    have_web = any(x.get("source") == "web" for x in [*old, *new])
    horizon = (dt.date.today() + dt.timedelta(days=200)).isoformat()
    out: list[dict] = []
    for x in sorted([*old, *new], key=lambda x: RANK.get(x.get("source"), 3)):
        if x.get("source") == "email" and (x["when"][:10] > horizon or have_web and not x.get("changed")):
            continue  # with the official timeline in hand, an email only counts when it says a date changed
        if not any(_same(x, y) for y in out):
            out.append(x)
    return sorted(out, key=lambda x: x["when"])


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


NOT_KEY = re.compile(r"workshop|clinic|webinar|session|office hours|\bama\b|livestream|meetup|mixer|talk\b", re.I)


def key_dates(t: dict) -> list[dict]:
    """The dates that matter (deadlines, a round closing, the finale), not every step ("problem statement goes live")."""
    return [x for x in t.get("timeline", []) if KEY.search(x["what"]) and not NOT_KEY.search(x["what"])]


def _schedule(t: dict, items: list[dict]) -> None:
    """The key dates under the clock on their day, a reminder before each; each one only ever once."""
    from batsy.memory_store import get_store
    from batsy.tools import monitor

    done = set(t.get("scheduled", []))
    had = [{"when": k.split("|", 1)[0], "what": k.split("|", 1)[1]} for k in done]
    items = [x for x in items if KEY.search(x["what"]) and not NOT_KEY.search(x["what"]) and f"{x['when']}|{x['what']}" not in done
             and not any(_same(x, y) for y in had) and x["when"][:10] >= dt.date.today().isoformat()]
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
        rows = "\n".join(f"| {x['when'][:10]} | {x['when'][11:16] or ''} | {'✓ ' if is_done(t, x) else ''}{x['what']} | {x.get('source', '')} |"
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


def repo_exists(full: str) -> bool:
    """Through git and your own GitHub login (the system keyring), so a private repo counts too."""
    import os
    import subprocess

    try:
        return subprocess.run(["git", "ls-remote", f"https://github.com/{full}", "HEAD"], capture_output=True, timeout=20,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _git_stats(full: str) -> dict | None:
    """Commits today and the last push, from a small local mirror of the repo (works for private repos: the
    GitHub API without a token only sees public ones). Only the history, never the files (--filter=blob:none)."""
    import os
    import subprocess

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    d = DATA_DIR / "repos" / (full.replace("/", "__") + ".git")
    try:
        if d.exists():
            subprocess.run(["git", "-C", str(d), "fetch", "--quiet", "--prune", "origin", "+refs/heads/*:refs/heads/*"],
                           capture_output=True, timeout=120, env=env)
        else:
            d.parent.mkdir(parents=True, exist_ok=True)
            if subprocess.run(["git", "clone", "--quiet", "--bare", "--filter=blob:none", f"https://github.com/{full}",
                               str(d)], capture_output=True, timeout=180, env=env).returncode:
                return None
        today = subprocess.run(["git", "-C", str(d), "log", "--all", "--since=midnight", "--format=%H"],
                               capture_output=True, text=True, timeout=30).stdout.split()
        last = subprocess.run(["git", "-C", str(d), "log", "--all", "-1", "--format=%cI|%an|%s"],
                              capture_output=True, text=True, timeout=30).stdout.strip()
        when, who, msg = (last.split("|", 2) + ["", "", ""])[:3]
        return {"today": len(today), "pushed": when or None, "last": msg[:80], "by": who}
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("repo %s: %s", full, e)
        return None


def link_repo(t_id: int, full: str) -> None:
    with _lock:
        all_ = _load()
        for x in all_:
            if x["id"] == t_id:
                x["repo"] = {"name": full, "url": f"https://github.com/{full}"}
        _save(all_)
    threading.Thread(target=check_repos, daemon=True).start()


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
            got = _git_stats(t["repo"]["name"])
            if got:
                t["repo"].update(got)
                changed = True
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
# what you've already done takes its deadline off the countdown: "I registered" -> "Registrations close" is done
STEPS = {"regist": r"regist|sign(?:ed)? ?up|enrol", "submi": r"submi", "team": r"team"}
DID = re.compile(r"\b(?:i|we)(?:'ve| have)? (?:just |already |finally )?(?P<verb>registered|signed up|enrolled|"
                 r"submitted|formed (?:a |our )?team|made (?:a |our )?team|joined (?:a |the )?team)\b")


def _kind(text: str) -> str:
    return next((k for k, rx in STEPS.items() if re.search(rx, text, re.I)), "")


def is_done(t: dict, x: dict) -> bool:
    return f"{x['when']}|{x['what']}" in t.get("done", []) or (_kind(x["what"]) and _kind(x["what"]) in t.get("did", []))


def mark_did(t_id: int, kind: str) -> list[dict]:
    """Every step of that kind is done (and its to-do ticked): it leaves the countdown and the cockpit."""
    from batsy.memory_store import get_store

    with _lock:
        items = _load()
        t = next(x for x in items if x["id"] == t_id)
        t["did"] = sorted(set(t.get("did", [])) | {kind})
        _save(items)
    steps = [x for x in t.get("timeline", []) if _kind(x["what"]) == kind]
    st = get_store()
    for x in steps:
        for d in {x["when"][:10], dt.date.today().isoformat()}:
            for r in st.agenda_for(d):
                if not r["done"] and f"({t['name']})" in r["text"] and _kind(r["text"]) == kind:
                    st.tick_agenda(r["id"])
    _to_brain(t)
    return steps


def status(t: dict) -> dict:
    today = dt.date.today().isoformat()
    future = [x for x in t.get("timeline", []) if x["when"][:10] >= today and not is_done(t, x)]
    nxt = future[0] if future else None
    final = next((x for x in reversed(t.get("timeline", [])) if re.search(r"submi|deadline|final|demo", x["what"], re.I)),
                 (t.get("timeline") or [None])[-1])
    left = None
    if nxt:
        target = dt.datetime.fromisoformat(nxt["when"] if len(nxt["when"]) > 10 else nxt["when"] + "T23:59")
        left = int((target - dt.datetime.now()).total_seconds())
    keys = [x for x in key_dates(t) if x["when"][:10] >= today and not is_done(t, x)]
    if keys and len(keys[0]["when"]) > 10 and dt.datetime.fromisoformat(keys[0]["when"]) < dt.datetime.now():
        keys = keys[1:]  # tonight's 11:59 pm has passed: on to the next one
    rnd = keys[0] if keys else None  # the nearest deadline: this round's end
    end = (key_dates(t) or t.get("timeline") or [None])[-1]  # the whole thing's last key date
    if rnd:
        target = dt.datetime.fromisoformat(rnd["when"] if len(rnd["when"]) > 10 else rnd["when"] + "T23:59")
        left = int((target - dt.datetime.now()).total_seconds())
    return {"name": t["name"], "round": rnd, "end": end, "left_s": left if rnd else None, "repo": t.get("repo"),
            "next": nxt, "final": final}


KINDS = {"hackathon", "course", "competition", "contest", "challenge", "fest", "program", "programme", "internship",
         "olympiad", "bootcamp", "exam", "application"}  # what it is, not which one
DOING = re.compile(r"\bi(?:'m| am) (?:doing|in|participating in|taking part in|working on|registered for|joining) "
                   r"(?:the |a |this )?.*?\b(?:hackathon|course|competition|contest|challenge|fest|program(?:me)?|"
                   r"internship|olympiad|bootcamp)\b")


def _heard(items: list[dict], n: str) -> dict | None:
    """Its name as the mic heard it: "core chain" for QoreChain, "scalar" for Scaler (letters, not spelling)."""
    import difflib

    words = re.findall(r"[a-z0-9]+", n)
    grams = {"".join(words[i:i + k]) for k in (1, 2, 3) for i in range(len(words))}
    best, score = None, 0.0
    for t in items:
        for k in [t.get("brand") or "", *t["keywords"]]:
            if len(k) < 5 or k in GENERIC | KINDS:
                continue
            for g in grams:
                r = difflib.SequenceMatcher(None, g, k).ratio()
                if r > score:
                    best, score = t, r
    return best if score >= 0.8 else None


REPO_MADE = re.compile(r"\b(?:created|made|started|set up|setup|opened) (?:a |the |my |our )?(?:new )?"
                       r"(?:repo|repository|repertof|repertoire|ripo|report|git ?hub repo)\b(?:.*?\b(?:name is|called|named) "
                       r"(?P<repo>[\w./ -]{3,40}?))?[.!]?$")


def handle(app, n: str) -> str | None:
    m = START.search(n)
    if m and not re.match(r"(?:how|what|when|is)\b", n):
        name = (m.group("name") or m.group("name2") or "").strip()
        if name and not re.match(r"(?:it|this|that|of it|my (?:email|print|printer))$", name):
            return start(app, name)
    items = active()
    if not items:
        return None
    t = next((x for x in items if any(re.search(rf"\b{k}\b", n) for k in x["keywords"] if k not in GENERIC | KINDS)),
             None) or _heard(items, n) or \
        (items[0] if len(items) == 1 and re.search(r"\b(?:hackathon|it|the event|tracking)\b", n) else None)
    did = DID.search(n)
    if did:
        kind = _kind(did.group("verb")) or "team"
        cands = [t] if t else [x for x in items if any(_kind(y["what"]) == kind and y["when"][:10] >= dt.date.today().isoformat()
                                                        and not is_done(x, y) for y in x.get("timeline", []))]
        if len(cands) == 1:
            who = cands[0]
            steps = mark_did(who["id"], kind)
            nxt = status(next(x for x in _load() if x["id"] == who["id"]))["round"]
            return (f"Nice. {who['name']}: {', '.join(x['what'] for x in steps[:2]) or kind} is done."
                    + (f" Next: {nxt['what']}, {_when(nxt['when'])}." if nxt else ""))
        if len(cands) > 1:
            return f"For which one, {' or '.join(x['name'] for x in cands[:3])}?"
    doing = DOING.search(n)
    if not t and doing:  # "I'm doing the course in hackathon": a name the mic got wrong. Ask, don't chat about it.
        names = " or ".join(x["name"] for x in items[:3])
        return f"Which one, {names}? Or open its website and say keep track of this."
    if not t:
        return None
    if doing and not re.search(r"\b(?:how|what|when|status|update|deadline|due)\b", n):
        n += " status"  # "I'm doing the QoreChain fest": tell him where it's at
    m = re.search(r"\b(?:its |the |my )?(?:repo|repository)(?: for it)? (?:is|called|named) (?P<repo>[\w./-]+)", n) or \
        (REPO_MADE.search(n) if REPO_MADE.search(n) and REPO_MADE.search(n).group("repo") else None)
    if m:  # "the hackathon repo is voice-india" / "I created a repo for Scaler, its name is vanigakural"
        name = re.sub(r"[^\w./-]", "", m.group("repo").replace(" ", ""))
        full = name if "/" in name else f"{GITHUB_USER}/{name}"
        if not repo_exists(full):  # misheard, or not pushed yet: never link a repo that isn't there
            return (f"I can't find a repo called {m.group('repo')} on your GitHub. Type its name in the Batsy app, "
                    f"like: the {t['name']} repo is <name>.")
        link_repo(t["id"], full)
        return f"Linked {name} to {t['name']}. I'll show its commits in the cockpit."
    if REPO_MADE.search(n):  # "I created a repo for it" (no name): the newest repo on GitHub, if it's new
        return f"What's the repo called? Say: the {t['name']} repo is, and its name. Or type it in the app."
    if re.search(r"\b(?:stop|don'?t|no longer) (?:keep(?:ing)? )?track", n):
        with _lock:
            all_ = _load()
            for x in all_:
                if x["id"] == t["id"]:
                    x["stopped"] = time.time()
            _save(all_)
        return f"Stopped keeping track of {t['name']}."
    if re.search(r"\b(?:how'?s|how is|what'?s left|status|update|when'?s|deadline|due)\b", n):
        s = status(t)  # the same two dates the cockpit shows: this round's deadline, and when it all ends
        bits = [f"{t['name']}:"]
        if s["round"]:
            bits.append(f"{s['round']['what']}, {_when(s['round']['when'])}.")
        if s["end"] and s["end"] != s["round"]:
            bits.append(f"It ends with {s['end']['what'][:1].lower() + s['end']['what'][1:]}, {_when(s['end']['when'])}.")
        if not s["round"] and not s["end"]:
            bits.append("I haven't found its dates yet.")
        r = s["repo"]
        bits.append(f"Your repo {r['name'].split('/')[-1]} has {r.get('today', 0)} commits today." if r else
                    "I'll link your GitHub repo as soon as you create it.")
        return " ".join(bits)
    return None
