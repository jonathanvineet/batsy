"""Recurring chores, projects and habits: Batsy keeps track and checks in with you (local, no tokens).

"Every weekend I work on my robot project", "I do laundry every Sunday", "gym Monday Wednesday Friday at
6 pm", "water the plants every 3 days". On due days Batsy reminds you (if there's a time) and later asks
how it went ("Did you get the laundry done?" / "How did the robot project go today?"). It asks when you're
actually at the laptop, logs the answer, and keeps a history you can ask about.
"""
import datetime as dt
import difflib
import json
import re
import sqlite3
import threading
import time

from batsy import activity
from batsy.config import DATA_DIR

DB = DATA_DIR / "tasks.db"
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
PARTS_OF_DAY = {"morning": "09:00", "afternoon": "14:00", "evening": "19:00", "night": "21:00", "tonight": "21:00"}
CHORE_WORDS = re.compile(r"\b(laundry|clean|cleaning|dishes|vacuum|mop|trash|garbage|groceries|shopping|iron|cook|"
                         r"water the plants|water plants|tidy|chores?|wash|bills?|rent)\b")
PROJECT_WORDS = re.compile(r"\b(project|build|robot|drone|app|website|research|thesis|study|studying|revision|"
                           r"practice|learn|learning|course)\b")
YES = re.compile(r"^(yes|yeah|yep|yup|done|did it|i did|finished|completed|all done|sure did|of course)\b")
NO = re.compile(r"^(no|nope|nah|not yet|didn'?t|i didn'?t|skipped|skip|forgot|not today|couldn'?t)\b")


# --- understanding schedules ------------------------------------------------------------------------------------
def _time(text: str) -> str | None:
    m = re.search(r"\bat (\d{1,2})(?::(\d{2}))? ?(am|pm|a m|p m)?\b", text)
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(" ", "")
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        if not ap and h < 7:  # "at 6" for a chore almost always means the evening
            h += 12
        return f"{h:02d}:{mi:02d}"
    m = re.search(r"\b(?:in the |every |each )?(morning|afternoon|evening|night|tonight)s?\b", text)
    return PARTS_OF_DAY[m.group(1)] if m else None


def parse_schedule(text: str) -> tuple[dict | None, str]:
    """'laundry every sunday at 6 pm' -> ({"days": [6], "time": "18:00"}, matched phrase)."""
    t = text.lower()
    sched, phrase = None, ""
    m = re.search(r"\bevery (other|second|\d+) days?\b", t)
    if m:
        n = 2 if m.group(1) in ("other", "second") else int(m.group(1))
        sched, phrase = {"interval": n, "anchor": dt.date.today().isoformat()}, m.group(0)
    elif re.search(r"\b(every ?day|daily|each day|every single day|every (?:morning|afternoon|evening|night)|nightly)\b", t):
        sched, phrase = {"days": list(range(7))}, re.search(r"\b(every ?day|daily|each day|every single day|every (?:morning|afternoon|evening|night)|nightly)\b", t).group(0)
    elif re.search(r"\b(every weekday|weekdays|on weekdays|monday to friday)\b", t):
        sched, phrase = {"days": [0, 1, 2, 3, 4]}, re.search(r"\b(every weekday|weekdays|on weekdays|monday to friday)\b", t).group(0)
    elif re.search(r"\b(every weekend|weekends|on weekends|on the weekend|at the weekend|saturday and sunday)\b", t):
        sched, phrase = {"days": [5, 6]}, re.search(r"\b(every weekend|weekends|on weekends|on the weekend|at the weekend|saturday and sunday)\b", t).group(0)
    else:
        found = [i for i, d in enumerate(DAYS) if re.search(rf"\b{d}s?\b", t)]
        if found:
            sched = {"days": found}
            day = "(?:" + "|".join(DAYS) + ")s?"
            phrase = re.search(rf"\b(?:every |on |each )?{day}(?:(?:,? and |, | ){day})*\b", t)
            phrase = phrase.group(0) if phrase else ""
        elif re.search(r"\b(every week|weekly|once a week)\b", t):
            sched, phrase = {"days": [dt.date.today().weekday()]}, re.search(r"\b(every week|weekly|once a week)\b", t).group(0)
        elif re.search(r"\b(every month|monthly|once a month)\b", t):
            sched, phrase = {"monthday": dt.date.today().day}, re.search(r"\b(every month|monthly|once a month)\b", t).group(0)
    if sched is not None:
        tm = _time(t)
        if tm:
            sched["time"] = tm
    return sched, phrase


def describe(s: dict) -> str:
    if "interval" in s:
        base = "every other day" if s["interval"] == 2 else f"every {s['interval']} days"
    elif "monthday" in s:
        d = s["monthday"]
        suffix = "th" if 11 <= d % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th")
        base = f"on the {d}{suffix} of every month"
    elif s.get("days") == list(range(7)):
        base = "every day"
    elif s.get("days") == [0, 1, 2, 3, 4]:
        base = "on weekdays"
    elif s.get("days") == [5, 6]:
        base = "every weekend"
    else:
        names = [DAYS[d].capitalize() for d in s.get("days", [])]
        base = "every " + (" and ".join(names) if len(names) < 3 else ", ".join(names[:-1]) + " and " + names[-1])
    if s.get("time"):
        h, m = map(int, s["time"].split(":"))
        base += f" at {h % 12 or 12}{':%02d' % m if m else ''} {'PM' if h >= 12 else 'AM'}"
    return base


def is_due(s: dict, day: dt.date) -> bool:
    if "interval" in s:
        return (day - dt.date.fromisoformat(s["anchor"])).days % s["interval"] == 0
    if "monthday" in s:
        return day.day == s["monthday"]
    return day.weekday() in s.get("days", [])


# --- storage ------------------------------------------------------------------------------------------------------
class Tasks:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(DB), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock, self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, title TEXT, kind TEXT, schedule TEXT,
                    active INTEGER DEFAULT 1, created REAL);
                CREATE TABLE IF NOT EXISTS task_log (id INTEGER PRIMARY KEY, task_id INTEGER, day TEXT, t REAL,
                    status TEXT, note TEXT);
            """)
        self.pending: dict | None = None  # the check-in question waiting for an answer
        self.asked: set[tuple[int, str, str]] = set()  # (task, day, "remind"|"checkin")

    def all(self) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute("SELECT * FROM tasks WHERE active = 1 ORDER BY id").fetchall()

    def add(self, title: str, kind: str, schedule: dict) -> int:
        with self.lock, self.db:
            return self.db.execute("INSERT INTO tasks (title, kind, schedule, created) VALUES (?,?,?,?)",
                                   (title, kind, json.dumps(schedule), time.time())).lastrowid

    def remove(self, task_id: int) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE tasks SET active = 0 WHERE id = ?", (task_id,))

    def log(self, task_id: int, status: str, note: str = "", day: dt.date | None = None) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT INTO task_log (task_id, day, t, status, note) VALUES (?,?,?,?,?)",
                            (task_id, (day or dt.date.today()).isoformat(), time.time(), status, note))

    def history(self, task_id: int, limit: int = 5) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute("SELECT * FROM task_log WHERE task_id = ? ORDER BY t DESC LIMIT ?",
                                   (task_id, limit)).fetchall()

    def logged_on(self, task_id: int, day: dt.date) -> bool:
        with self.lock:
            return self.db.execute("SELECT 1 FROM task_log WHERE task_id = ? AND day = ? AND status != 'asked'",
                                   (task_id, day.isoformat())).fetchone() is not None

    def find(self, words: str) -> sqlite3.Row | None:
        tasks = self.all()
        if not tasks:
            return None
        w = words.lower()
        scored = [(difflib.SequenceMatcher(None, w, t["title"].lower()).ratio() +
                   0.6 * len(set(w.split()) & set(t["title"].lower().split())), t) for t in tasks]
        score, task = max(scored, key=lambda x: x[0])
        return task if score >= 0.6 else None

    def due(self, day: dt.date) -> list[sqlite3.Row]:
        return [t for t in self.all() if is_due(json.loads(t["schedule"]), day)]

    def upcoming(self, days: int = 7) -> list[dict]:
        out, today = [], dt.date.today()
        for i in range(days):
            d = today + dt.timedelta(days=i)
            for t in self.due(d):
                if i == 0 and self.logged_on(t["id"], d):
                    continue
                s = json.loads(t["schedule"])
                when = "Today" if i == 0 else "Tomorrow" if i == 1 else d.strftime("%A")
                out.append({"title": t["title"], "kind": t["kind"], "when": when + (f" {s['time']}" if s.get("time") else "")})
        return out[:8]

    def checkin_time(self, t: sqlite3.Row) -> str:
        s = json.loads(t["schedule"])
        if s.get("time"):
            h, m = map(int, s["time"].split(":"))
            end = dt.datetime.combine(dt.date.today(), dt.time(h, m)) + dt.timedelta(minutes=90)
            return end.strftime("%H:%M")
        return self.cfg.get(f"tasks.checkin_{t['kind']}") or "20:00"

    def question(self, t: sqlite3.Row) -> str:
        if t["kind"] == "project":
            return f"How did {t['title']} go today? Any progress?"
        if t["kind"] == "habit":
            return f"Did you {t['title']} today?"
        return f"Did you get {t['title']} done?"


# --- talking -----------------------------------------------------------------------------------------------------
ADD = re.compile(r"^(?:(?:please )?(?:add|track|create|set up|start tracking|keep track of|remind me (?:to|about))"
                 r"(?: a| an| my)?(?: (chore|project|habit|task|routine))?(?: called| for| to)?\s*:?\s*(.+)|"
                 r"(?:i|we) (?:usually |always |normally )?(?:do|work on|have|go to|go|clean|wash|study|practice|"
                 r"train|cook|water|take out)\b.+)$")
LEAD = re.compile(r"^(?:i|we) (?:usually |always |normally |like to |try to )?(?:do|work on|have|go to|go for|go|attend)\s+|"
                  r"^(?:i|we) (?:usually |always |normally |like to |try to )?")
QUESTION = re.compile(r"^(what|when|how|why|where|who|which|do|does|did|is|are|can|could|should|set|change|"
                      r"make|turn|open|close|play|show|put|switch|stop|start|increase|decrease)\b")


class TaskTalk:
    """Owned by the app: voice commands, and the check-in loop that asks you for updates."""

    def __init__(self, app):
        self.app, self.cfg = app, app.cfg
        self.store = Tasks(app.cfg)
        threading.Thread(target=self._loop, daemon=True, name="task-checkins").start()

    # answers to a check-in question
    def answer(self, n: str, raw: str) -> bool:
        p = self.store.pending
        if not p or time.time() > p["until"]:
            return False
        self.store.pending = None
        activity.answered(f"task-{p['id']}")
        task = next((t for t in self.store.all() if t["id"] == p["id"]), None)
        if task is None:
            return False
        if NO.match(n):
            self.store.log(task["id"], "skipped", raw)
            nxt = self._next_due(task)
            self.app._reply(f"No worries. Next one is {nxt}." if nxt else "No worries.")
        elif YES.match(n) and task["kind"] != "project" and len(n.split()) <= 5:
            self.store.log(task["id"], "done", raw)
            streak = self._streak(task)
            self.app._reply(f"Nice, logged. That's {streak} in a row." if streak > 1 else "Nice, logged.")
        else:  # a real update (projects especially)
            self.store.log(task["id"], "update" if task["kind"] == "project" else "done", raw)
            self.app._reply("Got it, I've noted that. Keep it up.")
        return True

    def handle(self, n: str, raw: str) -> bool:
        s = self.store
        if self.answer(n, raw):
            return True
        if re.fullmatch(r"(what are|list|tell me) (all )?my (chores|projects|habits|tasks|routines)( again)?|"
                        r"what do i (track|have going on)", n):
            tasks = s.all()
            if not tasks:
                self.app._reply("You're not tracking anything yet. Say something like: I do laundry every Sunday.")
            else:
                self.app._reply("; ".join(f"{t['title']} {describe(json.loads(t['schedule']))}" for t in tasks) + ".")
                self.app.show_visual("card", "What you track | " + "; ".join(
                    f"{t['title']}: {describe(json.loads(t['schedule']))}" for t in tasks))
            return True
        if re.fullmatch(r"what (chores|tasks|do i have to do|should i do|have i got)( today)?|"
                        r"what('s| is) (on )?(for )?today('s)?( list| chores)?|any chores today", n):
            due = [t for t in s.due(dt.date.today()) if not s.logged_on(t["id"], dt.date.today())]
            self.app._reply(("Today: " + ", ".join(t["title"] for t in due) + ".") if due else
                            "Nothing left to track today.")
            return True
        m = re.fullmatch(r"how('s| is| are) (my )?(.+?) (going|coming along|doing)|"
                         r"(progress|updates?) on (my )?(.+)", n)
        if m:
            task = s.find(m.group(3) or m.group(7))
            if task:
                hist = s.history(task["id"])
                if not hist:
                    self.app._reply(f"No updates on {task['title']} yet.")
                else:
                    last = hist[0]
                    when = dt.date.fromisoformat(last["day"]).strftime("%A")
                    self.app._reply(f"Last update {when}: {(last['note'] or last['status']).rstrip('.!')}.")
                    self.app.show_visual("card", f"{task['title']} | " + "; ".join(
                        f"{dt.date.fromisoformat(h['day']).strftime('%a %d %b')}: {h['note'] or h['status']}"
                        for h in hist))
                return True
        m = re.fullmatch(r"(?:i )?(?:finished|did|completed|done with|just did) (?:the |my )?(.+?)( today| already)?", n)
        if m and s.all():
            task = s.find(m.group(1))
            if task:
                s.log(task["id"], "done", raw)
                activity.answered(f"task-{task['id']}")
                self.app._reply(f"Nice, logged {task['title']}.")
                return True
        m = re.fullmatch(r"(?:update on|progress on|for) (?:my |the )?(.+?)[: ,]+(.{6,})", n)
        if m:
            task = s.find(m.group(1))
            if task:
                s.log(task["id"], "update", m.group(2))
                self.app._reply(f"Noted for {task['title']}.")
                return True
        m = re.fullmatch(r"(?:stop tracking|remove|delete|forget|cancel) (?:the |my )?(?:chore |project |habit |task )?(.+)", n)
        if m:
            task = s.find(m.group(1))
            if task:
                s.remove(task["id"])
                self.app._reply(f"Stopped tracking {task['title']}.")
                return True
        sched, phrase = parse_schedule(n)
        m = ADD.match(n)
        short_item = phrase and 0 < n.find(phrase) and len(n.split()) <= 10  # "gym monday and friday at 6"
        # Needs a real request: "add / remind me to ...", or a short "I ... every Monday". A long ramble that
        # happens to say "daily" is someone talking, not a task.
        if sched and not QUESTION.match(n) and (m or (re.search(r"\b(i|we)\b", n) and len(n.split()) <= 14)
                                                or short_item):
            if True:
                body = (m.group(2) if m and m.group(2) else n)
                body = re.sub(re.escape(phrase), " ", body).strip(" ,") if phrase else body
                body = re.sub(r"\s+", " ", body)
                title = LEAD.sub("", body).strip()
                title = re.sub(r"\b(at \d{1,2}(:\d{2})?( ?[ap] ?m)?|in the (morning|afternoon|evening|night)|"
                               r"every|each|on|the)\s*$", "", title).strip(" ,")
                title = re.sub(r"\s+(at \d{1,2}(:\d{2})?( ?[ap] ?m)?|in the (morning|afternoon|evening|night))\b", "",
                               title).strip(" ,")
                title = re.sub(r"^(my|the|a|an|to)\s+", "", title)
                kind = m.group(1) if m and m.group(1) in ("chore", "project", "habit") else \
                    "project" if PROJECT_WORDS.search(n) else "chore" if CHORE_WORDS.search(n) else "habit"
                if not title or len(title.split()) > 8:
                    return False
                s.add(title, kind, sched)
                self.app._reply(f"Got it. I'll keep track of {title} {describe(sched)} and check in with you.")
                return True
        return False

    # --- background check-ins ---------------------------------------------------------------------------------
    def _next_due(self, task) -> str | None:
        s = json.loads(task["schedule"])
        for i in range(1, 32):
            d = dt.date.today() + dt.timedelta(days=i)
            if is_due(s, d):
                return "tomorrow" if i == 1 else d.strftime("%A")
        return None

    def _streak(self, task) -> int:
        n = 0
        for h in self.store.history(task["id"], 30):
            if h["status"] != "done":
                break
            n += 1
        return n

    def _user_here(self) -> bool:
        from batsy import idle

        away = idle.idle_ms()
        return away is None or away < 120_000

    def _loop(self) -> None:
        while True:
            time.sleep(30)
            try:
                self._tick()
            except Exception:
                pass

    def _tick(self) -> None:
        s, today, now = self.store, dt.date.today(), dt.datetime.now().strftime("%H:%M")
        busy = self.app.turn_lock.locked() or not self.app.speaker.idle() or self.app.routines.capturing
        for t in s.due(today):
            if s.logged_on(t["id"], today):
                continue
            sched = json.loads(t["schedule"])
            if sched.get("time"):
                h, m = map(int, sched["time"].split(":"))
                remind = (dt.datetime.combine(today, dt.time(h, m)) -
                          dt.timedelta(minutes=self.cfg.get("tasks.remind_minutes_before"))).strftime("%H:%M")
                key = (t["id"], today.isoformat(), "remind")
                if remind <= now < sched["time"] and key not in s.asked:
                    s.asked.add(key)
                    self.app.announce(f"Reminder: {t['title']} at {describe({'days': [], 'time': sched['time']}).split(' at ')[-1]}.")
            key = (t["id"], today.isoformat(), "checkin")
            if now >= s.checkin_time(t) and key not in s.asked:
                q = s.question(t)
                activity.asking(f"task-{t['id']}", q)  # shows on the screensaver until answered
                if busy or s.pending or not self._user_here():
                    continue  # ask once you're back at the laptop
                s.asked.add(key)
                s.pending = {"id": t["id"], "until": time.time() + 300}
                self.app.ask_user(q)
                return  # one question at a time

    def morning_line(self) -> str:
        due = self.store.due(dt.date.today())
        if not due:
            return ""
        chores = [t["title"] for t in due if t["kind"] != "project"]
        projects = [t["title"] for t in due if t["kind"] == "project"]
        bits = []
        if chores:
            bits.append("to do: " + ", ".join(chores))
        if projects:
            bits.append("project time: " + ", ".join(projects))
        return " Also " + "; ".join(bits) + "."
