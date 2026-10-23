"""Presets that run locally (zero tokens): good morning, good night, and user-defined routines.

Day logic: until `routines.day_starts_at` (default 5 AM) it is still "last night". So "good night" at
3 AM on 5 Oct asks about 5 Oct (the day you'll wake into), and at 11 PM on 4 Oct also asks about 5 Oct.
"""
import datetime as dt
import json
import re
import subprocess
import time

from batsy.config import DATA_DIR
from batsy.memory_store import get_store

ROUTINES_FILE = DATA_DIR / "routines.json"
MORNING = re.compile(r"(good )?morning( batsy)?|rise and shine|i'?m (up|awake)")
NIGHT = re.compile(r"(good ?night|night night|nighty night)( batsy)?|i'?m (going to|off to) (sleep|bed)|"
                   r"going to (sleep|bed)|time (to|for) (sleep|bed)")
NO_MORE = re.compile(r"(no|nope|nothing|nah|that'?s (it|all|everything)|i'?m done|done|not really|"
                     r"i don'?t (have|think so))\b")


def day_name(d: dt.date) -> str:
    return f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}"


def next_day(now: dt.datetime, day_starts_at: int) -> dt.date:
    """The day you'll wake up into, if you went to sleep now."""
    logical_today = (now - dt.timedelta(hours=day_starts_at)).date()
    return logical_today + dt.timedelta(days=1)


def custom_routines(cfg) -> dict:
    routines = dict(cfg.data.get("routines", {}).get("custom", {}))
    if ROUTINES_FILE.exists():
        try:
            routines.update(json.loads(ROUTINES_FILE.read_text()))
        except ValueError:
            pass
    return routines


def save_routine(name: str, spec: dict) -> None:
    data = json.loads(ROUTINES_FILE.read_text()) if ROUTINES_FILE.exists() else {}
    data[name] = spec
    ROUTINES_FILE.write_text(json.dumps(data, indent=1))


class Routines:
    """Owned by the app. `handle(n)` returns True if it took the utterance."""

    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        self.capture: dict | None = None  # waiting for the answer to "anything for tomorrow?"

    @property
    def capturing(self) -> bool:
        return bool(self.capture) and time.time() < self.capture["until"]

    def handle(self, n: str) -> bool:
        if self.capturing and not (MORNING.fullmatch(n) or NIGHT.fullmatch(n)):
            return self._capture_answer(n)
        self.capture = None
        if MORNING.fullmatch(n):
            self.morning()
            return True
        if NIGHT.fullmatch(n):
            self.night()
            return True
        for name, spec in custom_routines(self.cfg).items():
            if any(n == re.sub(r"[^a-z0-9' ]", "", p.lower()).strip() for p in spec.get("phrases", [])):
                self.run_custom(name, spec)
                return True
        return False

    # --- built-ins --------------------------------------------------------
    def morning(self) -> None:
        today = dt.date.today()
        items = get_store().agenda_for(today.isoformat())
        reply = self.app._reply
        tracked = (self.app.tasks.morning_line() + self.app.tasks.morning_projects()) if hasattr(self.app, "tasks") else ""
        if items:
            plan = "; ".join(i["text"].rstrip(".!") for i in items)
            reply(f"Good morning! It's {day_name(today)}. Here's your day: {plan}.{tracked}")
            self.app.show_visual("card", f"{day_name(today)} | " + "\n".join(f"• {i['text']}" for i in items))
        else:
            reply(f"Good morning! It's {day_name(today)}." + (tracked or " Nothing's planned for today, so it's all yours."))
        extra = self.cfg.get("routines.morning_extra")
        if extra:
            self.app.speak_brief(extra)

    def night(self) -> None:
        target = next_day(dt.datetime.now(), self.cfg.get("routines.day_starts_at"))
        already = get_store().agenda_for(target.isoformat())
        have = f" You already have: {'; '.join(i['text'].rstrip('.!') for i in already)}." if already else ""
        self.app._reply(f"Good night!{have} Anything you have on {day_name(target)}?")
        self.capture = {"date": target, "until": time.time() + 120, "count": 0}

    def _capture_answer(self, n: str) -> bool:
        cap = self.capture
        if NO_MORE.match(n) and len(n.split()) <= 6:
            self.capture = None
            self.app._reply("Okay. Sleep well." if cap["count"] == 0 else "All saved. Sleep well.")
            return True
        get_store().add_agenda(cap["date"].isoformat(), self.app.last_raw_text or n)
        cap["count"] += 1
        cap["until"] = time.time() + 120
        self.app._reply(f"Got it for {cap['date'].strftime('%A')}. Anything else?")
        return True

    @property
    def follow_up_seconds(self) -> float | None:
        """Give the user longer to think when Batsy asked about tomorrow."""
        return 15 if self.capturing else None

    # --- user presets -----------------------------------------------------
    def run_custom(self, name: str, spec: dict) -> None:
        for cmd in spec.get("commands", []):
            subprocess.Popen(["bash", "-lc", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        if spec.get("spotify"):
            from batsy import spotify
            from batsy.tools import music

            sp = music.get_spotify(self.cfg)
            if sp.ready():
                try:
                    kind, query = spotify.parse_play(spec["spotify"])
                    sp.play(query, kind)
                except spotify.SpotifyError as e:
                    self.app._reply(str(e))
        if spec.get("say"):
            self.app._reply(spec["say"])
        if spec.get("ask"):  # optional: hand a prompt to Claude (costs tokens)
            self.app.speak_brief(spec["ask"])
