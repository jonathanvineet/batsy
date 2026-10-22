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


@handles(r"how long (?:have you|has the laptop|has the computer) been (?:on|running|up)", r"uptime")
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


@handles(r"(?:set |start )?(?:a )?timer (?:for )?(.+?)(?: called (.+))?", r"(.+?) timer",
         r"remind me in (.+?) to (.+)", r"remind me to (.+?) in (.+)")
def _timer(app, m, text):
    pat = m.re.pattern
    if "remind me to" in pat:
        what, when = m.group(1), m.group(2)
    elif "remind me in" in pat:
        when, what = m.group(1), m.group(2)
    else:
        when, what = m.group(1), (m.group(2) if m.lastindex and m.lastindex >= 2 else None)
    secs = _duration(when)
    if not secs:
        return None
    message = f"Reminder: {what}." if what else "Your timer is done."
    out, err = app.registry.execute("set_timer", {"seconds": secs, "message": message})
    if err:
        return None
    mins = secs / 60
    span = f"{mins:g} minute{'s' if mins != 1 else ''}" if secs >= 60 else f"{secs:g} seconds"
    return f"Okay, I'll remind you to {what} in {span}." if what else f"Timer set for {span}."


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
