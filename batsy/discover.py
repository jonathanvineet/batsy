"""Batsy Discovery: a weekly Spotify playlist of songs you've never played, picked for your taste.

Claude (Pro plan) reads your taste profile (music_profile.json), what you've actually been listening to
(listening.json: top artists, eras, moods) and suggests songs; each is found on Spotify, and anything you already
know (your library, everything tagged, everything you've played) is dropped. The rest replace the tracks of your
private "Batsy Discovery" playlist. Built every Monday (first check after 5 AM), or now: "make my discovery
playlist" / "find me new songs". "Play my discovery playlist" / "play something new" plays it.

The brain gets the list (03 Areas/Music.md, Discovery), and the songs you then play are ticked there.
Creating the playlist needs Spotify's playlist-modify scope: after this was added, run `batsy spotify-login` once.
"""
import collections
import datetime as dt
import json
import logging
import re
import threading
import time

from batsy.config import DATA_DIR

log = logging.getLogger("batsy.discover")
STATE = DATA_DIR / "discovery.json"
NAME = "Batsy Discovery"
SIZE = 25

MAKE = re.compile(r"^(?:(?:can you|please|hey|ok|okay)\s+)*(?:make|build|create|refresh|update|redo) (?:me )?(?:a |my |the )?"
                  r"(?:new )?discovery(?: playlist)?$|^(?:(?:can you|please)\s+)*(?:find|suggest|recommend|get) me "
                  r"(?:some )?new (?:songs|music)$|^suggest (?:some )?new (?:songs|music)(?: for me)?$")
VERSION = re.compile(r"\b(?:re-?mix|mix|cover|lo-?fi|slowed|reverb|karaoke|instrumental|reprise|unplugged|mashup|"
                     r"8d|sped up|version)\b", re.I)
PLAY = re.compile(r"^(?:(?:can you|please|hey|ok|okay)\s+)*play (?:my |the )?(?:discovery(?: playlist)?|batsy discovery|"
                  r"something new|some new (?:songs|music)|new (?:songs|music) for me)$")


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _save(**kv) -> None:
    STATE.write_text(json.dumps({**_state(), **kv}))


def _known() -> tuple[set[str], set[str]]:
    """Track ids you already know, and 'song|artist' names (the same song can have several ids)."""
    ids, names = set(), set()
    try:
        for uri in json.loads((DATA_DIR / "music_moods.json").read_text()):
            ids.add(uri.rsplit(":", 1)[-1])
    except (OSError, ValueError):
        pass
    from batsy import vault_music

    for p in vault_music._load():
        ids.add(p["id"])
        names.add(_key(p["name"], p["artists"][0]))
    return ids, names


def _key(song: str, artist: str) -> str:
    from batsy import vault_music

    return re.sub(r"\W+", "", vault_music._short(song).lower()) + "|" + re.sub(r"\W+", "", artist.lower())


def _brief() -> str:
    """Your taste, for Claude: the profile Batsy built plus what you've played lately."""
    try:
        prof = json.loads((DATA_DIR / "music_profile.json").read_text())
    except (OSError, ValueError):
        prof = {}
    from batsy import vault_music

    plays = vault_music._load()[-400:]
    artists = collections.Counter(a for p in plays for a in p["artists"][:2]).most_common(15)
    recent = [f"{vault_music._short(p['name'])} – {p['artists'][0]}" for p in plays[-40:]]
    return ("TASTE PROFILE:\n" + (prof.get("summary") or "(none yet)") +
            ("\nCorrections he made: " + "; ".join(prof.get("rules") or []) if prof.get("rules") else "") +
            "\n\nMOST PLAYED ARTISTS LATELY: " + ", ".join(f"{a} ({n})" for a, n in artists) +
            "\n\nLAST SONGS PLAYED:\n" + "\n".join(recent))


def _ask_claude(cfg, brief: str, avoid: list[str]) -> list[dict]:
    from batsy import claude_code

    prompt = (f"{brief}\n\nSuggest 40 songs Jonathan has probably NEVER heard but will love, for a weekly discovery "
              "playlist. Match his languages, eras and moods from the profile; mix about 60% close to his favourites "
              "(other songs by composers/singers he likes, deep cuts from films he'd know) and 40% new artists in the "
              "same style. No song from the 'last songs played' list" + (f" or these: {'; '.join(avoid[:60])}" if avoid else "")
              + ". Real, existing songs on Spotify only.\n\nReply with JSON only: "
              '[{"song": "...", "artist": "main artist", "why": "max 8 words"}]')
    out = claude_code.run(prompt, "discovery playlist", cfg=cfg, timeout=240)
    m = re.search(r"\[.*\]", out or "", re.S)
    return json.loads(m.group(0)) if m else []


def _find(sp, song: str, artist: str) -> dict | None:
    from rapidfuzz import fuzz

    for q in (f'track:"{song}" artist:"{artist}"', f"{song} {artist}"):
        try:
            items = ((sp._req("GET", "/search", {"q": q, "type": "track", "limit": 5}) or {}).get("tracks") or {}).get("items", [])
        except Exception:
            items = []
        for t in items:
            if VERSION.search(t["name"]) and not VERSION.search(song):
                continue  # the original, not a remix / cover / lo-fi / karaoke version
            names = " ".join(a["name"] for a in t["artists"])
            if fuzz.partial_ratio(artist.lower(), names.lower()) >= 80 and fuzz.WRatio(song.lower(), t["name"].lower()) >= 75:
                return t
    return None


def _playlist(sp) -> tuple[str, str]:
    """(id, url) of the Batsy Discovery playlist, made the first time."""
    st = _state()
    if st.get("playlist"):
        try:
            p = sp._req("GET", f"/playlists/{st['playlist']}", {"fields": "id,external_urls"})
            if p:
                return p["id"], p["external_urls"]["spotify"]
        except Exception:
            pass
    body = {"name": NAME, "public": False,
            "description": "New songs for you, picked every Monday by Batsy from what you love."}
    try:
        p = sp._req("POST", "/me/playlists", body=body)
    except Exception:
        me = sp._req("GET", "/me")
        p = sp._req("POST", f"/users/{me['id']}/playlists", body=body)
    _save(playlist=p["id"])
    return p["id"], p["external_urls"]["spotify"]


def _fill(sp, pid: str, uris: list[str]) -> None:
    for path in (f"/playlists/{pid}/items", f"/playlists/{pid}/tracks"):  # newer and older Web API names
        try:
            sp._req("PUT", path, body={"uris": uris})
            return
        except Exception as e:
            err = e
    raise err


def build(cfg) -> str:
    """Make this week's playlist. Returns what to say."""
    from batsy.tools import music

    sp = music.get_spotify(cfg)
    if not sp.ready():
        return "Spotify isn't connected."
    scopes = ""
    try:
        from batsy.spotify import TOKEN_FILE

        scopes = json.loads(TOKEN_FILE.read_text()).get("scope", "")
    except Exception:
        pass
    if "playlist-modify" not in scopes:
        return ("I need permission to make playlists in your Spotify. Run batsy spotify-login on the laptop once, "
                "then ask again.")
    ids, names = _known()
    picks = _ask_claude(cfg, _brief(), _state().get("given", [])[-120:])
    chosen, seen = [], set()
    for p in picks:
        if len(chosen) >= SIZE:
            break
        t = _find(sp, str(p.get("song", "")), str(p.get("artist", "")))
        if not t or t["id"] in ids or t["id"] in seen or _key(t["name"], t["artists"][0]["name"]) in names:
            continue
        seen.add(t["id"])
        chosen.append({"id": t["id"], "uri": t["uri"], "name": t["name"],
                       "artist": ", ".join(a["name"] for a in t["artists"][:2]), "why": str(p.get("why", ""))[:80]})
    if len(chosen) < 5:
        return "I couldn't find enough new songs on Spotify this time. Try again in a bit."
    pid, url = _playlist(sp)
    _fill(sp, pid, [c["uri"] for c in chosen])
    week = dt.date.today().isocalendar()
    _save(week=f"{week.year}-W{week.week:02d}", built=time.time(), url=url, songs=chosen,
          given=(_state().get("given", []) + [f"{c['name']} – {c['artist']}" for c in chosen])[-300:])
    note_in_brain()
    log.info("discovery: %d songs", len(chosen))
    return f"Your Batsy Discovery playlist has {len(chosen)} new songs. Say play my discovery playlist."


def note_in_brain() -> None:
    """Music.md, Discovery: this week's songs, ticked once you've played them."""
    try:
        from batsy import vault, vault_music

        st = _state()
        if not st.get("songs") or vault._q is None:
            return
        played = {p["id"] for p in vault_music._load() if p["t"] >= st.get("built", 0)}
        lines = [f"**{st['week']}** · [{NAME}]({st['url']}) · {sum(c['id'] in played for c in st['songs'])} of "
                 f"{len(st['songs'])} played"]
        lines += [f"- {'▶' if c['id'] in played else '○'} **{vault_music._short(c['name'])}** · {c['artist']}"
                  + (f" · _{c['why']}_" if c["why"] else "") for c in st["songs"]]
        vault.submit(vault.set_block, vault.ROOT / "03 Areas" / "Music.md", "discovery", "\n".join(lines), "Discovery")
    except Exception:
        log.exception("discovery note")


def _can_edit() -> bool:
    try:
        from batsy.spotify import TOKEN_FILE

        return "playlist-modify" in json.loads(TOKEN_FILE.read_text()).get("scope", "")
    except Exception:
        return False


def play(cfg) -> str:
    from batsy.tools import music

    st = _state()
    if not st.get("playlist"):
        return "There's no discovery playlist yet. Say make my discovery playlist."
    sp = music.get_spotify(cfg)
    try:
        sp._req("PUT", "/me/player/play", {"device_id": sp.device()["id"]},
                {"context_uri": f"spotify:playlist:{st['playlist']}"})
    except Exception as e:
        return str(e)
    return f"Playing your Batsy Discovery: {len(st.get('songs', []))} new songs."


def handle(app, n: str) -> str | None:
    if PLAY.match(n):
        return play(app.cfg)
    if MAKE.match(n):
        def go():
            app.announce(build(app.cfg))
        threading.Thread(target=go, daemon=True, name="discovery").start()
        return "Finding new songs for you. It takes a minute."
    return None


def start(app) -> None:
    """Every Monday (logical day), a fresh playlist; the brain's ticks stay current."""
    def loop():
        time.sleep(120)
        while True:
            try:
                today = (dt.datetime.now() - dt.timedelta(hours=5)).date()
                week = f"{today.isocalendar().year}-W{today.isocalendar().week:02d}"
                if _state().get("week") != week and _can_edit():  # (until spotify-login adds the scope: waits)
                    log.info("discovery (weekly): %s", build(app.cfg))
                note_in_brain()
            except Exception:
                log.exception("discovery loop")
            time.sleep(3600)

    threading.Thread(target=loop, daemon=True, name="discovery").start()
