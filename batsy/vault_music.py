"""Music in the brain: everything you listen to on Spotify (any device, any way you started it), not just what
Batsy played.

Every few minutes Batsy reads Spotify's listening history (recently played, with exact times) and keeps it in
DATA_DIR/listening.json. From that:
  - each day's note gets a Music block: "23 songs · 1 h 22 min · mostly Ilaiyaraaja · from Timeless Tamil", then
    every song with its time, artists, album and mood
  - 03 Areas/Music.md gets "Listening": this week and this month (plays, time, top artists and songs) and the
    latest plays
"""
import collections
import datetime as dt
import json
import logging
import threading
import time

from batsy import vault
from batsy.config import DATA_DIR

log = logging.getLogger("batsy.vault")
LOG = DATA_DIR / "listening.json"
_playlists: dict[str, str] = {}


def _load() -> list[dict]:
    try:
        return json.loads(LOG.read_text())
    except (OSError, ValueError):
        return []


def _spotify(cfg):
    from batsy.tools import music

    sp = music.get_spotify(cfg)
    return sp if sp.ready() else None


def _playlist_name(sp, uri: str) -> str:
    if not uri or not uri.startswith("spotify:playlist:"):
        return ""
    if uri not in _playlists:
        try:
            _playlists[uri] = (sp._req("GET", f"/playlists/{uri.rsplit(':', 1)[1]}", {"fields": "name"}) or {}).get("name", "")
        except Exception:
            _playlists[uri] = ""
    return _playlists[uri]


def poll(cfg) -> int:
    """New plays since last time -> listening.json and the notes. Returns how many were new."""
    sp = _spotify(cfg)
    if sp is None:
        return 0
    plays = _load()
    after = max((p["t"] for p in plays), default=0)
    r = sp._req("GET", "/me/player/recently-played", {"limit": 50, **({"after": int(after * 1000)} if after else {})})
    new = []
    for it in (r or {}).get("items", []):
        t = dt.datetime.fromisoformat(it["played_at"].replace("Z", "+00:00")).timestamp()
        if t <= after:
            continue
        tr, ctx = it["track"], it.get("context") or {}
        mood = ""
        try:
            from batsy import moods

            m = moods.of(tr["uri"]) or {}
            mood = ", ".join(m.get("m") or [])
        except Exception:
            pass
        new.append({"t": t, "id": tr["id"], "name": tr["name"], "artists": [a["name"] for a in tr["artists"]],
                    "album": tr["album"]["name"], "ms": tr.get("duration_ms") or 0,
                    "from": _playlist_name(sp, ctx.get("uri", "")), "mood": mood})
    if not new:
        return 0
    plays = sorted(plays + new, key=lambda p: p["t"])
    LOG.write_text(json.dumps(plays))
    days = {vault.today(p["t"]) for p in new}
    vault.submit(_write, sorted(days), plays)
    log.info("brain: %d new song(s) listened to", len(new))
    return len(new)


def _short(name: str) -> str:
    """'Elangathu - From "Pithamagan"' -> 'Elangathu'."""
    import re

    s = re.sub(r"\s*(?:-\s*|\(|\[)\s*from\b.*$", "", name, flags=re.I)  # ' - From "Film"', '(From "Film") [Tamil]'
    s = re.sub(r"\s*[(\[](?:tamil|telugu|hindi|malayalam|kannada|original motion picture soundtrack)[)\]]\s*$", "", s,
               flags=re.I)
    return s.strip() or name


def _album(p: dict) -> str:
    """The album, shortened; nothing when it's just the song again (singles)."""
    a = _short(p["album"])
    return "" if a.lower() == _short(p["name"]).lower() else a


def _length(ms: int) -> str:
    m = round(ms / 60000)
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def _write(days: list, plays: list[dict]) -> None:
    for day in days:
        mine = [p for p in plays if vault.today(p["t"]) == day]
        if not mine:
            continue
        artists = collections.Counter(p["artists"][0] for p in mine)
        lists = collections.Counter(p["from"] for p in mine if p["from"])
        head = f"**{len(mine)} song{'s' if len(mine) != 1 else ''}** · {_length(sum(p['ms'] for p in mine))}"
        if artists:
            head += f" · mostly {artists.most_common(1)[0][0]}"
        if lists:
            head += f" · from *{lists.most_common(1)[0][0]}*"
        lines = [head] + [
            f"- {time.strftime('%H:%M', time.localtime(p['t']))} · **{_short(p['name'])}** · "
            f"{', '.join(p['artists'][:2])}" + (f" · *{_album(p)}*" if _album(p) else "")
            + (f" · {p['mood']}" if p["mood"] else "")
            for p in mine]
        vault.set_block(vault.daily(day), "music", "\n".join(lines), "Music")
    _area(plays)


def _stats(plays: list[dict], since: float) -> str:
    sel = [p for p in plays if p["t"] >= since]
    if not sel:
        return "_Nothing yet._"
    artists = collections.Counter(a for p in sel for a in p["artists"][:1]).most_common(5)
    songs = [x for x in collections.Counter((_short(p["name"]), p["artists"][0]) for p in sel).most_common(5) if x[1] > 1]
    return (f"{len(sel)} plays · {_length(sum(p['ms'] for p in sel))}\n"
            f"- **Artists:** " + " · ".join(f"{a} ({n})" for a, n in artists) + "\n"
            f"- **On repeat:** " + (" · ".join(f"{s} – {a} ({n}×)" for (s, a), n in songs) or "nothing twice yet"))


def _area(plays: list[dict]) -> None:
    p = vault.ROOT / "03 Areas" / "Music.md"
    if not p.exists():
        return
    now = dt.datetime.now()
    week = (now - dt.timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0).timestamp()
    month = now.replace(day=1, hour=0, minute=0, second=0).timestamp()
    latest = "\n".join(f"- [[{vault.daily_name(vault.today(x['t']))}]] {time.strftime('%H:%M', time.localtime(x['t']))} · "
                       f"{_short(x['name'])} · {x['artists'][0]}" for x in plays[-10:][::-1])
    vault.set_block(p, "listening", f"**This week:** {_stats(plays, week)}\n\n**This month:** {_stats(plays, month)}"
                                   f"\n\n**Latest:**\n{latest}", "Listening")


def start(app) -> None:
    def loop():
        time.sleep(20)
        while True:
            try:
                poll(app.cfg)
            except Exception as e:
                log.info("brain music: %s", e)
            time.sleep(600)  # Spotify keeps the last 50 plays: every 10 min never misses any

    threading.Thread(target=loop, daemon=True, name="vault-music").start()
