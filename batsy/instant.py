"""Instant answers: everyday requests handled by plain code, no AI, in milliseconds.

Small talk, time and dates (here and in other cities), weather, unit conversions, laptop status, timers, coin
flips, dice, spelling and jokes. `answer(app, n, text)` returns what to say, or None to let something else
handle it. Patterns match the normalised text (lowercase, no punctuation; see app._norm).
"""
import datetime as dt
import json
import random
import re
import socket
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo, available_timezones

import psutil

POLITE = r"(?:(?:hey|ok|okay|so|um|uh|and|batsy|please|can you|could you|would you|will you|tell me|do you know)\s+)*"
HANDLERS: list[tuple[re.Pattern, callable]] = []


def handles(*patterns: str):
    """Register a handler for one or more full-match patterns (polite openers and a trailing 'please' allowed)."""
    def deco(fn):
        for pat in patterns:
            HANDLERS.append((re.compile(rf"^{POLITE}(?:{pat})(?: please| batsy| now)*$"), fn))
        return fn
    return deco


def answer(app, n: str, text: str) -> str | None:
    said = _weather_anyhow(app, n)  # speech-to-text garbles questions; weather words are enough
    if said:
        return said
    for rx, fn in HANDLERS:
        m = rx.match(n)
        if m:
            said = fn(app, m, text)
            if said:
                return said
    return None


# --- small talk -------------------------------------------------------------------------------------------------
def _pick(options: list[str]) -> str:
    return random.choice(options)


@handles(r"how are you(?: doing)?(?: today| tonight| this morning)?", r"how(?:'s| is) it going", r"how have you been",
         r"how(?:'s| is) your day(?: going)?", r"how are things", r"what(?:'s| is) up", r"whats up", r"sup")
def _how_are_you(app, m, text):
    return _pick(["I'm good, thanks.", "Doing well.", "All good here.", "Running smoothly."])  # no question back


@handles(r"(?:hi|hello|hey|hiya|yo|howdy)(?: there)?(?: batsy)?")
def _hello(app, m, text):
    hour = time.localtime().tm_hour
    part = "morning" if 5 <= hour < 12 else "afternoon" if hour < 17 else "evening"
    return _pick([f"Hey. Good {part}.", "Hi.", "Hey there."])


@handles(r"(?:thanks|thank you|thank you so much|thanks a lot|cheers|appreciate it)(?: batsy)?")
def _thanks(app, m, text):
    return _pick(["Any time.", "You're welcome.", "Happy to help."])


@handles(r"(?:are you there|are you listening|you there|can you hear me)")
def _there(app, m, text):
    return "I'm here."


@handles(r"(?:who are you|what(?:'s| is) your name|what are you|introduce yourself)")
def _who(app, m, text):
    return "I'm Batsy, your assistant on this laptop. I run here, and ask Claude when something needs real thinking."


@handles(r"(?:what can you do|what do you do|help|what are your skills|how can you help(?: me)?)")
def _what_can(app, m, text):
    return ("I can answer questions, check the time and weather, do maths and conversions, set timers, open apps, "
            "play music, look through the camera, show pictures and 3D models, and learn new tricks when you ask.")


@handles(r"(?:good job|well done|nice(?: one| work)?|great(?: job)?|awesome|perfect|cool|nice job)")
def _praise(app, m, text):
    return _pick(["Thanks.", "Glad that worked.", "Cheers."])


@handles(r"(?:i love you|love you)")
def _love(app, m, text):
    return "That's kind. I'm rather fond of you too."


@handles(r"(?:tell me a joke|say something funny|make me laugh|(?:tell me )?another joke|joke)")
def _joke(app, m, text):
    return random.choice(JOKES)


JOKES = [
    "Why do programmers prefer dark mode? Because light attracts bugs.",
    "I told my computer I needed a break. It said: no problem, I'll go to sleep.",
    "Why did the Arduino break up with the breadboard? It felt there were too many connections.",
    "There are ten kinds of people: those who understand binary and those who don't.",
    "Why was the resistor so calm? It just went with the current.",
    "A SQL query walks into a bar, goes up to two tables and asks: can I join you?",
    "Why don't bats ever get lost? They always know where they're echo-located.",
    "I would tell you a UDP joke, but you might not get it.",
    "Why did the scarecrow win an award? He was outstanding in his field.",
    "Why do Java developers wear glasses? Because they don't C sharp.",
    "My 3D printer and I have a lot in common. We both work best in layers.",
    "Why was the math book sad? It had too many problems.",
    "How many programmers does it take to change a light bulb? None, that's a hardware problem.",
    "I'm reading a book about anti-gravity. It's impossible to put down.",
    "Why did the capacitor get kicked out of the party? It wouldn't stop discharging.",
]


# --- time and dates ------------------------------------------------------------------------------------------
def _city_zone(city: str) -> ZoneInfo | None:
    key = city.strip().lower().replace(" ", "_")
    aliases = {"new_york": "America/New_York", "nyc": "America/New_York", "la": "America/Los_Angeles",
               "los_angeles": "America/Los_Angeles", "san_francisco": "America/Los_Angeles",
               "london": "Europe/London", "uk": "Europe/London", "england": "Europe/London", "india": "Asia/Kolkata",
               "chennai": "Asia/Kolkata", "mumbai": "Asia/Kolkata", "delhi": "Asia/Kolkata", "bangalore": "Asia/Kolkata",
               "bengaluru": "Asia/Kolkata", "japan": "Asia/Tokyo", "dubai": "Asia/Dubai", "china": "Asia/Shanghai",
               "beijing": "Asia/Shanghai", "germany": "Europe/Berlin", "france": "Europe/Paris",
               "australia": "Australia/Sydney", "singapore": "Asia/Singapore", "seattle": "America/Los_Angeles",
               "chicago": "America/Chicago", "texas": "America/Chicago", "canada": "America/Toronto",
               "korea": "Asia/Seoul", "russia": "Europe/Moscow", "brazil": "America/Sao_Paulo"}
    if key in aliases:
        return ZoneInfo(aliases[key])
    for zone in available_timezones():
        if zone.split("/")[-1].lower() == key:
            return ZoneInfo(zone)
    return None


def _clock(t: dt.datetime) -> str:
    return t.strftime("%-I:%M %p").replace("AM", "a.m.").replace("PM", "p.m.")


@handles(r"what(?:'s| is) the time(?: now)?", r"what time is it(?: now)?", r"time", r"(?:tell me )?the time",
         r"do you have the time")
def _time(app, m, text):
    return f"It's {_clock(dt.datetime.now())}"


@handles(r"what time is it in ([a-z ]+)", r"what(?:'s| is) the time in ([a-z ]+)", r"time in ([a-z ]+)")
def _time_in(app, m, text):
    zone = _city_zone(m.group(1))
    if zone is None:
        return None
    return f"It's {_clock(dt.datetime.now(zone))} in {m.group(1).strip().title()}."  # ends "p.m. in Tokyo."


@handles(r"what(?:'s| is) (?:the date|today's date|todays date|the date today)", r"what day is (?:it|today)",
         r"what(?:'s| is) today", r"what date is it(?: today)?", r"which day is today")
def _date(app, m, text):
    return dt.date.today().strftime("It's %A, %B %-d.")


@handles(r"what(?:'s| is) (?:the date )?tomorrow(?:'s date)?", r"what day is tomorrow")
def _tomorrow(app, m, text):
    return (dt.date.today() + dt.timedelta(days=1)).strftime("Tomorrow is %A, %B %-d.")


MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}
HOLIDAYS = {"christmas": (12, 25), "new year": (1, 1), "new years": (1, 1), "halloween": (10, 31),
            "valentines day": (2, 14), "valentine's day": (2, 14), "diwali": None}


def _parse_day(s: str) -> dt.date | None:
    s = s.strip().replace("the ", "")
    if s in HOLIDAYS and HOLIDAYS[s]:
        month, day = HOLIDAYS[s]
    else:
        m = re.fullmatch(r"([a-z]+) (\d{1,2})(?:st|nd|rd|th)?|(\d{1,2})(?:st|nd|rd|th)? (?:of )?([a-z]+)", s)
        if not m:
            return None
        name, num = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        if name not in MONTHS:
            return None
        month, day = MONTHS[name], int(num)
    today = dt.date.today()
    try:
        d = dt.date(today.year, month, day)
    except ValueError:
        return None
    return d if d >= today else dt.date(today.year + 1, month, day)


@handles(r"how many days (?:until|till|to|left until|left till) ([a-z0-9' ]+)", r"how long (?:until|till) ([a-z0-9' ]+)",
         r"days (?:until|till) ([a-z0-9' ]+)")
def _days_until(app, m, text):
    d = _parse_day(m.group(1))
    if d is None:
        return None
    days = (d - dt.date.today()).days
    return "That's today." if days == 0 else f"{days} day{'s' if days != 1 else ''}, on {d.strftime('%A, %B %-d')}."


@handles(r"what day (?:is|was|will be) ([a-z]+ \d{1,2}(?:st|nd|rd|th)?|\d{1,2}(?:st|nd|rd|th)? (?:of )?[a-z]+)")
def _weekday_of(app, m, text):
    d = _parse_day(m.group(1))
    return d.strftime("That's a %A.") if d else None


# --- weather --------------------------------------------------------------------------------------------------
WEATHER_CODES = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy",
                 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
                 71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers", 81: "rain showers",
                 82: "heavy showers", 95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail"}


def _get(url: str, params: dict) -> dict:
    with urllib.request.urlopen(url + "?" + urllib.parse.urlencode(params), timeout=8) as r:
        return json.loads(r.read())


@handles(r"(?:what(?:'s| is) the |how(?:'s| is) the )?weather(?: like)?(?: (today|tomorrow|now))?(?: in ([a-z ]+?))?"
         r"(?: (today|tomorrow|now))?", r"(?:is it|will it) (?:going to )?(rain|be hot|be cold|be sunny)"
                                        r"(?: (today|tomorrow))?(?: in ([a-z ]+))?",
         r"(?:what(?:'s| is) the )?temperature (?:outside|today|in ([a-z ]+))")
def _weather(app, m, text):
    groups = [g for g in m.groups() if g]
    when = "tomorrow" if "tomorrow" in groups else "now"
    city = next((g for g in groups if g not in ("today", "tomorrow", "now", "rain", "be hot", "be cold",
                                                 "be sunny")), None) or app.cfg.get("weather.city")
    try:
        place = _get("https://geocoding-api.open-meteo.com/v1/search", {"name": city, "count": 1})["results"][0]
        w = _get("https://api.open-meteo.com/v1/forecast", {
            "latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto",
            "current": "temperature_2m,weather_code,apparent_temperature",
            "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max"})
    except (OSError, ValueError, KeyError, IndexError):
        return f"I couldn't get the weather for {city} right now."
    name = place["name"]
    if when == "tomorrow":
        d = w["daily"]
        return (f"Tomorrow in {name}: {WEATHER_CODES.get(d['weather_code'][1], 'mixed')}, "
                f"{round(d['temperature_2m_min'][1])} to {round(d['temperature_2m_max'][1])} degrees, "
                f"{d['precipitation_probability_max'][1]} percent chance of rain.")
    c, d = w["current"], w["daily"]
    return (f"It's {round(c['temperature_2m'])} degrees and {WEATHER_CODES.get(c['weather_code'], 'mixed')} in {name}, "
            f"with a high of {round(d['temperature_2m_max'][0])} and a {d['precipitation_probability_max'][0]} "
            f"percent chance of rain today.")


def _weather_anyhow(app, n: str) -> str | None:
    """Any sentence about the weather, however it came out of speech-to-text: "what will the we what will be the
    weather tomorrow", "do i need an umbrella in london"."""
    if not re.search(r"\b(weather|forecast|rain|raining|rainy|umbrella|sunny|cloudy|snow|snowing)\b", n):
        return None
    if re.search(r"\b(screensaver|screen saver|widget|add|remove)\b", n):  # "add the weather to the screensaver"
        return None
    when = "tomorrow" if "tomorrow" in n else "now"
    m = re.search(r"\b(?:in|for|at) ([a-z]+(?: [a-z]+)?)$", n.replace(" tomorrow", "").replace(" today", "").strip())
    city = m.group(1) if m and m.group(1) not in ("the", "it", "general") else None
    return _weather(app, _Groups(when, city), "")


class _Groups:
    """Stand-in for a regex match, so _weather can be called directly."""

    def __init__(self, *groups):
        self._g = groups

    def groups(self):
        return self._g


# --- unit conversions -----------------------------------------------------------------------------------------
UNITS = {  # name -> (dimension, factor to the base unit)
    "km": ("len", 1000), "kilometer": ("len", 1000), "kilometre": ("len", 1000), "m": ("len", 1), "meter": ("len", 1),
    "metre": ("len", 1), "cm": ("len", 0.01), "centimeter": ("len", 0.01), "centimetre": ("len", 0.01),
    "mm": ("len", 0.001), "millimeter": ("len", 0.001), "millimetre": ("len", 0.001), "mile": ("len", 1609.344),
    "yard": ("len", 0.9144), "foot": ("len", 0.3048), "feet": ("len", 0.3048), "ft": ("len", 0.3048),
    "inch": ("len", 0.0254), "inches": ("len", 0.0254), "in": ("len", 0.0254),
    "kg": ("mass", 1), "kilogram": ("mass", 1), "kilo": ("mass", 1), "g": ("mass", 0.001), "gram": ("mass", 0.001),
    "pound": ("mass", 0.45359237), "lb": ("mass", 0.45359237), "lbs": ("mass", 0.45359237), "ounce": ("mass", 0.0283495),
    "oz": ("mass", 0.0283495), "ton": ("mass", 1000), "tonne": ("mass", 1000),
    "liter": ("vol", 1), "litre": ("vol", 1), "l": ("vol", 1), "ml": ("vol", 0.001), "milliliter": ("vol", 0.001),
    "millilitre": ("vol", 0.001), "gallon": ("vol", 3.785411784), "cup": ("vol", 0.2365882),
    "kmh": ("speed", 1 / 3.6), "kph": ("speed", 1 / 3.6), "mph": ("speed", 0.44704), "mps": ("speed", 1),
    "byte": ("data", 1), "kb": ("data", 1e3), "mb": ("data", 1e6), "gb": ("data", 1e9), "tb": ("data", 1e12),
    "second": ("time", 1), "minute": ("time", 60), "hour": ("time", 3600), "day": ("time", 86400),
    "week": ("time", 604800), "year": ("time", 31557600),
}
TEMPS = {"c": "c", "celsius": "c", "centigrade": "c", "f": "f", "fahrenheit": "f", "k": "k", "kelvin": "k"}


def _unit(word: str) -> str:
    w = word.strip().replace("degrees ", "").replace("per hour", "ph").replace("kilometers ph", "kmh")
    w = {"miles ph": "mph", "kilometres ph": "kmh", "km ph": "kmh"}.get(w, w)
    if w in UNITS or w in TEMPS:
        return w
    return w[:-1] if w.endswith("s") and (w[:-1] in UNITS or w[:-1] in TEMPS) else w


def _num(s: str) -> float | None:
    from batsy.calc import _words_to_numbers
    s = _words_to_numbers(s.strip()).replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _fmt(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:.3g}" if abs(v) < 10 else f"{v:.1f}".rstrip("0").rstrip(".")


@handles(r"(?:convert |what(?:'s| is) |how many [a-z ]+ (?:is|are|in) )?([0-9.,]+|[a-z ]+?) ([a-z ]+?) (?:in|to|into|as) ([a-z ]+)",
         r"how many ([a-z ]+) (?:are )?in (?:a |an |one )?([0-9.,]*) ?([a-z ]+)")
def _convert(app, m, text):
    if m.re.pattern.find("how many ([a-z ]+)") >= 0 and m.lastindex == 3 and not re.match(r"[0-9]", m.group(1)):
        target, qty, src = m.group(1), m.group(2) or "1", m.group(3)
    else:
        qty, src, target = m.group(1), m.group(2), m.group(3)
    value = _num(qty)
    src, target = _unit(src), _unit(target)
    if value is None:
        return None
    if src in TEMPS and target in TEMPS:
        a, b = TEMPS[src], TEMPS[target]
        c = value if a == "c" else (value - 32) * 5 / 9 if a == "f" else value - 273.15
        out = c if b == "c" else c * 9 / 5 + 32 if b == "f" else c + 273.15
        names = {"c": "Celsius", "f": "Fahrenheit", "k": "Kelvin"}
        return f"{_fmt(value)} degrees {names[a]} is {_fmt(out)} degrees {names[b]}."
    if src not in UNITS or target not in UNITS or UNITS[src][0] != UNITS[target][0]:
        return None
    out = value * UNITS[src][1] / UNITS[target][1]
    return f"{_fmt(value)} {_say_unit(src, value)} is {_fmt(out)} {_say_unit(target, out)}."


SPOKEN = {"km": "kilometre", "m": "metre", "cm": "centimetre", "mm": "millimetre", "ft": "foot", "in": "inch",
          "kg": "kilogram", "g": "gram", "lb": "pound", "lbs": "pound", "oz": "ounce", "l": "litre", "ml": "millilitre",
          "kmh": "kilometre per hour", "kph": "kilometre per hour", "mph": "mile per hour", "mps": "metre per second",
          "kb": "kilobyte", "mb": "megabyte", "gb": "gigabyte", "tb": "terabyte", "kilo": "kilogram", "feet": "foot",
          "inches": "inch"}


def _say_unit(unit: str, value: float) -> str:
    word = SPOKEN.get(unit, unit)
    if abs(value - 1) < 1e-9:
        return word
    head, _, tail = word.partition(" per ")  # "mile per hour" -> "miles per hour"
    head = {"foot": "feet", "inch": "inches"}.get(head, head + "s")
    return head + (f" per {tail}" if tail else "")


# --- the laptop -----------------------------------------------------------------------------------------------
@handles(r"(?:what(?:'s| is) (?:my |the )?)?battery(?: level| percentage| status)?", r"how much battery(?: do i have)?(?: left)?",
         r"(?:how(?:'s| is) )?(?:my |the )?battery(?: doing)?")
def _battery(app, m, text):
    b = psutil.sensors_battery()
    if b is None:
        return "I can't find a battery."
    state = "charging" if b.power_plugged else "on battery"
    return f"Battery is at {b.percent:.0f} percent, {state}."


def _cpu_temp() -> float | None:
    temps = psutil.sensors_temperatures()
    for key in ("coretemp", "k10temp", "acpitz"):
        if temps.get(key):
            return max(t.current for t in temps[key])
    return None


@handles(r"(?:what(?:'s| is) (?:the |my )?)?(?:cpu |laptop |computer )?temp(?:erature)?(?: of (?:the |my )?(?:cpu|laptop))?",
         r"how hot is (?:the |my )?(?:laptop|cpu|computer)", r"is (?:the |my )?laptop (?:hot|overheating)")
def _temp(app, m, text):
    t = _cpu_temp()
    if t is None:
        return None
    feel = "that's hot" if t >= 85 else "warm" if t >= 70 else "fine"
    return f"The CPU is at {t:.0f} degrees, {feel}."


@handles(r"(?:what(?:'s| is) (?:the |my )?)?cpu(?: usage| load)?", r"how busy is (?:the |my )?(?:cpu|laptop|computer)",
         r"(?:how(?:'s| is) )?(?:the |my )?(?:system|laptop|computer)(?: doing| status)?")
def _cpu(app, m, text):
    load = psutil.cpu_percent(interval=0.4)
    mem = psutil.virtual_memory()
    t = _cpu_temp()
    return (f"CPU is at {load:.0f} percent, memory {mem.percent:.0f} percent used"
            + (f", and it's {t:.0f} degrees." if t is not None else "."))


@handles(r"(?:how much )?(?:ram|memory)(?: usage| left| free| am i using)?", r"how much (?:ram|memory) (?:is )?(?:left|free|used)")
def _memory(app, m, text):
    mem = psutil.virtual_memory()
    return f"{mem.used / 2**30:.1f} of {mem.total / 2**30:.1f} gigabytes used, {mem.percent:.0f} percent."


@handles(r"(?:how much )?(?:disk|storage|disk space|space)(?: left| free)?(?: do i have)?(?: left)?",
         r"how much (?:disk |storage )?space (?:do i have )?(?:left|free)")
def _disk(app, m, text):
    d = psutil.disk_usage("/home")
    return f"{d.free / 2**30:.0f} gigabytes free out of {d.total / 2**30:.0f}."


@handles(r"(?:what(?:'s| is) )?my (?:ip|ip address)", r"ip address")
def _ip(app, m, text):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 80))
            return f"Your local IP is {s.getsockname()[0].replace('.', ' dot ')}."
    except OSError:
        return "I'm not connected to a network."


@handles(r"(?:have you|did you) (?:just )?(?:restart|restarted|reboot|rebooted|update|updated)(?: yourself)?(?: already| yet)?",
         r"when did you (?:last )?(?:restart|start|update|reboot)(?: yourself)?",
         r"how long have you been (?:on|running|up|awake|online)", r"(?:what(?:'s| is) )?your uptime",
         r"are you (?:back|updated|restarted)")
def _batsy_uptime(app, m, text):
    from batsy import lifecycle

    return lifecycle.describe()


@handles(r"how long (?:has the laptop|has the computer|has this laptop) been (?:on|running|up)", r"uptime")
def _uptime(app, m, text):
    secs = time.time() - psutil.boot_time()
    h, mnt = int(secs // 3600), int(secs % 3600 // 60)
    return f"The laptop has been on for {h} hour{'s' if h != 1 else ''} and {mnt} minute{'s' if mnt != 1 else ''}."


# --- timers ---------------------------------------------------------------------------------------------------
def _duration(s: str) -> float | None:
    total = 0.0
    for num, unit in re.findall(r"([0-9.]+|[a-z]+(?: [a-z]+)?) (seconds?|secs?|minutes?|mins?|hours?|hrs?)", s):
        v = _num(num.replace("a ", "1 ").replace("an ", "1 ")) if num not in ("a", "an") else 1.0
        if v is None:
            return None
        total += v * (3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1)
    if re.search(r"\bhalf an hour\b", s):
        total += 1800
    return total or None


@handles(r"(?:set |start )?(?:a )?timer (?:for )?(.+?)(?: called (.+))?", r"(.+?) timer")
def _timer(app, m, text):
    when, what = m.group(1), (m.group(2) if m.lastindex and m.lastindex >= 2 else None)
    secs = _duration(when)
    if not secs:
        return None
    from batsy.tools import monitor

    monitor.add_reminder(time.time() + secs, what)
    return f"Okay, I'll remind you to {_yours(what)} {_span(secs)}." if what else f"Timer set for {_span(secs)[3:]}."


# --- reminders at a time ("remind me at 5 pm to call mom", "remind me tomorrow morning to submit the assignment") --
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
PART_OF_DAY = {"morning": (9, 0), "afternoon": (14, 0), "evening": (18, 0), "night": (20, 0), "tonight": (20, 0)}
_MONTH = "(?:" + "|".join(MONTHS) + ")"
_CLOCK = (r"(?:(?:half|quarter) (?:past|to) [a-z0-9]+|\d{1,4}(?: \d{2})?|noon|midday|midnight|[a-z]+(?: [a-z]+)?)"
          r"(?: ?(?:am|pm|a m|p m|oclock|o'clock))?")
WHEN = re.compile(
    rf"\b(?:(?:on |this |next |coming )?(?P<wd>{'|'.join(WEEKDAYS)})|(?P<rel>day after tomorrow|tomorrow|today)|"
    rf"(?:(?:this|in the|tomorrow|at) )?(?P<part>morning|afternoon|evening|night|tonight)|"
    rf"(?:at|by|around|for) (?P<clock>{_CLOCK})|"
    rf"(?:on )?(?:the )?(?P<date>\d{{1,2}}(?:st|nd|rd|th)? (?:of )?{_MONTH}|{_MONTH} \d{{1,2}}(?:st|nd|rd|th)?))\b")
_PRONOUNS = {"my": "your", "me": "you", "i": "you", "i'm": "you're", "im": "you're", "myself": "yourself",
             "mine": "yours", "am": "are", "your": "my"}


def _yours(what: str | None) -> str:
    """'call my mom' -> 'call your mom' (it's said back to you)."""
    return " ".join(_PRONOUNS.get(w, w) for w in (what or "").split())


def _span(secs: float) -> str:
    secs = round(secs)
    mins = round(secs / 60)
    if secs < 60 or secs < 300 and secs % 60:
        return f"in {secs} seconds"
    if mins < 90:
        return f"in {mins:g} minute{'s' if mins != 1 else ''}"
    h = mins / 60
    return f"in {h:.1f}".rstrip("0").rstrip(".") + " hours"


def _clock_of(s: str) -> tuple[int, int, str] | None:
    """'5 pm' -> (5, 0, 'pm'); '530' -> (5, 30, ''); 'noon' -> (12, 0, 'pm'); 'five thirty' -> (5, 30, '')."""
    s = s.strip().replace("o'clock", "").replace("oclock", "").replace("a m", "am").replace("p m", "pm").strip()
    if s in ("noon", "midday"):
        return 12, 0, "pm"
    q = re.fullmatch(r"(half|quarter) (past|to) ([a-z0-9]+)(?: (am|pm))?", s)
    if q:
        base = _clock_of(q.group(3) + (" " + q.group(4) if q.group(4) else ""))
        if base is None:
            return None
        mins = 30 if q.group(1) == "half" else 15
        h, mi = (base[0], mins) if q.group(2) == "past" else ((base[0] - 1) % 24, 60 - mins)
        return h, mi, base[2]
    if s == "midnight":
        return 0, 0, "am"
    ap = ""
    m = re.search(r" ?(am|pm)$", s)
    if m:
        ap, s = m.group(1), s[:m.start()].strip()
    from batsy.calc import _words_to_numbers

    parts = [_words_to_numbers(w) for w in s.split()]  # word by word: "five thirty" is 5:30, not 35
    try:
        nums = [int(float(x)) for x in parts]
    except ValueError:
        return None
    if len(nums) == 1 and nums[0] >= 100:  # "530" / "1730" (speech-to-text drops the colon)
        nums = [nums[0] // 100, nums[0] % 100]
    if len(nums) == 1:
        nums.append(0)
    if len(nums) != 2 or not (0 <= nums[0] <= 23 and 0 <= nums[1] <= 59):
        return None
    return nums[0], nums[1], ap


def _when(rest: str) -> tuple[dt.datetime, list[tuple[int, int]]] | None:
    """Find the time in a reminder. Returns (when, spans of the text that said it) or None."""
    now = dt.datetime.now()
    day, clock, part, spans, explicit_day = now.date(), None, None, [], False
    for m in WHEN.finditer(rest):
        if m.group("clock"):
            c = _clock_of(m.group("clock"))
            span = m.span()
            if c is None and " " in m.group("clock"):  # "at seven to call home": the clock is just "seven"
                first = m.group("clock").split()[0]
                c, span = _clock_of(first), (m.start(), m.start("clock") + len(first))
            if c is None:
                continue
            clock = c
            spans.append(span)
            continue
        elif m.group("wd"):
            ahead = (WEEKDAYS.index(m.group("wd")) - now.weekday()) % 7
            if m.group(0).startswith("next ") and ahead == 0:
                ahead = 7
            day, explicit_day = now.date() + dt.timedelta(days=ahead), True
        elif m.group("rel"):
            day = now.date() + dt.timedelta(days={"today": 0, "tomorrow": 1, "day after tomorrow": 2}[m.group("rel")])
            explicit_day = True
        elif m.group("part"):
            part = m.group("part")
            if m.group(0).startswith("tomorrow"):
                day, explicit_day = now.date() + dt.timedelta(days=1), True
        elif m.group("date"):
            d = _parse_day(m.group("date").replace("the ", ""))
            if d is None:
                continue
            day, explicit_day = d, True
        spans.append(m.span())
    if not spans:
        return None
    if clock:
        h, mi, ap = clock
        if ap == "pm" and h < 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        elif not ap and h <= 12:
            if part in ("afternoon", "evening", "night", "tonight") and h < 12:
                h += 12
            elif part != "morning":  # "at 5": the next 5 o'clock that's sensible
                options = [h, h + 12] if h < 12 else [12]
                if day == now.date() and not explicit_day:
                    later = [x for x in options if dt.datetime.combine(day, dt.time(x % 24, mi)) > now]
                    h = later[0] if later else options[0]
                else:
                    h = h + 12 if h < 7 else h
        when = dt.datetime.combine(day, dt.time(h % 24, mi))
    else:
        h, mi = PART_OF_DAY.get(part, (9, 0)) if (part or explicit_day) else (9, 0)
        when = dt.datetime.combine(day, dt.time(h, mi))
    if when <= now:
        if explicit_day and day > now.date():
            return None
        when += dt.timedelta(days=1)  # "at 7 am" said at 8 am: tomorrow
    return when, spans


def _say_when(when: dt.datetime) -> str:
    t = when.strftime("%-I:%M %p").replace(":00", "")
    days = (when.date() - dt.date.today()).days
    if days == 0:
        return f"at {t}" if when.hour < 18 or when.hour >= 22 else f"tonight at {t}" if when.hour >= 20 else f"at {t}"
    if days == 1:
        return f"tomorrow at {t}"
    if days < 7:
        return f"on {when.strftime('%A')} at {t}"
    return f"on {when.strftime('%A, %B %-d')} at {t}"


@handles(r"snooze(?: (?:it|that))?(?: for (.+))?", r"remind me (?:again|later)(?: in (.+))?")
def _snooze(app, m, text):
    from batsy.tools import monitor

    last = monitor.last_fired
    if not last or time.time() - last.get("fired", 0) > 1800:
        return None
    secs = _duration(m.group(1)) if m.group(1) else 600
    if not secs:
        return None
    monitor.add_reminder(time.time() + secs, last.get("what") or None, last.get("message"))
    monitor.last_fired = None
    return f"Okay, again {_span(secs)}."


@handles(r"(?:can you |please )?remind me (.+)", r"(?:set|create|add|make)(?: me)? (?:a |an )?reminder (.+)",
         r"(?:a )?reminder (?:for|at|on|to) (.+)")
def _remind(app, m, text):
    rest = re.sub(r"\b(?:please|thanks|thank you)$", "", m.group(1)).strip()
    if re.search(r"\b(every|daily|weekly|monthly|each|weekdays|weekends|nightly)\b", rest):
        return None  # a recurring one: the chores/habits tracker handles that
    from batsy.tools import monitor

    rel = re.fullmatch(r"in (.+?) (?:to|about|that|of) (.+)", rest) or re.fullmatch(r"(?:to |about |that |of )?(.+?) in (.+)", rest)
    if rel:
        when_s, what = (rel.group(1), rel.group(2)) if rest.startswith("in ") else (rel.group(2), rel.group(1))
        secs = _duration(when_s)
        if secs:
            what = _as_said(what, text)
            monitor.add_reminder(time.time() + secs, what)
            return f"Okay, I'll remind you to {_yours(what)} {_span(secs)}."
    found = _when(rest)
    if found is None:
        return None
    when, spans = found
    what = rest
    for a, b in sorted(spans, reverse=True):
        what = what[:a] + " " + what[b:]
    what = re.sub(r"\s+", " ", what).strip()
    what = re.sub(r"^(?:for|at|on|in)\s+", "", what)
    what = re.sub(r"^(?:to|about|that|of)\s+|\s+(?:to|about|that|at|on|by)$", "", what).strip()
    about = bool(re.match(r"(?:about|of)\s", what)) or bool(re.search(r"\b(?:about|of) " + re.escape(what) + "$", rest))
    what = re.sub(r"^(?:to|about|that)\s+", "", what).strip()
    what = _as_said(what, text)
    monitor.add_reminder(when.timestamp(), (f"about {what}" if about else what) or None,
                         None if what else when.strftime("This is your %-I:%M %p reminder.").replace(":00", ""))
    said = _say_when(when)
    if not what:
        return f"Okay, reminder set for {said[3:] if said.startswith('at ') else said}."
    return f"Okay, I'll remind you {'about' if about else 'to'} {_yours(what)} {said}."


def _as_said(words: str, text: str) -> str:
    """The words as you said them ("DBMS", "Ender 3"), not lowercased: matching runs on normalised text."""
    if not words:
        return words
    rx = r"[\s,.!?'-]*".join(re.escape(w) for w in words.split())
    m = re.search(rx, text, re.I)
    return m.group(0) if m else words


@handles(r"(?:what are|list|tell me|show me|read me|read out|whats|what's|what is) (?:all )?(?:my |the )?(?:reminders|timers)"
         r"(?: (?:for )?today| left| set| coming up)?",
         r"(?:do i have )?any (?:reminders|timers)(?: (?:for )?today| left| set| coming up)?",
         r"what (?:reminders|timers) do i have(?: (?:for )?today| left| set)?",
         r"what (?:did|have) i (?:ask|asked|tell|told) you to remind me(?: about| of)?")
def _list_reminders(app, m, text):
    from batsy.tools import monitor

    items = monitor.reminders()
    if not items:
        return "You don't have any reminders set."
    lines = []
    for _, it in items:
        due = dt.datetime.fromtimestamp(it["next_run"])
        soon = it["next_run"] - time.time() < 3600
        when = _span(max(1, it["next_run"] - time.time())) if soon else _say_when(due)
        what = _yours(it["what"]) if it.get("what") else ""
        lines.append(f"{what} {when}" if what.startswith("about ") else f"to {what} {when}" if what else f"a timer {when}")
    if hasattr(app, "show_visual"):
        app.show_visual("card", "Reminders | " + "\n".join(
            f"- {re.sub(r'^(?:to|about) ', '', x)[0].upper() + re.sub(r'^(?:to|about) ', '', x)[1:]}" for x in lines))
    head = f"You have {len(lines)} reminder{'s' if len(lines) != 1 else ''}: "
    return head + (lines[0] if len(lines) == 1 else ", ".join(lines[:-1]) + " and " + lines[-1]) + "."


@handles(r"(?:cancel|delete|remove|clear|stop|forget|turn off) (?:all )?(?:of )?(?:my |the )?(?:reminders|timers)",
         r"(?:cancel|delete|remove|clear|stop|forget|turn off) (?:my |the |that )?(?:reminder|timer)"
         r"(?: (?:about|to|for|at|on|of) (.+))?")
def _cancel_reminder(app, m, text):
    from batsy.tools import monitor

    items = monitor.reminders()
    if not items:
        return "There aren't any reminders to cancel."
    if re.search(r"\b(reminders|timers)\b", m.group(0)) and not m.lastindex:
        for mid, _ in items:
            monitor.cancel(mid)
        return f"Cancelled all {len(items)} reminder{'s' if len(items) != 1 else ''}." if len(items) > 1 else "Cancelled it."
    target = (m.group(1) or "") if m.lastindex else ""
    if not target:
        if len(items) == 1:
            monitor.cancel(items[0][0])
            return "Cancelled it."
        return f"You have {len(items)} reminders. Which one? Say cancel the reminder about, and what it's for."
    want = set(target.split()) - {"the", "my", "a", "to", "about"}
    found = _when(target)
    best, best_score = None, 0.0
    for mid, it in items:
        sc = len(want & set((it.get("what") or "").split())) / max(1, len(want))
        if found and abs(it["next_run"] - found[0].timestamp()) < 120:
            sc += 1
        if sc > best_score:
            best, best_score = (mid, it), sc
    if not best or best_score < 0.34:
        return "I couldn't find a reminder like that."
    monitor.cancel(best[0])
    what = _yours(best[1].get("what") or "")
    return (f"Cancelled the reminder {what}." if what.startswith("about ") else f"Cancelled the reminder to {what}.") \
        if what else "Cancelled that timer."


# --- little tools ---------------------------------------------------------------------------------------------
@handles(r"(?:flip|toss) a coin", r"heads or tails")
def _coin(app, m, text):
    return random.choice(["Heads.", "Tails."])


@handles(r"roll (?:a |the )?(?:die|dice)", r"roll a d(\d+)")
def _dice(app, m, text):
    sides = int(m.group(1)) if m.lastindex else 6
    return f"You rolled a {random.randint(1, sides)}."


@handles(r"(?:pick|choose|give me) a (?:random )?number (?:between|from) ([0-9a-z ]+?) (?:and|to) ([0-9a-z ]+)")
def _random_number(app, m, text):
    a, b = _num(m.group(1)), _num(m.group(2))
    if a is None or b is None:
        return None
    lo, hi = sorted((int(a), int(b)))
    return f"{random.randint(lo, hi)}."


@handles(r"how do you spell ([a-z' ]+)", r"spell ([a-z' ]+)")
def _spell(app, m, text):
    word = m.group(1).strip()
    if len(word) > 30:
        return None
    return f"{word}: " + ", ".join(c.upper() for c in word if c.isalpha()) + "."
