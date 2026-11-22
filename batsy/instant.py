"""Instant answers: everyday requests handled by plain code, no AI, in milliseconds.

Small talk, time and dates (here and in other cities), weather, unit conversions, laptop status, timers, coin
flips, dice, spelling and jokes. `answer(app, n, text)` returns what to say, or None to let something else
handle it. Patterns match the normalised text (lowercase, no punctuation; see app._norm).
"""
import datetime as dt
import json
import logging
import random
import re
import socket
import threading
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo, available_timezones

import psutil

log = logging.getLogger("batsy.instant")
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
    strong = re.search(r"\b(weather|forecast|umbrella)\b", n)
    weak = re.search(r"\b(rain|raining|rainy|sunny|cloudy|snow|snowing)\b", n)
    # "sunny" alone only in a short weather question ("is it sunny", "will it rain tomorrow"), not "how much does the
    # panel heat up on a sunny day" (that's a question about panels)
    if not strong and not (weak and len(n.split()) <= 7 and re.match(r"(?:is|will|does|do|should|gonna|going to|it'?s)\b", n)):
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


# --- "are you reading the PDF?", "what are you doing?": from what Batsy is actually doing right now -----------------
@handles(r"(?:what are you|what're you|whatcha) (?:doing|working on|up to|busy with)(?: now| right now)?",
         r"are you (?:still )?(?:reading|doing|working on|busy|done|finished|thinking|slicing|writing|done with)(?: .*)?",
         r"(?:is|are) (?:it|that|the \w+(?: \w+)?) (?:done|finished|ready)(?: yet)?", r"how far (?:along )?are you(?: .*)?",
         r"what'?s (?:taking|happening)(?: so long)?")
def _what_im_doing(app, m, text):
    from batsy import activity

    snap = activity.snapshot()
    busy = [a for a in snap["agents"] if a.get("kind") not in ("monitor",)]
    if not busy:
        done = snap.get("done") or []
        return (f"Nothing right now. I last finished: {done[0]['result'] or done[0]['label']}." if done
                else "Nothing right now.")
    def one(a):
        mins = a["secs"] // 60
        took = f"{mins} minute{'s' if mins != 1 else ''}" if mins else f"{a['secs']} seconds"
        step = f": {a['step'][0].lower() + a['step'][1:]}" if a.get("step") else ""
        pct = f", about {int(a['pct'])} percent" if a.get("pct") else ""
        return f"{a['label']}{step}{pct} ({took} so far)"
    lead = "Not yet. " if re.search(r"\b(?:done|finished|ready)\b", m.string) else "Yes. " if re.match(r"are you", m.string) else ""
    return lead + "I'm " + "; and ".join(
        one(a)[0].lower() + one(a)[1:] for a in busy[:3]) + "."


# --- money: "what is 35k euros in INR", "how much is 200 dollars in rupees" (live rates, no AI) ------------------------
CURRENCY = {"eur": ("euro", "euros?|eur|€"), "usd": ("dollar", "(?:us |american )?dollars?|usd|bucks|\\$"),
            "inr": ("rupee", "(?:indian )?rupees?|inr|rs|₹"), "gbp": ("pound", "pounds?(?: sterling)?|gbp|quid|£"),
            "jpy": ("yen", "yen|jpy"), "aed": ("dirham", "dirhams?|aed"), "cny": ("yuan", "yuan|rmb|cny|renminbi"),
            "sgd": ("Singapore dollar", "singapore dollars?|sgd"), "aud": ("Australian dollar", "australian dollars?|aud"),
            "cad": ("Canadian dollar", "canadian dollars?|cad"), "chf": ("Swiss franc", "swiss francs?|francs?|chf"),
            "sar": ("riyal", "riyals?|sar"), "myr": ("ringgit", "ringgits?|myr"), "krw": ("won", "won|krw"),
            "lkr": ("Sri Lankan rupee", "sri lankan rupees?|lkr")}
_CUR = "|".join(f"(?:{v[1]})" for v in sorted(CURRENCY.values(), key=lambda x: -len(x[1])))
_rates: dict[str, tuple[float, dict]] = {}


def _cur(word: str) -> str | None:
    return next((code for code, (_, rx) in CURRENCY.items() if re.fullmatch(rx, word.strip())), None)


def _amount(s: str) -> float | None:
    """'35k', '35 k', 'thirty five thousand', '1.2 million', '2 lakh', '1.5 crore' -> the number."""
    s = s.strip().replace(",", "")
    mult = 1.0
    for word, k in (("k", 1e3), ("thousand", 1e3), ("lakh", 1e5), ("lakhs", 1e5), ("lac", 1e5), ("million", 1e6),
                    ("crore", 1e7), ("crores", 1e7), ("billion", 1e9), ("m", 1e6), ("bn", 1e9)):
        if re.search(rf"(?<=[\d ]){word}$", s) or s.endswith(" " + word):
            s, mult = s[: -len(word)].strip(), k
            break
    v = _num(s) if not re.fullmatch(r"[\d.]+", s) else float(s)
    return v * mult if v is not None else None


def _rate(src: str, dst: str) -> float | None:
    cached = _rates.get(src)
    if not cached or time.time() - cached[0] > 6 * 3600:
        try:
            with urllib.request.urlopen(f"https://open.er-api.com/v6/latest/{src.upper()}", timeout=8) as r:
                _rates[src] = (time.time(), json.loads(r.read())["rates"])
        except Exception as e:
            log.info("exchange rate: %s", e)
            return None
    return _rates[src][1].get(dst.upper())


def _money(v: float, code: str) -> str:
    name = CURRENCY[code][0]
    if code == "inr" and v >= 1e5:  # the way you'd say it: 35.9 lakh, 1.2 crore
        big = f"{v / 1e7:.2f}".rstrip("0").rstrip(".") + " crore" if v >= 1e7 else f"{v / 1e5:.2f}".rstrip("0").rstrip(".") + " lakh"
        return f"{big} rupees"
    shown = f"{v:,.0f}" if v >= 100 or v == int(v) else f"{v:,.2f}"
    return f"{shown} {name}{'' if shown in ('1', '1.00') or name.endswith('s') or code in ('jpy', 'krw') else 's'}"


@handles(rf"(?:what(?:'s| is| are)|how much (?:is|are)|convert|change)? ?(?P<amt>[0-9a-z .]+?) ?(?P<src>{_CUR}) "
         rf"(?:in|to|into|as) (?:(?:uh|um|er) )*(?P<dst>{_CUR})")
def _currency(app, m, text):
    src, dst = _cur(m["src"]), _cur(m["dst"])
    amt = _amount(re.sub(r"\b(?:uh|um|er|about|around|like)\b", " ", m["amt"]).strip())
    if not (src and dst and amt) or src == dst:
        return None
    rate = _rate(src, dst)
    if rate is None:
        return "I couldn't get today's exchange rate. Check your internet and ask again."
    out = amt * rate
    exact = f" ({out:,.0f} {dst.upper()})" if dst == "inr" and out >= 1e5 else ""
    shown_rate = f"{rate:,.2f}" if rate >= 1 else f"{rate:.4g}"
    return f"{_money(amt, src)} is about {_money(out, dst)}{exact}, at {shown_rate} per {CURRENCY[src][0]}."


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
    return " ".join(_PRONOUNS.get(w.lower(), w) if w.lower() in _PRONOUNS and (w.islower() or w in ("I", "My", "Me"))
                    else w for w in (what or "").split())


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
            late = 1 if now.hour < 4 else 0  # said at 12:30 am, "tomorrow" is the day that's just begun
            day = now.date() + dt.timedelta(days=max(0, {"today": 0, "tomorrow": 1, "day after tomorrow": 2}[m.group("rel")] - late))
            explicit_day = True
        elif m.group("part"):
            part = m.group("part")
            if m.group(0).startswith("tomorrow"):
                day, explicit_day = now.date() + dt.timedelta(days=0 if now.hour < 4 else 1), True
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
    if dt.datetime.now().hour < 4 and days in (0, 1):  # after midnight "tomorrow" is ambiguous: name the day
        return f"on {when.strftime('%A')} at {t}"
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


# "Pati, remind me..." (a misheard "Batsy" in front): one stray word before the command is fine
@handles(r"(?:[a-z]+ )?(?:can you |please )?remind me (.+)", r"(?:set|create|add|make)(?: me)? (?:a |an )?reminder (.+)",
         r"(?:a )?reminder (?:for|at|on|to) (.+)")
def _remind(app, m, text):
    rest = re.sub(r"\b(?:please|thanks|thank you)$", "", m.group(1)).strip()
    rest = re.sub(r"\b(?:uh+|um+|hmm+|mm+|mmhmm|mm hmm|mhm|er+|ah+)\b", " ", rest)  # "at uh 11 30"
    rest = re.sub(r"\s+", " ", rest).strip()
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
    if not _HAS_CLOCK.search(rest) and not re.search(r"\b(?:morning|afternoon|evening|night|tonight)\b", rest) \
            and _day_in(rest)[0] is not None:
        return None  # a day but no time ("on the 25th", "in 5 days"): a to-do under the clock for that day
    found = _when(rest)
    if found is None:
        return None
    when, spans = found
    said_clock = re.search(rf"\b(?:at|by|around|for) {_CLOCK}", rest) or re.search(r"\b(?:morning|afternoon|evening|night|tonight)\b", rest)
    if re.search(r"\btoday\b", rest) and not said_clock and when.date() != dt.date.today():
        return "What time today? Say it like: remind me at 5 pm to " + _yours(re.sub(r"\s*\btoday\b", "", rest).strip()) + "."
    what = rest
    for a, b in sorted(spans, reverse=True):
        what = what[:a] + " " + what[b:]
    what = re.sub(r"\s+", " ", what).strip()
    what = re.sub(r"^(?:for|at|on|in)\s+", "", what)
    what = re.sub(r"^(?:to|about|that|of)\s+|\s+(?:to|about|that|at|on|by)$", "", what).strip()
    about = bool(re.match(r"(?:about|of)\s", what)) or bool(re.search(r"\b(?:about|of) " + re.escape(what) + "$", rest))
    what = re.sub(r"^(?:to|about|that)\s+", "", what).strip()
    what = _as_said(what, text)
    full = (f"about {what}" if about else what) or None
    mid = monitor.add_reminder(when.timestamp(), full,
                               None if what else when.strftime("This is your %-I:%M %p reminder.").replace(":00", ""))
    _last_set.update(mid=mid, what=full, when=when, t=time.time())
    said = _say_when(when)
    if not what:
        return f"Okay, reminder set for {said[3:] if said.startswith('at ') else said}."
    yours = _yours(what)
    link = "about" if about else "that" if re.match(r"(?:you|your|we|there|it)\b", yours) else "to"
    return f"Okay, I'll remind you {link} {yours} {said}."


_last_set: dict = {}
_DAYWORD = r"(?:on )?(?P<d>monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|today|tonight)\b"


# "I have an interview on Tuesday at 7:30, so remind me at 7 pm that I have a meeting": the remind-me can come
# after other words, and the day can be in them
@handles(r"(.+?)[ ,]+(?:so |and |then |also |just |please |can you |could you )*remind me (.+)")
def _remind_in_sentence(app, m, text):
    before, rest = m.group(1), m.group(2)
    fixing = re.match(r"(?:no|nope|not|actually|sorry|wait)\b", before)
    if fixing and not _undo_last():  # "not 5 days, on the 25th…": replaces what was just made…
        fixing = None
    if not re.search(_DAYWORD, rest) and _day_in(rest)[0] is None:
        d = re.search(_DAYWORD, before)
        if d:  # its time is on the day mentioned before it
            rest = f"{rest} on {d.group('d')}" if d.group("d") not in ("today", "tonight", "tomorrow") else f"{rest} {d.group('d')}"
        else:
            day, _ = _day_in(re.sub(r"^(?:no|nope|not|actually|sorry|wait)\b.*?(?=\bon\b|\bfor\b|$)", "", before))
            if day:  # "…on 25th November, whole day, remind me to go to Polar Bear"
                rest = f"{rest} on {day.day} {day.strftime('%B').lower()}"
    said = _remind(app, re.match(r"(.+)", rest), text) or _remind_todo(app, re.match(r"(.+)", rest), text)
    return ("Changed it. " + said.removeprefix("Okay, ").removeprefix("Got it. ")) if said and fixing else said


# right after setting one: "not tomorrow evening, on Monday evening", "no, make it 8", "move it to Friday"
@handles(r"(?:no|nope|actually|wait|sorry)?[ ,]*(?:not [a-z0-9 ]+?[ ,]+)?(?:make it|change it to|move it to|i meant|i mean|"
         r"on|at|for) (.+)")
def _fix_reminder(app, m, text):
    last = _last_set
    note = _last_note
    if note.get("id") and time.time() - note.get("t", 0) < 300 and note.get("t", 0) > last.get("t", 0):
        day, _ = _day_in(m.group(1))  # the last thing was a to-do: move it to the day you said
        if not day:
            return None
        from batsy.memory_store import get_store

        st = get_store()
        row = next((r for r in st.agenda_for(note["day"].isoformat()) if r["id"] == note["id"]), None)
        if not row:
            return None
        st.remove_agenda(note["id"])
        note.update(id=st.add_agenda(day.isoformat(), row["text"]), day=day, t=time.time())
        return f"Moved it: {row['text']} is under the clock for {_when_word(day)}."
    if not last or time.time() - last.get("t", 0) > 300:
        return None
    from batsy.tools import monitor

    new = m.group(1)
    found = _when(new)
    if found is None:
        return None
    when = found[0]
    if not re.search(rf"\b(?:at|by|around|for)? ?{_CLOCK}\b", new) or re.fullmatch(
            r"(?:on )?(?:[a-z]+day|tomorrow|today)(?: (?:morning|afternoon|evening|night))?", new):
        old = last["when"]  # only a day: keep the time it had
        if not re.search(r"\b(?:morning|afternoon|night)\b", new) or old.hour >= 17:
            when = when.replace(hour=old.hour, minute=old.minute)
    if when.timestamp() <= time.time():
        return None
    monitor.cancel(last["mid"])
    mid = monitor.add_reminder(when.timestamp(), last.get("what"))
    _last_set.update(mid=mid, when=when, t=time.time())
    what = _yours(last.get("what") or "")
    return f"Moved it: I'll remind you {'' if what.startswith('about ') else 'that ' if re.match(r'(?:you|your)\b', what) else 'to '}{what} {_say_when(when)}."


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


# --- to-dos for a day: under the clock in the phone app ---------------------------------------------------------------
_last_note: dict = {"text": "", "t": 0.0, "todo": False}  # what "put that under the clock" means
TASKY = re.compile(r"^(?:i|we)(?:'ve| have| had| need| needed| must| should| got| gotta| am going| was going)?"
                   r"(?: got)? (?:to|gotta) ", re.I)
DAYWORD = re.compile(r"\s*\b(?:today|tonight|this (?:morning|afternoon|evening)|tomorrow(?: morning| evening)?)\b", re.I)


def _as_todo(what: str) -> tuple[dt.date, str] | None:
    """'I had to make a video for our alumni meeting today' -> (today, 'Make a video for our alumni meeting').
    Only something to do on a named day (today / tonight / tomorrow): other notes stay notes."""
    if not TASKY.match(what) or not DAYWORD.search(what):
        return None
    day = dt.date.today() + dt.timedelta(days=1 if re.search(r"\btomorrow\b", what, re.I) else 0)
    task = DAYWORD.sub("", TASKY.sub("", what, count=1)).strip(" ,.")
    return (day, task[:1].upper() + task[1:]) if len(task.split()) >= 2 else None


_WORDNUM = {"one": 1, "a": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
            "ten": 10, "a couple of": 2, "couple of": 2, "a few": 3, "few": 3}
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_HAS_CLOCK = re.compile(r"\b(?:at|by|around) (?:\d|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|noon|midnight)|"
                        r"\d{1,2}(?::\d{2})? ?(?:am|pm|a m|p m)\b|\b\d{3,4}\b|o'?clock|\bnoon\b|\bmidnight\b|half past|"
                        r"quarter (?:past|to)|\bin \S+ (?:minutes?|hours?)\b|\b(?:minutes?|hours?)\b")


def _day_in(s: str) -> tuple[dt.date | None, str]:
    """The day a to-do is for, and the words without it: "in 5 days", "5 days", "on Friday", "on the 25th",
    "25th November whole day", "tomorrow" (after midnight: the day just begun). (None, s) when no day is said."""
    now = dt.datetime.now()
    today = now.date()
    late = now.hour < 4
    pats = [
        (rf"\b(?:in|after|within)? ?(\d+|{'|'.join(_WORDNUM)}) days?(?: from (?:now|today))?\b",
         lambda m: today + dt.timedelta(days=int(m.group(1)) if m.group(1).isdigit() else _WORDNUM[m.group(1)])),
        (r"\b(?:on )?(?:the )?day after tomorrow\b", lambda m: today + dt.timedelta(days=1 if late else 2)),
        (r"\b(?:on |for )?tomorrow\b", lambda m: today + dt.timedelta(days=0 if late else 1)),
        (r"\b(?:for |on )?(?:today|tonight)\b", lambda m: today),
        (rf"\b(?:on |this |next |for )?({'|'.join(_WEEKDAYS)})\b",
         lambda m: today + dt.timedelta(days=(_WEEKDAYS.index(m.group(1)) - today.weekday()) % 7 or 7)),
        (rf"\b(?:on |for )?(?:the )?(\d{{1,2}})(?:st|nd|rd|th)?(?: of)? ({'|'.join(MONTHS)})\b",
         lambda m: _parse_day(f"{m.group(1)} {m.group(2)}")),
        (rf"\b(?:on |for )?({'|'.join(MONTHS)}) (?:the )?(\d{{1,2}})(?:st|nd|rd|th)?\b",
         lambda m: _parse_day(f"{m.group(1)} {m.group(2)}")),
        (r"\b(?:on |for )?the (\d{1,2})(?:st|nd|rd|th)\b",
         lambda m: (lambda d: d if d >= today else (d.replace(month=d.month % 12 + 1, year=d.year + (d.month == 12))))(
             today.replace(day=int(m.group(1))))),
    ]
    for rx, fn in pats:
        m = re.search(rx, s)
        if m:
            try:
                d = fn(m)
            except (ValueError, KeyError):
                d = None
            if d:
                rest = (s[:m.start()] + " " + s[m.end():])
                rest = re.sub(r"\b(?:the )?(?:whole|all|full|entire) day\b", " ", rest)
                return d, re.sub(r"\s+", " ", rest).strip(" ,.")
    return None, s


def _when_word(day: dt.date) -> str:
    n = (day - dt.date.today()).days
    return "today" if n == 0 else "tomorrow" if n == 1 and dt.datetime.now().hour >= 4 else \
        day.strftime("%A") if n < 7 else day.strftime("%A, %B %-d")


def _undo_last(within: float = 300) -> str | None:
    """The reminder or to-do Batsy made in the last few minutes, taken back (you're correcting it). What it was."""
    from batsy.memory_store import get_store
    from batsy.tools import monitor

    r, n = _last_set, _last_note
    if r.get("mid") and time.time() - r.get("t", 0) < within and r.get("t", 0) >= n.get("t", 0):
        monitor.cancel(r["mid"])
        r.clear()
        return "reminder"
    if n.get("id") and time.time() - n.get("t", 0) < within:
        get_store().remove_agenda(n["id"])
        n.update(id=None, todo=False)
        return "to-do"
    return None


# "remind (me) that I have to add a mail connector to you" with no time: a to-do for today, under the clock.
# (With a time, "remind me at 5 to…" / "in 10 minutes", it's a real reminder: left for the reminder handler.)
@handles(r"(?:[a-z]+ )?remind(?: me)?(?: that| to| about| of)? (.{6,})")
def _remind_todo(app, m, text):
    what = m.group(1).strip()
    if _HAS_CLOCK.search(what) or re.search(r"\b(?:every|morning|evening|afternoon|night)\b", what) \
            or re.match(r"(?:me )?(?:at|in) (?!\S+ days?\b)", what):
        return None  # a time: the reminder handler sets a real reminder
    given_day = _day_in(what)[0]  # the day can come from words before "remind me" (handed in with `what`)
    raw = re.search(r"(?i)\bremind(?: me)?(?: that| to| about| of)?\s+(.+)", text)
    what = _clean_note(raw.group(1).strip().rstrip(".") if raw else what)
    day, what = _day_in(what.lower()) if _day_in(what.lower())[0] else (None, what)
    day = day or given_day or dt.date.today()
    what = re.sub(r"^(?:me )?(?:that |to |about |of )?(?:something )?(?:that )?", "", what)  # "of something five days that…"
    task = DAYWORD.sub("", TASKY.sub("", what, count=1)).strip(" ,.")
    task = (task[:1].upper() + task[1:]) or what
    task = _as_said(task, text)[:1].upper() + _as_said(task, text)[1:]
    from batsy.memory_store import get_store

    rid = get_store().add_agenda(day.isoformat(), task)
    _last_note.update(text=what, t=time.time(), todo=True, id=rid, day=day)
    return f"Got it. It's under the clock for {_when_word(day)}: {task}."


# "put that under the clock" (heard as "under my timer, a chick clock" too), "add that to my to-do"
CLOCKY = r"(?:the |my )?(?:clock|timer|chick ?clock|tick ?clock|to ?-?do(?: list)?|todo(?: list)?|check ?list|list)"


@handles(rf"(?:also |and |uh |um )*(?:put|add|move|show|pin|keep) (?:that|it|this)(?: one)? (?:under|on|in|to|into|below|beneath) "
         rf"{CLOCKY}(?:.*)?")
def _under_clock(app, m, text):
    if not _last_note["text"] or time.time() - _last_note["t"] > 1800:
        return None  # nothing just noted: let the rest of Batsy have it
    if _last_note["todo"]:
        return "It's already under the clock."
    from batsy.memory_store import get_store

    task = TASKY.sub("", DAYWORD.sub("", _last_note["text"]), count=1).strip(" ,.")
    task = task[:1].upper() + task[1:]
    get_store().add_agenda(dt.date.today().isoformat(), task)
    _last_note["todo"] = True
    return f"Done, it's under the clock in the phone app: {task}."


# "completed making a video for our alumni meeting", "I finished the website", "tick off the video", "mark X done"
@handles(r"(?:batsy )?(?:i(?:'ve| have)? )?(?:completed|finished|done with|done|did|wrapped up|sent|submitted) (.+)",
         r"(?:tick|check|cross) off (.+)", r"mark (.+?) (?:as )?(?:done|finished|complete|completed)")
def _tick_off(app, m, text):
    from rapidfuzz import fuzz

    from batsy.memory_store import get_store

    st = get_store()
    said = re.sub(r"^(?:the|my|our|that|it)\s+", "", m.group(1).strip())
    days = [(dt.date.today() - dt.timedelta(days=k)).isoformat() for k in range(3)]  # today, and 2 days back
    open_ = [r for d in days for r in st.agenda_for(d) if not r["done"]]
    if not open_:
        return None
    best = max(open_, key=lambda r: fuzz.token_set_ratio(said.lower(), r["text"].lower()))
    if fuzz.token_set_ratio(said.lower(), best["text"].lower()) < 70:
        return None  # "I finished the movie": not one of your to-dos, so just talk
    st.tick_agenda(best["id"])
    left = sum(1 for r in st.agenda_for(days[0]) if not r["done"])
    return f"Nice, ticked off: {best['text']}." + (f" {left} left for today." if left else " That's everything for today.")


# --- "remember that ..." -> long-term memory, every time (it used to go to the AI, which could lose it) ------------
@handles(r"(?:[a-z]+ )?(?:please )?(?:remember|don'?t forget|keep in mind)"
         r"(?: that| to| this| of)?:? (.+)")
def _remember(app, m, text):
    what = m.group(1).strip()
    if len(what.split()) < 2 or re.match(r"(?:my voice|what|when|where|who|me)\b", what):
        return None  # "remember my voice", "remember what I said": not a note
    raw = re.search(r"(?i)\b(?:remember|don'?t forget|keep in mind)(?: that| to| this| of)?:?\s+(.+)", text)
    what = (raw.group(1).strip().rstrip(".") if raw else what)
    from batsy.memory_store import get_store

    what, dest = _destination(_clean_note(what))
    todo = _as_todo(what)
    if todo:  # "I have to make a video for the alumni meeting today": a to-do for that day, under the phone's clock
        day, task = todo
        get_store().add_agenda(day.isoformat(), task)
        _last_note.update(text=what, t=time.time(), todo=True)
        _to_brain(what, dest)
        when = "today" if day == dt.date.today() else "tomorrow"
        return f"Got it. It's under the clock for {when}: {task}."
    _last_note.update(text=what, t=time.time(), todo=False)
    fid = get_store().add_fact(f"Jonathan asked me to remember ({dt.date.today():%d %b %Y}): {what}")
    if len(what.split()) > 14 or re.search(r"\b(?:i mean|what is it|no wait|sorry|actually|say not)\b", what, re.I):
        threading.Thread(target=_condense_fact, args=(app, fid, what, dest), daemon=True, name="tidy-memory").start()
        return "Got it, I'll remember that" + (" and put it in your brain." if dest else ".")
    _maybe_task(what)
    _to_brain(what, dest)
    return f"Got it. I'll remember: {_yours(what) if what[:1].islower() else what}."


FILLERS = re.compile(r"\b(?:uh+|um+|erm|uhm|hmm+|you know)\b[,.]?\s*", re.I)


def _clean_note(text: str) -> str:
    """Spoken notes without the 'uh's, doubled words and stray punctuation."""
    t = FILLERS.sub("", text)
    t = re.sub(r"\b(\w+)( \1\b)+", r"\1", t, flags=re.I)  # "the the" -> "the"
    t = re.sub(r"\s+([,.?!])", r"\1", re.sub(r"\s{2,}", " ", t)).strip(" ,.")
    return t[:1].upper() + t[1:] if t else text


DEST = re.compile(r"[,.]?\s*(?:and )?(?:please )?(?:add|put|save|write|log|note) (?:it|this|that|them)? ?(?:to|in|into|under|on) "
                  r"(?:the |my )?(?P<where>.*?)(?:\s+(?:in|on|to|into) (?:the |my )?(?:obsidian|brain|second brain|notes|"
                  r"vault|journal)\b[\w ]{0,12})?[.!]?$", re.I)
LEARNING = re.compile(r"\b(?:i(?:'m| am) (?:learning|studying)|i learn(?:ed|t)|i(?:'ve| have) been (?:learning|studying)|"
                      r"studying|learning)\b", re.I)


def _destination(what: str) -> tuple[str, str | None]:
    """'… Add it to 3D printing learning in the Obsidian brain.' -> (the note, '3D printing learning').
    Something you're learning, with no place named -> 'Learning'."""
    m = DEST.search(what)
    if m and (m.group("where") or "").strip():
        rest = what[:m.start()].strip(" ,.")
        rest = re.sub(r"[,.]?\s*(?:remember (?:this|that|it)|and remember (?:this|that|it))[,.]?\s*$", "", rest, flags=re.I)
        return rest or what, m.group("where").strip()
    return what, ("Learning" if LEARNING.search(what) else None)


def _to_brain(what: str, dest: str | None) -> None:
    if dest:
        try:
            from batsy import vault_capture

            vault_capture.learning(dest, what)
        except Exception:
            log.exception("brain: learning note")


def _condense_fact(app, fid: str, what: str, dest: str | None = None) -> None:
    """A long, self-correcting note -> one clear sentence (free local model), replacing the raw one."""
    from batsy import vault_daily
    from batsy.memory_store import get_store

    try:
        vault_daily._cfg = vault_daily._cfg or app.cfg
        out = vault_daily.local_model(
            "Jonathan dictated this note to his assistant Batsy (speech-to-text, rambling, with false starts):\n"
            f"\"{what}\"\n\nRewrite it as one or two short, clear sentences stating the facts themselves (not that he asked "
            "to remember them), in third person (\"Jonathan ...\"), keeping every name, number and detail, with units "
            "written normally (\"100 x 60 x 10 mm\"). In his note, \"you\" means Batsy. Reply with the text only.", 120,
            background=True)
        clean = out.strip().strip('"').split("\n")[0].strip()
        if 4 <= len(clean.split()) <= 45:
            st = get_store()
            st.forget_fact(fid)
            st.add_fact(f"Jonathan asked me to remember ({dt.date.today():%d %b %Y}): {clean}", fid)
            _maybe_task(clean)
            _to_brain(clean, dest)
            app.announce(f"Noted: {clean}" + (" It's in your brain too." if dest else ""))
            return
    except Exception:
        log.exception("tidying a memory")
    _to_brain(what, dest)  # the model wasn't available: the note as said


def _maybe_task(what: str) -> None:
    """'I have to / need to / should …': also a task in the brain's inbox, to tick off."""
    if re.search(r"\b(?:have to|need to|has to|needs to|must|should|got to|gotta|todo|to do)\b", what, re.I):
        try:
            from batsy import vault_capture

            vault_capture.note(f"To do: {what}", "memory")
        except Exception:
            pass


@handles(r"what (?:did|have) i (?:ask|asked|tell|told) you to remember", r"what do you remember(?: for me)?",
         r"(?:what are|list|read) my notes", r"what(?:'s| is) on my (?:notes|memory)")
def _recall_notes(app, m, text):
    from batsy.memory_store import get_store

    notes = [r["fact"].split(": ", 1)[1] for r in get_store().all_facts()
             if r["fact"].startswith("Jonathan asked me to remember")]
    if not notes:
        return "You haven't asked me to remember anything yet."
    return f"You asked me to remember {len(notes)} thing{'s' if len(notes) != 1 else ''}: " + "; ".join(notes[-6:]) + "."
