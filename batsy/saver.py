"""The screensaver as a live dashboard that Batsy can rebuild.

Its layout lives in screensaver.json: a theme (animated background style + accent colour) and a list of widgets.
Batsy fills each widget with live data every refresh, and adapts the order and emphasis to what matters right now
(budget nearly used, questions waiting, music playing, late at night). Change it by voice ("add the weather in
Chennai to the screensaver", "make it minimal") or let Claude Code edit the JSON (`batsy saver get/set/preview`).
"""
import datetime as dt
import json
import re
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

from batsy.config import DATA_DIR, HOME

SPEC_FILE = DATA_DIR / "screensaver.json"
STYLES = ["aurora", "stars", "waves", "orb", "minimal"]
ACCENTS = {"purple": "#d646ff", "blue": "#4f9dff", "cyan": "#38e1ff", "green": "#43e08a", "red": "#ff4d5e",
           "orange": "#ff9d3a", "pink": "#ff5fbf", "gold": "#f5c542", "yellow": "#ffe14d", "white": "#f0f0f0",
           "teal": "#2fd4b5", "batman": "#ffc20e"}
WIDGETS = {  # type: (default title, column span)
    "clock": ("", 3), "claude_budget": ("Claude", 2), "local_tokens": ("Local AI · today", 1),
    "running": ("Running now", 1), "waiting": ("Waiting for you", 1), "upcoming": ("Coming up", 1),
    "agenda": ("Today's plan", 1), "system": ("Laptop", 1), "now_playing": ("Now playing", 1),
    "countdown": ("Countdown", 1), "note": ("Note", 1), "weather": ("Weather", 1), "photo": ("", 1),
    "project": ("Project", 1), "command": ("", 1), "quote": ("", 2),
}
PRESETS = {
    "default": [{"type": "clock"}, {"type": "claude_budget"}, {"type": "local_tokens"}, {"type": "running"},
                {"type": "waiting"}, {"type": "upcoming"}],
    "minimal": [{"type": "clock"}, {"type": "waiting"}, {"type": "upcoming"}],
    "detailed": [{"type": "clock"}, {"type": "claude_budget"}, {"type": "system"}, {"type": "running"},
                 {"type": "waiting"}, {"type": "upcoming"}, {"type": "agenda"}, {"type": "now_playing"},
                 {"type": "quote"}],
    "focus": [{"type": "clock"}, {"type": "upcoming"}, {"type": "quote"}],
}
QUOTES = [
    "Make it work, make it right, make it fast. (Kent Beck)", "Simplicity is prerequisite for reliability. (Dijkstra)",
    "The best way to predict the future is to invent it. (Alan Kay)", "Done is better than perfect.",
    "First, solve the problem. Then, write the code. (John Johnson)", "Stay hungry, stay foolish. (Steve Jobs)",
    "It always seems impossible until it's done. (Nelson Mandela)", "Small steps every day.",
]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
WEATHER_CODES = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy",
                 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain",
                 65: "heavy rain", 71: "light snow", 73: "snow", 75: "heavy snow", 80: "showers", 81: "showers",
                 82: "heavy showers", 95: "thunderstorm", 96: "thunderstorm", 99: "thunderstorm"}


# --- the spec -------------------------------------------------------------------------------------------------
def load() -> dict:
    if SPEC_FILE.exists():
        try:
            return json.loads(SPEC_FILE.read_text())
        except ValueError:
            pass
    return {"theme": {"style": "stars", "accent": ACCENTS["batman"]}, "widgets": PRESETS["default"],
            "auto_adapt": True}


def save(spec: dict) -> None:
    spec.setdefault("theme", {}).setdefault("style", "aurora")
    spec["theme"].setdefault("accent", ACCENTS["batman"])
    spec["widgets"] = [w for w in spec.get("widgets", []) if w.get("type") in WIDGETS]
    SPEC_FILE.write_text(json.dumps(spec, indent=1))


# --- live data ------------------------------------------------------------------------------------------------
_cache: dict = {}


def _cached(key: str, ttl: float, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception as e:
        val = {"error": str(e)[:80]}
    _cache[key] = (time.time(), val)
    return val


def _weather(city: str) -> dict:
    def fetch():
        g = json.loads(urllib.request.urlopen("https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
            {"name": city, "count": 1}), timeout=8).read())["results"][0]
        w = json.loads(urllib.request.urlopen("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(
            {"latitude": g["latitude"], "longitude": g["longitude"],
             "current": "temperature_2m,weather_code,relative_humidity_2m"}), timeout=8).read())["current"]
        return {"place": g["name"], "temp": round(w["temperature_2m"]), "desc": WEATHER_CODES.get(w["weather_code"], ""),
                "humidity": w.get("relative_humidity_2m")}
    return _cached(f"weather:{city}", 900, fetch)


def _system() -> dict:
    import psutil

    temps = psutil.sensors_temperatures() if hasattr(psutil, "sensors_temperatures") else {}
    hottest = max((e.current for es in (temps or {}).values() for e in es), default=None)
    b = psutil.sensors_battery()
    return {"cpu": psutil.cpu_percent(interval=None), "ram": psutil.virtual_memory().percent,
            "temp": round(hottest) if hottest else None,
            "battery": round(b.percent) if b else None, "plugged": bool(b and b.power_plugged)}


def _countdown(w: dict) -> dict:
    try:
        target = dt.datetime.fromisoformat(w["date"])
    except (KeyError, ValueError):
        return {"error": "no date"}
    left = target - dt.datetime.now()
    if left.total_seconds() <= 0:
        return {"big": "Today!" if left.days == -1 else "Done", "sub": w.get("title", "")}
    days, hours = left.days, left.seconds // 3600
    return {"big": f"{days}d {hours}h" if days else f"{hours}h {(left.seconds % 3600) // 60}m",
            "sub": f"until {w.get('title', 'it')} · {target.strftime('%a %d %b')}"}


def _photo(w: dict) -> dict:
    folder = Path(w.get("folder", str(HOME / "Pictures"))).expanduser()
    pics = sorted(p for p in folder.rglob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))[:400] \
        if folder.is_dir() else []
    if not pics:
        return {"error": f"no photos in {folder}"}
    return {"path": str(pics[int(time.time() // w.get("every", 20)) % len(pics)])}


def _command(w: dict) -> dict:
    def run():
        p = subprocess.run(["bash", "-lc", w["command"]], capture_output=True, text=True, timeout=8)
        return {"lines": (p.stdout or p.stderr).strip().splitlines()[:8]}
    return _cached(f"cmd:{w.get('command')}", max(15, w.get("every", 60)), run)


def payload(app) -> dict:
    """Spec + live data for every widget, adapted to the moment."""
    from batsy import activity, claude_code, usage
    from batsy.memory_store import get_store

    spec = load()
    act = activity.snapshot()
    budget = claude_code.budget(app.cfg)
    widgets = [dict(w) for w in spec["widgets"]]
    theme = dict(spec["theme"])
    hour = dt.datetime.now().hour

    if spec.get("auto_adapt", True):
        types = {w["type"] for w in widgets}
        playing = None
        try:
            from batsy.tools import music

            sp = music.get_spotify(app.cfg)
            if sp.ready():
                playing = _cached("np", 30, sp.now_playing)
        except Exception:
            pass
        if isinstance(playing, str) and playing.startswith("Playing") and "now_playing" not in types:
            widgets.append({"type": "now_playing", "auto": True})
        if act["asks"] and "waiting" not in types:
            widgets.insert(1, {"type": "waiting", "auto": True})
        if act["agents"] and "running" not in types:
            widgets.append({"type": "running", "auto": True})
        pct = max(budget["session"]["pct"] or 0, budget["week"]["pct"] or 0)
        for w in widgets:  # urgent things glow and move up
            if (w["type"] == "claude_budget" and pct >= 80) or (w["type"] == "waiting" and act["asks"]):
                w["highlight"] = True
        widgets.sort(key=lambda w: (w["type"] != "clock", not w.get("highlight")))
        if hour >= 23 or hour < 6:  # night: calm and dim
            theme.update(style="minimal" if theme.get("style") != "stars" else "stars", dim=True)

    data = {}
    for i, w in enumerate(widgets):
        t = w["type"]
        if t == "claude_budget":
            d = budget
        elif t == "local_tokens":
            d = usage.stats()["today"]
        elif t == "running":
            d = {"rows": [f"{a['label']}  ·  {a['secs'] // 60}m {a['secs'] % 60:02d}s" if a.get("secs") else a["label"]
                          for a in act["agents"]]}
        elif t == "waiting":
            d = {"rows": [q["question"] + (f"  ({q['mins']}m)" if q.get("mins") else "") for q in act["asks"]]}
        elif t == "upcoming":
            d = {"rows": [f"{u['when']}: {u['title']}" for u in app.tasks.store.upcoming()]}
        elif t == "agenda":
            d = {"rows": [a["text"] for a in get_store().agenda_for(time.strftime("%Y-%m-%d"))]}
        elif t == "system":
            d = _system()
        elif t == "now_playing":
            from batsy.tools import music

            sp = music.get_spotify(app.cfg)
            d = {"text": _cached("np", 30, sp.now_playing) if sp.ready() else "Spotify not connected"}
        elif t == "countdown":
            d = _countdown(w)
        elif t == "note":
            d = {"text": w.get("text", "")}
        elif t == "weather":
            d = _weather(w.get("city", "Chennai"))
        elif t == "photo":
            d = _photo(w)
        elif t == "project":
            task = app.tasks.store.find(w.get("name", ""))
            d = {"rows": [f"{h['day'][5:]}: {h['note'] or h['status']}" for h in app.tasks.store.history(task["id"], 4)]
                 if task else [], "title": task["title"] if task else w.get("name", "")}
        elif t == "command":
            d = _command(w)
        elif t == "quote":
            d = {"text": QUOTES[(int(time.time()) // 3600) % len(QUOTES)]}
        else:
            d = {}
        w.setdefault("title", w.get("title") or (d.get("title") if isinstance(d, dict) else None) or WIDGETS[t][0])
        w.setdefault("span", WIDGETS[t][1])
        data[str(i)] = d
    return {"theme": theme, "widgets": widgets, "data": data}


# --- voice (no AI) --------------------------------------------------------------------------------------------
ALIASES = [
    (r"weather(?: in| for)? ?([a-z ]+)?", "weather"), (r"(?:system|laptop|cpu|battery|computer) ?(?:stats|status|info)?", "system"),
    (r"(?:music|song|now playing|spotify|what'?s playing)", "now_playing"), (r"clock|time", "clock"),
    (r"claude (?:usage|budget|limits?)|budget|limits?", "claude_budget"), (r"(?:local )?tokens", "local_tokens"),
    (r"(?:running|agents|jobs|what'?s running)", "running"), (r"(?:questions|waiting|what you'?re waiting for)", "waiting"),
    (r"(?:chores|tasks|habits|coming up|upcoming)", "upcoming"), (r"(?:agenda|plan|today'?s plan|schedule)", "agenda"),
    (r"(?:photos?|pictures?|slideshow)(?: from (?:my )?([a-z ]+))?", "photo"), (r"quotes?", "quote"),
]


def _date(text: str) -> dt.datetime | None:
    t = text.lower()
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)? (?:of )?(" + "|".join(MONTHS) + r")\b(?: (\d{4}))?", t) or \
        re.search(r"\b(" + "|".join(MONTHS) + r") (\d{1,2})(?:st|nd|rd|th)?\b(?: (\d{4}))?", t)
    if m:
        a, b, y = m.groups()
        day, month = (int(a), MONTHS.index(b) + 1) if a.isdigit() else (int(b), MONTHS.index(a) + 1)
        year = int(y) if y else dt.date.today().year
        d = dt.datetime(year, month, day, 9, 0)
        return d if d >= dt.datetime.now() - dt.timedelta(days=1) or y else d.replace(year=year + 1)
    m = re.search(r"\bin (\d+) (day|week)s?\b", t)
    if m:
        return dt.datetime.now() + dt.timedelta(days=int(m.group(1)) * (7 if m.group(2) == "week" else 1))
    for i, name in enumerate(["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]):
        if re.search(rf"\b(?:next |this |on )?{name}\b", t):
            ahead = (i - dt.date.today().weekday()) % 7 or 7
            return dt.datetime.combine(dt.date.today() + dt.timedelta(days=ahead), dt.time(9))
    return None


def handle(n: str, app) -> str | None:
    """Zero-AI screensaver edits. Returns what to say, or None if this isn't one."""
    if "screensaver" not in n and "screen saver" not in n:
        return None
    n = n.replace("screen saver", "screensaver")
    spec = load()
    if re.search(r"\b(show|preview|start|open|turn on) (me )?(the |my )?screensaver( now)?$", n):
        app.preview_saver()
        return ""
    m = re.search(r"\b(minimal|simple|default|normal|detailed|busy|everything|focus)\b", n)
    if m and re.search(r"\b(make|set|switch|change|use)\b", n) and "theme" not in n:
        preset = {"simple": "minimal", "normal": "default", "busy": "detailed", "everything": "detailed"}.get(m.group(1), m.group(1))
        spec["widgets"] = [dict(w) for w in PRESETS[preset]]
        save(spec)
        app.refresh_saver()
        return f"Screensaver set to {preset}."
    changed = []
    style = next((s for s in STYLES if re.search(rf"\b{s}\b", n)), None)
    if style and not re.search(r"\b(add|remove)\b", n):
        spec["theme"]["style"] = style
        changed.append(f"{style} background")
    accent = next((c for c in ACCENTS if re.search(rf"\b{c}\b", n)), None)
    if accent and re.search(r"\b(colou?r|theme|make|accent|turn)\b", n) and not re.search(r"\b(add|remove)\b", n):
        spec["theme"]["accent"] = ACCENTS[accent]
        changed.append(f"{accent} accent")
    if re.search(r"\b(turn off|disable|stop) (the )?(auto|adapt|adaptive)", n):
        spec["auto_adapt"] = False
        changed.append("no auto changes")
    elif re.search(r"\b(turn on|enable) (the )?(auto|adapt|adaptive)", n):
        spec["auto_adapt"] = True
        changed.append("auto changes on")
    m = re.search(r"\b(?:add |put |show |make )?(?:a )?countdown (?:to|until|for) (?:my |the )?(.+?)(?: on| at| in|$)", n)
    if m and _date(n):
        spec["widgets"].append({"type": "countdown", "title": m.group(1).strip(), "date": _date(n).isoformat()})
        changed.append(f"countdown to {m.group(1).strip()}")
    m = re.search(r"\b(?:add|put|show) (?:a )?note(?: saying| that says)? (.+?) (?:on|to) (?:the |my )?screensaver", n)
    if m:
        spec["widgets"].append({"type": "note", "text": m.group(1)})
        changed.append("note")
    m = re.search(r"\b(?:add|show|put) (?:my )?(.+?) project (?:on|to) (?:the |my )?screensaver", n)
    if m:
        spec["widgets"].append({"type": "project", "name": m.group(1) + " project", "title": m.group(1).title()})
        changed.append(f"{m.group(1)} project")
    m = re.search(r"\b(add|show|put|include|remove|hide|take off|drop|get rid of) (?:the |my |a )?(.+?) "
                  r"(?:on|to|from|off) (?:the |my )?screensaver", n)
    if m and not changed:
        verb, what = m.group(1), m.group(2)
        for pattern, wtype in ALIASES:
            mm = re.fullmatch(pattern, what)
            if mm:
                if verb in ("remove", "hide", "take off", "drop", "get rid of"):
                    before = len(spec["widgets"])
                    spec["widgets"] = [w for w in spec["widgets"] if w["type"] != wtype]
                    changed.append(f"removed {wtype.replace('_', ' ')}" if len(spec["widgets"]) < before else "")
                else:
                    w = {"type": wtype}
                    if wtype == "weather":
                        w["city"] = (mm.group(1) or app.cfg.data.get("saver_city") or "Chennai").strip().title()
                    if wtype == "photo" and mm.group(1):
                        w["folder"] = str(HOME / mm.group(1).strip().title())
                    spec["widgets"] = [x for x in spec["widgets"] if x["type"] != wtype or wtype in ("weather", "photo")] + [w]
                    changed.append(f"added {wtype.replace('_', ' ')}")
                break
    changed = [c for c in changed if c]
    if not changed:
        return None  # something fancier: let Claude edit the layout
    save(spec)
    app.refresh_saver()
    return "Screensaver updated: " + ", ".join(changed) + "."


def cli(argv: list[str]) -> None:
    """`batsy saver get | set JSON | preview` (Claude Code uses this to redesign the screensaver)."""
    if not argv or argv[0] == "get":
        print(json.dumps(load(), indent=1))
    elif argv[0] == "set":
        spec = json.loads(Path(argv[1][1:]).read_text() if argv[1].startswith("@") else argv[1])
        save(spec)
        from batsy.app import ask_daemon

        ask_daemon("show the screensaver")
        print(json.dumps({"ok": True, "widgets": [w["type"] for w in load()["widgets"]]}))
    elif argv[0] == "preview":
        from batsy.app import ask_daemon

        print("shown" if ask_daemon("show the screensaver") else "Batsy isn't running")


SAVER_BRIEF = """You design the screensaver of Batsy, a voice assistant on the user's Fedora laptop. The user said: \
"{request}"

The current layout (JSON):
{spec}

Layout format: {{"theme": {{"style": aurora|stars|waves|orb|minimal, "accent": "#rrggbb"}}, "auto_adapt": true|false,
"widgets": [ ... ]}}. Widgets are drawn in order: "clock" spans the top, and the rest flow into rows of 3 columns.
Each widget can take "title" and "span" (1-3). Types:
  clock, claude_budget, local_tokens, running, waiting, upcoming, agenda, system, now_playing, quote,
  countdown {{title, date: ISO datetime}}, note {{text}}, weather {{city}}, photo {{folder, every: seconds}},
  project {{name}}, command {{title, command: bash, every: seconds}} (shows up to 8 lines of the command's output,
  so you can build any custom widget with a shell one-liner or a small script).
Save the new layout with: batsy saver set 'JSON'   (it shows the screensaver right away)
Final reply: one short spoken sentence about what changed."""
