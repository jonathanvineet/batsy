"""Spotify control through the Spotify Web API (Spotify Connect).

Music plays on another device (the iPad); the iPad's Spotify app doesn't accept LAN remote control,
so Batsy talks to Spotify's servers, which relay commands to whichever device is running Spotify.
Needs Spotify Premium and a one-time `batsy spotify-login`.

Common commands ("play X", "pause", "next", "what's playing") are parsed here locally, so they
cost no Claude tokens at all. The `spotify` tool covers anything fancier.
"""
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from batsy.config import CONFIG_DIR, DATA_DIR

API = "https://api.spotify.com/v1"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
TOKEN_FILE = CONFIG_DIR / "spotify_token.json"
SCOPES = ("user-read-playback-state user-modify-playback-state user-read-currently-playing "
          "playlist-read-private user-library-read user-library-modify user-top-read user-read-recently-played")
INDEX_FILE = DATA_DIR / "music_index.json"  # your saved songs, playlists and artists, for matching what you say
CATALOG_FILE = DATA_DIR / "music_catalog.json"  # + popular songs of your artists and of big Tamil/English names
# Spotify no longer lets new apps browse genres or other people's playlists, but search still works: the catalogue
# is these artists' most popular songs (plus your own artists'), refreshed weekly.
CATALOG_ARTISTS = [
    "A.R. Rahman", "Ilaiyaraaja", "Anirudh Ravichander", "Yuvan Shankar Raja", "Harris Jayaraj", "G. V. Prakash",
    "D. Imman", "Santhosh Narayanan", "Sid Sriram", "Hiphop Tamizha", "Sean Roldan", "Ghibran", "Devi Sri Prasad",
    "Vidyasagar", "Deva", "S. P. Balasubrahmanyam", "K. S. Chithra", "Shreya Ghoshal", "Shankar Mahadevan",
    "Dhanush", "Thalapathy Vijay", "Sai Abhyankkar", "Thaman S", "Hariharan", "Karthik", "Chinmayi",
    "Pradeep Kumar", "Govind Vasantha", "Justin Prabhakaran", "Leon James", "Sam C.S.", "Vijay Antony",
    "Benny Dayal", "Haricharan", "Andrea Jeremiah", "Jonita Gandhi", "Dhee", "Arivu", "Sushin Shyam", "Vedan",
    "Anirudh", "Taylor Swift", "The Weeknd", "Ed Sheeran", "Arijit Singh", "Imagine Dragons", "Billie Eilish"]
TAMIL = set(CATALOG_ARTISTS[:41])  # the Tamil composers and singers above: a Tamil song carries on with Tamil music
SURE = 0.75  # a match this close to what you said plays straight away; below it, Batsy asks first
# "play it on my iPad": a device, not a song name
DEVICE_WORDS = r"(ipad|laptop|computer|pc|phone|mobile|desktop|this computer|here|speaker|tv)"
GENERIC = re.compile(r"^(it|this|that|music|some music|spotify|the music|my music|the song|this song|that song|"
                     r"the current song|current song|whatever was playing|what was playing|something|the track)$")
MOODS = re.compile(r"\b(chill|relax\w*|calm|focus|study\w*|work(ing|out)?|gym|sad|happy|party|lo-?fi|sleep\w*|"
                   r"romantic|mood|vibes?|coding|morning|night|rainy|energetic|hype|acoustic|instrumental)\b")
NOT_MUSIC = re.compile(r"\b(youtube|video|movie|netflix|film|episode|game|chess)\b")


class SpotifyError(Exception):
    pass


def _post_form(url: str, data: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise SpotifyError(f"Spotify login failed: {e.read().decode(errors='replace')[:200]}")


def _save_token(tok: dict, old: dict | None = None) -> None:
    tok["expires_at"] = time.time() + tok.get("expires_in", 3600)
    if old and "refresh_token" not in tok:
        tok["refresh_token"] = old["refresh_token"]
    TOKEN_FILE.write_text(json.dumps(tok))
    os.chmod(TOKEN_FILE, 0o600)


def login(cfg) -> None:
    """One-time authorization (PKCE, no client secret). Opens the browser on this laptop."""
    client_id = cfg.get("spotify.client_id")
    if not client_id:
        raise SystemExit("Set [spotify] client_id in ~/.config/batsy/config.toml first (see README).")
    port = cfg.get("spotify.redirect_port")
    redirect = f"http://127.0.0.1:{port}/callback"
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": redirect, "scope": SCOPES,
        "code_challenge_method": "S256", "code_challenge": challenge, "state": state,
    })
    got: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Batsy is connected to Spotify. You can close this tab.</h2>")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
    print(f"Opening Spotify login in your browser. If it doesn't open, visit:\n{url}\n")
    webbrowser.open(url)
    while "code" not in got and "error" not in got:
        srv.handle_request()
    srv.server_close()
    if got.get("state") != state or "code" not in got:
        raise SystemExit(f"Spotify login failed: {got.get('error', 'state mismatch')}")
    tok = _post_form(TOKEN_URL, {"grant_type": "authorization_code", "code": got["code"], "redirect_uri": redirect,
                                 "client_id": client_id, "code_verifier": verifier})
    _save_token(tok)
    print("Spotify connected.")


class Spotify:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client_id = cfg.get("spotify.client_id")
        self.device_hint = (cfg.get("spotify.device") or "").lower()
        self.pending: list[tuple] = []  # candidates Batsy asked about ("Is it X by Y?"), best first
        self.radio: dict | None = None  # keeps the music going after the song you asked for (see _radio_loop)
        self.last: list[tuple] = []     # what the last play chose from, for "not that one"
        self._indexing = False

    def ready(self) -> bool:
        return bool(self.client_id) and TOKEN_FILE.exists()

    # --- HTTP -------------------------------------------------------------
    def _token(self) -> str:
        tok = json.loads(TOKEN_FILE.read_text())
        if tok["expires_at"] - 60 < time.time():
            new = _post_form(TOKEN_URL, {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                         "client_id": self.client_id})
            _save_token(new, tok)
            tok = json.loads(TOKEN_FILE.read_text())
        return tok["access_token"]

    def _req(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else (b"" if method in ("PUT", "POST") else None)
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read()).get("error", {})
            except ValueError:
                err = {}
            reason, msg = err.get("reason", ""), err.get("message", "")
            if e.code == 404 and ("NO_ACTIVE_DEVICE" in reason or "device" in msg.lower()):
                raise SpotifyError("I can't see the iPad in Spotify. Open the Spotify app on it and try again.")
            if e.code == 403 and "PREMIUM" in reason:
                raise SpotifyError("Spotify Premium is needed for remote control.")
            if e.code == 403 and "volume" in path:
                raise SpotifyError("That device doesn't allow remote volume control. iPads usually don't.")
            if e.code == 429:
                raise SpotifyError("Spotify is rate limiting me. Try again in a moment.")
            raise SpotifyError(f"Spotify said: {msg or e.code}")
        except urllib.error.URLError:
            raise SpotifyError("I can't reach Spotify. Check the internet connection.")

    # --- devices ----------------------------------------------------------
    def devices(self) -> list[dict]:
        return (self._req("GET", "/me/player/devices") or {}).get("devices", [])

    def device(self) -> dict:
        devs = self.devices()
        if not devs:
            raise SpotifyError("No Spotify devices are online. Open Spotify on the iPad.")
        hinted = [d for d in devs if self.device_hint and self.device_hint in d["name"].lower()]
        active = [d for d in devs if d.get("is_active")]
        return (hinted or active or devs)[0]

    def _dev_params(self) -> dict:
        return {"device_id": self.device()["id"]}

    def find_device(self, word: str) -> dict:
        """'ipad' -> the iPad; 'laptop' / 'computer' / 'here' -> this computer's Spotify (desktop app or web player)."""
        devs = self.devices()
        if not devs:
            raise SpotifyError("No Spotify devices are online. Open Spotify on the device you want.")
        word = word.lower()
        if word in ("laptop", "computer", "pc", "desktop", "this computer", "here"):
            hits = [d for d in devs if d.get("type", "").lower() == "computer"]
        elif word in ("phone", "mobile"):
            hits = [d for d in devs if d.get("type", "").lower() == "smartphone"]
        else:
            hits = [d for d in devs if word in d["name"].lower() or word == d.get("type", "").lower()]
        if not hits:
            names = ", ".join(d["name"] for d in devs)
            raise SpotifyError(f"I can't see your {word} in Spotify. Online now: {names}. Open Spotify on it first.")
        return sorted(hits, key=lambda d: not d.get("is_active"))[0]

    def _playing_on(self, device: dict, wait: float) -> bool:
        time.sleep(wait)
        st = self._req("GET", "/me/player") or {}
        return st.get("device", {}).get("id") == device["id"] and bool(st.get("is_playing"))

    def transfer(self, word: str) -> str:
        """Move whatever is playing (or was last playing) to another device, and check it really started."""
        target = self.find_device(word)
        self._req("PUT", "/me/player", body={"device_ids": [target["id"]], "play": True})
        if self._playing_on(target, 1.5):
            return f"Playing on {target['name']} now."
        return (f"Spotify didn't start on {target['name']}. Open the Spotify app on it, make sure it's awake, "
                f"and ask again.")

    # --- playback ---------------------------------------------------------
    def now_playing(self) -> str:
        st = self._req("GET", "/me/player")
        if not st or not st.get("item"):
            return "Nothing is playing on Spotify."
        it = st["item"]
        who = ", ".join(a["name"] for a in it.get("artists", [])) or it.get("show", {}).get("name", "")
        state = "Playing" if st.get("is_playing") else "Paused on"
        return f"{state} {it['name']}" + (f" by {who}" if who else "") + f", on {st['device']['name']}."

    def pause(self) -> str:
        self._req("PUT", "/me/player/pause", self._dev_params())
        return ""

    def resume(self) -> str:
        self._req("PUT", "/me/player/play", self._dev_params())
        return ""

    def next(self) -> str:
        self._req("POST", "/me/player/next", self._dev_params())
        return ""

    def previous(self) -> str:
        self._req("POST", "/me/player/previous", self._dev_params())
        return ""

    def volume(self, percent: int) -> str:
        self._req("PUT", "/me/player/volume", {"volume_percent": max(0, min(100, percent)), **self._dev_params()})
        return f"Music volume {percent}."

    def shuffle(self, on: bool) -> str:
        self._req("PUT", "/me/player/shuffle", {"state": str(on).lower(), **self._dev_params()})
        return f"Shuffle {'on' if on else 'off'}."

    def repeat(self, mode: str) -> str:
        self._req("PUT", "/me/player/repeat", {"state": mode, **self._dev_params()})
        return f"Repeat {mode}."

    def like_current(self) -> str:
        st = self._req("GET", "/me/player/currently-playing")
        if not st or not st.get("item"):
            return "Nothing is playing."
        self._req("PUT", "/me/tracks", {"ids": st["item"]["id"]})
        return f"Saved {st['item']['name']} to your liked songs."

    def search(self, query: str, kinds: str, limit: int = 5, offset: int = 0) -> dict:
        return self._req("GET", "/search", {"q": query, "type": kinds, "limit": limit, "offset": offset}) or {}

    def _my_playlist(self, name: str) -> dict | None:
        name = name.lower()
        res = self._req("GET", "/me/playlists", {"limit": 50}) or {}
        items = [p for p in res.get("items", []) if p]
        return next((p for p in items if p["name"].lower() == name), None) or \
            next((p for p in items if name in p["name"].lower()), None)

    def resolve(self, query: str, kind: str = "auto") -> tuple[dict, str]:
        """Find what to play. Returns (play body, spoken description)."""
        q = query.strip()
        if kind == "track_by":
            title, artist = q.split("|", 1)
            items = self.search(f"track:{title} artist:{artist}", "track", 1).get("tracks", {}).get("items", [])
            if not items:
                return self.resolve(f"{title} {artist}", "track")
            t = items[0]
            return {"uris": [t["uri"]]}, f"{t['name']} by {t['artists'][0]['name']}"
        if kind == "playlist":
            mine = self._my_playlist(q)
            if mine:
                return {"context_uri": mine["uri"]}, f"your playlist {mine['name']}"
        if kind == "auto" and MOODS.search(q):
            kind = "playlist"
        kinds = "track,artist,album,playlist" if kind == "auto" else kind
        res = self.search(q, kinds, 3)
        pick = lambda k: [x for x in res.get(k + "s", {}).get("items", []) if x]  # noqa: E731
        if kind in ("auto", "artist"):
            artists = pick("artist")
            if artists and (kind == "artist" or _same(artists[0]["name"], q)):
                return {"context_uri": artists[0]["uri"]}, f"songs by {artists[0]['name']}"
        if kind in ("auto", "track") and pick("track"):
            t = pick("track")[0]
            return {"uris": [t["uri"]]}, f"{t['name']} by {t['artists'][0]['name']}"
        if kind in ("auto", "album") and pick("album"):
            a = pick("album")[0]
            return {"context_uri": a["uri"]}, f"the album {a['name']} by {a['artists'][0]['name']}"
        if kind in ("auto", "playlist") and pick("playlist"):
            p = pick("playlist")[0]
            return {"context_uri": p["uri"]}, f"the playlist {p['name']}"
        raise SpotifyError(f"I couldn't find {query} on Spotify.")

    # --- your library: matching what you said against what you actually listen to ----------------------
    def library(self) -> dict:
        try:
            idx = json.loads(INDEX_FILE.read_text())
        except (OSError, ValueError):
            idx = {}
        if time.time() - idx.get("t", 0) > 86400 and not self._indexing:  # refresh daily, in the background
            import threading

            self._indexing = True
            threading.Thread(target=self._build_library, daemon=True, name="music-index").start()
        return idx

    def _build_library(self) -> None:
        try:
            tracks, counts, uris = [], {}, {}
            for offset in range(0, 500, 50):
                page = self._req("GET", "/me/tracks", {"limit": 50, "offset": offset}) or {}
                for it in page.get("items", []):
                    t = it.get("track") or {}
                    if t.get("uri"):
                        artist = t["artists"][0]["name"] if t.get("artists") else ""
                        tracks.append([t["name"], artist, t["uri"]])
                        counts[artist] = counts.get(artist, 0) + 1
                        if t.get("artists"):
                            uris[artist] = t["artists"][0].get("uri", "")
                if not page.get("next"):
                    break
            for path in ("/me/player/recently-played", "/me/top/tracks"):  # need the newer login scopes
                try:
                    for it in (self._req("GET", path, {"limit": 50}) or {}).get("items", []):
                        t = it.get("track", it)
                        if t.get("uri"):
                            artist = t["artists"][0]["name"] if t.get("artists") else ""
                            tracks.append([t["name"], artist, t["uri"]])
                            counts[artist] = counts.get(artist, 0) + 2
                            if t.get("artists"):
                                uris[artist] = t["artists"][0].get("uri", "")
                except SpotifyError:
                    pass
            artists = {}
            try:
                for a in (self._req("GET", "/me/top/artists", {"limit": 30}) or {}).get("items", []):
                    artists[a["name"]] = [a["name"], a["uri"], counts.get(a["name"], 0) + 5]
            except SpotifyError:
                pass
            playlists = [[p["name"], p["uri"]] for p in
                         ((self._req("GET", "/me/playlists", {"limit": 50}) or {}).get("items", [])) if p]
            seen, unique = set(), []
            for t in tracks:
                if t[2] not in seen:
                    seen.add(t[2])
                    unique.append(t)
            for name, n in counts.items():
                if name and name not in artists:
                    artists[name] = [name, uris.get(name, ""), n]
            idx = {"t": time.time(), "tracks": unique, "playlists": playlists,
                   "artists": sorted(artists.values(), key=lambda a: -a[2])}
            INDEX_FILE.write_text(json.dumps(idx))
            self.build_catalog()
        except Exception:
            pass
        finally:
            self._indexing = False

    def build_catalog(self) -> None:
        """Weekly: the most popular songs of your artists and CATALOG_ARTISTS, so Batsy recognises them when
        you say them (voice/topics.py) and can match a misheard title by how it sounds."""
        try:
            if time.time() - json.loads(CATALOG_FILE.read_text()).get("t", 0) < 7 * 86400:
                return
        except (OSError, ValueError):
            pass
        lib = self.library()
        names = [a[0] for a in lib.get("artists", [])[:40]] + CATALOG_ARTISTS
        tracks, artists, seen = [], [], set()
        for name in dict.fromkeys(names):  # unique, in order
            try:
                for offset in range(0, 60, 10):  # Spotify allows 10 results per search now
                    items = (self.search(name, "track", 10, offset=offset) or {}).get("tracks", {}).get("items", [])
                    for t in items:
                        by = [a["name"] for a in t.get("artists", [])] if t else []
                        if t and t.get("uri") and t["uri"] not in seen and any(_same(a, name) or name.lower() in a.lower()
                                                                               for a in by):
                            seen.add(t["uri"])
                            tracks.append([t["name"], by[0], t["uri"]])
                    if len(items) < 10:
                        break
                    time.sleep(0.12)
                artists.append(name)
            except SpotifyError:
                time.sleep(2)
            time.sleep(0.15)  # be gentle with the API
        CATALOG_FILE.write_text(json.dumps({"t": time.time(), "tracks": tracks, "artists": artists}))

    def catalog_matches(self, said: str, limit: int = 5) -> list[tuple]:
        """Catalogue songs that SOUND like what was said (Tamil spellings vary): (score, body, desc)."""
        try:
            data = json.loads(CATALOG_FILE.read_text())
        except (OSError, ValueError):
            return []
        from rapidfuzz import fuzz, process

        from batsy.voice.topics import sound_key

        tracks = data.get("tracks", [])
        if not tracks:
            return []
        keys = [sound_key(_clean(t[0]) or t[0]) for t in tracks]  # bare title: no '(From "Master")', ' - Remix'...
        out = []
        def close(a, b, **_):  # whole titles, any word order; a few shared words alone isn't a match
            return (fuzz.ratio(a, b) + fuzz.token_sort_ratio(a, b)) / 2

        for _, score, i in process.extract(sound_key(said), keys, scorer=close, limit=limit):
            name, artist, uri = tracks[i]
            out.append((round(score / 100 * 0.95, 3), {"uris": [uri]}, f"{name} by {artist}"))
        return out

    def candidates(self, query: str, kind: str = "auto") -> list[tuple]:
        """Everything that could be meant, scored by how closely it matches the words: (score, body, desc)."""
        q = query.strip()
        title, _, artist = q.partition("|") if kind == "track_by" else (q, "", "")
        out: list[tuple] = []
        lib = self.library()
        if kind in ("auto", "track", "track_by"):
            for name, art, uri in lib.get("tracks", []):
                sc = _score(title, name, art, artist)
                if sc > 0.5:
                    out.append((sc + 0.05, {"uris": [uri]}, f"{name} by {art}"))  # yours: a small bonus
        if kind in ("auto", "playlist"):
            for name, uri in lib.get("playlists", []):
                sc = _score(q, name)
                if sc > 0.5:
                    out.append((sc + 0.05, {"context_uri": uri}, f"your playlist {name}"))
        if kind in ("auto", "artist"):
            for name, uri, plays in lib.get("artists", []):
                if uri:
                    sc = _score(q, name)
                    if set(_clean(q).split()) <= set(_clean(name).split()):  # "rahman" -> A.R. Rahman, whom you play
                        sc = max(sc, 0.97)
                    if sc > 0.6:
                        out.append((sc + 0.05 + min(0.1, plays * 0.01), {"context_uri": uri}, f"songs by {name}"))
        if kind in ("auto", "track"):
            out += self.catalog_matches(title)  # by sound: "sarasara sara karthi" -> Sara Sara Saara Kathu
        search_q = f"track:{title} artist:{artist}" if kind == "track_by" else q
        kinds = {"auto": "track,artist,album,playlist", "track_by": "track"}.get(kind, kind)
        res = self.search(search_q, kinds, 5)
        if kind == "track_by" and not res.get("tracks", {}).get("items"):
            res = self.search(f"{title} {artist}", "track", 5)
        for t in [x for x in res.get("tracks", {}).get("items", []) if x]:
            out.append((_score(title, t["name"], t["artists"][0]["name"], artist), {"uris": [t["uri"]]},
                        f"{t['name']} by {t['artists'][0]['name']}"))
        for a in [x for x in res.get("artists", {}).get("items", []) if x][:3]:
            out.append((_score(q, a["name"]), {"context_uri": a["uri"]}, f"songs by {a['name']}"))
        for a in [x for x in res.get("albums", {}).get("items", []) if x][:2]:
            out.append((_score(q, a["name"], a["artists"][0]["name"]) - 0.05, {"context_uri": a["uri"]},
                        f"the album {a['name']} by {a['artists'][0]['name']}"))
        for p in [x for x in res.get("playlists", {}).get("items", []) if x][:2]:
            out.append((_score(q, p["name"]) - 0.05, {"context_uri": p["uri"]}, f"the playlist {p['name']}"))
        # One entry per song: other releases of it ("- From the film", "Remastered") and playlists or albums that
        # merely share its title aren't rivals, so they mustn't make Batsy unsure.
        seen, titles, films, ranked = set(), set(), set(), []
        for c in sorted(out, key=lambda c: -c[0]):
            title, _, by = re.sub(r"^(the playlist|your playlist|the album|songs by) ", "", c[2]).rpartition(" by ")
            title = _clean(title or by)
            if "uris" in c[1]:
                key = (title, _clean(by))
                film = 'From "' in c[2] or title in films  # film songs are credited to the composer or the singer
                if film and title in titles:
                    continue
                if 'From "' in c[2]:
                    films.add(title)
                titles.add(title)
            else:
                key = c[1]["context_uri"]
                if title in titles:
                    continue
            if key not in seen:
                seen.add(key)
                ranked.append(c)
        return ranked

    def play(self, query: str, kind: str = "auto", device: str | None = None) -> str:
        if kind == "auto" and MOODS.search(query):  # "something chill": any good playlist will do, no questions
            body, what = self.resolve(query, "playlist")
            return self._start((1.0, body, what), device)
        ranked = self.candidates(query, kind)
        if not ranked:
            raise SpotifyError(f"I couldn't find {query} on Spotify.")
        best = ranked[0]
        clear_winner = len(ranked) > 1 and best[0] >= 0.6 and best[0] - ranked[1][0] >= 0.25
        if best[0] >= SURE or clear_winner or len(ranked) == 1 and best[0] >= 0.6:
            self.last, self.pending = ranked, []
            return self._start(best, device)
        self.pending, self.pending_device = ranked[:3], device  # not sure: ask rather than play the wrong thing
        return self._ask()

    def _ask(self) -> str:
        first = self.pending[0][2]
        if len(self.pending) > 1:
            return f"I'm not sure which one you mean. Is it {first}, or {self.pending[1][2]}?"
        return f"I'm not sure that's right. Is it {first}?"

    def _start(self, cand: tuple, device: str | None = None) -> str:
        """Play one candidate on the chosen device, and check it really started there."""
        target = self.find_device(device) if device else self.device()
        body = cand[1]
        if len(body.get("uris", [])) == 1:  # one song: keep going afterwards, forever (radio)
            more = self._more_like(cand[2], body["uris"][0])
            body = {"uris": body["uris"] + more}
            self._radio_start(cand[2], body["uris"])
        else:
            self.radio = None  # an album / playlist / artist: Spotify plays the whole thing itself
        self._req("PUT", "/me/player/play", {"device_id": target["id"]}, body)
        if self._playing_on(target, 1.2):
            return f"Playing {cand[2]}."
        # Spotify accepted it, but the device didn't start (asleep, app in the background): hand it over explicitly
        self._req("PUT", "/me/player", body={"device_ids": [target["id"]], "play": True})
        if self._playing_on(target, 1.5):
            return f"Playing {cand[2]} on {target['name']}."
        return (f"Spotify didn't start {cand[2]} on {target['name']}. Open the Spotify app on it, "
                f"make sure it's awake, and ask again.")

    # --- radio: the music never runs out ---------------------------------------------------------------------------
    def _radio_start(self, desc: str, queued: list[str]) -> None:
        artist = desc.rpartition(" by ")[2].strip()
        self.radio = {"artist": artist, "tamil": any(_same(artist, a) or a.lower() in artist.lower() for a in TAMIL),
                      "known": set(queued), "paused_since": None}
        if not getattr(self, "_radio_thread", None):
            import threading

            self._radio_thread = threading.Thread(target=self._radio_loop, daemon=True, name="spotify-radio")
            self._radio_thread.start()

    def _radio_loop(self) -> None:
        """Every 20 s while a radio is on: if fewer than 5 songs are left, queue about 10 more like it. Stops when
        you play something else yourself, or after 30 minutes paused."""
        while True:
            time.sleep(20)
            r = self.radio
            if not r:
                continue
            try:
                q = self._req("GET", "/me/player/queue") or {}
                now = (q.get("currently_playing") or {}).get("uri")
                if now and now not in r["known"]:  # you chose something else: step aside
                    self.radio = None
                    continue
                st = self._req("GET", "/me/player") or {}
                if not st.get("is_playing"):
                    r["paused_since"] = r["paused_since"] or time.time()
                    if time.time() - r["paused_since"] > 1800:
                        self.radio = None
                    continue
                r["paused_since"] = None
                left = [x.get("uri") for x in q.get("queue", []) if x and x.get("uri") in r["known"]]
                if len(left) < 5:
                    for uri in self._radio_next(r, 10):
                        self._req("POST", "/me/player/queue", {"uri": uri})
                        r["known"].add(uri)
            except SpotifyError:
                pass
            except Exception:
                pass

    def _radio_next(self, r: dict, n: int) -> list[str]:
        """Songs not played yet: the same artist first, then similar music (Tamil after Tamil), then anything."""
        import random

        try:
            cat = json.loads(CATALOG_FILE.read_text()).get("tracks", [])
        except (OSError, ValueError):
            cat = []
        lib = self.library().get("tracks", [])
        by_artist = lambda a: _same(a, r["artist"]) or r["artist"].lower() in a.lower()  # noqa: E731
        in_group = lambda a: (a in TAMIL or any(t.lower() in a.lower() for t in TAMIL)) == r["tamil"]  # noqa: E731
        fresh = lambda pool: [t[2] for t in pool if t[2] not in r["known"]]  # noqa: E731
        for pool in ([t for t in lib + cat if by_artist(t[1])], [t for t in cat + lib if in_group(t[1])], lib + cat):
            picks = list(dict.fromkeys(fresh(pool)))
            if picks:
                random.shuffle(picks)
                return picks[:n]
        r["known"] = set()  # heard everything: start over
        return []

    def _more_like(self, desc: str, first: str, n: int = 25) -> list[str]:
        """Songs to play after the one you asked for: more by the same artist (your saved ones, the catalogue,
        then Spotify search), shuffled, so the music carries on."""
        import random

        artist = desc.rpartition(" by ")[2].strip()
        if not artist:
            return []
        same = lambda a: _same(a, artist) or artist.lower() in a.lower()  # noqa: E731
        songs = [(t[2], t[0]) for t in self.library().get("tracks", []) if same(t[1])]  # (uri, title)
        try:
            songs += [(t[2], t[0]) for t in json.loads(CATALOG_FILE.read_text()).get("tracks", []) if same(t[1])]
        except (OSError, ValueError):
            pass
        if len(songs) < 10:
            try:
                for offset in (0, 10):
                    items = self.search(artist, "track", 10, offset=offset).get("tracks", {}).get("items", [])
                    songs += [(t["uri"], t["name"]) for t in items if t and any(same(a["name"]) for a in t.get("artists", []))]
            except SpotifyError:
                pass
        # one of each song: other releases of the same title ("From the film", "Remix") and the song you asked
        # for itself don't count as more
        playing = _clean(desc.rpartition(" by ")[0])
        seen, rest = {playing}, []
        for uri, title in songs:
            key = _clean(title) or title.lower()
            if uri != first and key not in seen:
                seen.add(key)
                rest.append(uri)
        random.shuffle(rest)
        return rest[:n]

    def choose(self, n: str) -> str | None:
        """Answers to "Is it X, or Y?" and "not that one" after a play. None if n isn't one."""
        if self.pending:
            pick = {"yes": 0, "yeah": 0, "yep": 0, "yup": 0, "correct": 0, "that one": 0, "the first one": 0,
                    "first": 0, "the first": 0, "the second one": 1, "second": 1, "the second": 1, "the other one": 1,
                    "the other": 1, "the third one": 2, "third": 2, "the third": 2}.get(re.sub(r"^(it'?s|its) ", "", n))
            if pick is not None and pick < len(self.pending):
                cand, device = self.pending[pick], self.pending_device
                self.last, self.pending = self.pending[pick:] + self.pending[:pick], []
                return self._start(cand, device)
            if re.fullmatch(r"(no|nope|neither|neither of them|no it'?s not|not those|none of them)", n):
                self.pending = self.pending[2:] if len(self.pending) > 2 else []
                return self._ask() if self.pending else "Okay. Say the song name and the artist, like: play Numb by Linkin Park."
            self.pending = []  # they moved on to something else
            return None
        if self.last and re.fullmatch(r"(no )?(not (that|this) (one|song)|wrong song|that'?s the wrong song|that'?s not it|"
                                      r"that'?s not the (right )?song|play the other one|different song)", n):
            self.last = self.last[1:]
            if not self.last:
                return "That was the last match I had. Say the song name and the artist."
            return self._start(self.last[0])
        return None


    def queue(self, query: str) -> str:
        body, what = self.resolve(query, "track")
        self._req("POST", "/me/player/queue", {"uri": body["uris"][0], **self._dev_params()})
        return f"Queued {what}."


def _clean(s: str) -> str:
    s = re.sub(r"\(.*?\)|\[.*?\]| [-\u2013\u2014] .*$|feat\..*$", "", s.lower())  # "(feat. X)", " - Remastered 2011"
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", s).split())


def _score(said: str, name: str, artist: str = "", said_artist: str = "") -> float:
    """0..1: how well a title (and artist) matches what was said."""
    from difflib import SequenceMatcher

    q, nm, ar = _clean(said), _clean(name), _clean(artist)
    if not q or not nm:
        return 0.0
    forms = [nm, f"{nm} {ar}".strip(), f"{ar} {nm}".strip()]
    ratio = max(SequenceMatcher(None, q, f).ratio() for f in forms)
    words = set(q.split())
    overlap = len(words & (set(nm.split()) | set(ar.split()))) / len(words)
    score = max(ratio, 0.95 * overlap if len(words) > 1 or q == nm else 0)
    if said_artist:  # "X by Y": the artist has to match too
        score = 0.6 * score + 0.4 * SequenceMatcher(None, _clean(said_artist), ar).ratio()
    return round(score, 3)


def _same(a: str, b: str) -> bool:
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())  # noqa: E731
    return norm(a) == norm(b)


def parse_play(q: str) -> tuple[str, str]:
    """'the album x' -> (album, x); 'x by y' -> (track_by, 'x|y'); 'songs by y' -> (artist, y) ..."""
    q = re.sub(r"^(some|a|the song|song)\s+", "", q.strip())
    for pattern, kind in [
        (r"^(?:the )?album (.+)$", "album"),
        (r"^(?:my )?playlist (.+)$", "playlist"),
        (r"^(?:my |the )?(.+?) playlist$", "playlist"),
        (r"^(?:songs|music|tracks|something) (?:by|from) (.+)$", "artist"),
        (r"^(?:the )?artist (.+)$", "artist"),
    ]:
        m = re.match(pattern, q)
        if m:
            return kind, m.group(1)
    m = re.match(r"^(.+?) by (.+)$", q)
    if m:
        return "track_by", f"{m.group(1)}|{m.group(2)}"
    return "auto", q


def handle_local(sp: Spotify, n: str) -> str | None:
    """Zero-token handling of a normalised utterance. Returns the reply ('' = silent), or None if not music."""
    said = sp.choose(n)  # "yes" / "the second one" / "not that one"
    if said is not None:
        return said
    m = re.fullmatch(rf"(?:please )?(?:play|move|switch|transfer|put|continue|resume|send)(?: it| this| that| the music| music|"
                     rf" spotify| the song| this song| playback| the current song)?(?: (?:on|to|onto|in|over to|on to)"
                     rf" (?:my |the )?{DEVICE_WORDS})(?: instead| please| now)?", n)
    if m:
        return sp.transfer(m.group(1))
    if re.fullmatch(r"(pause|stop)( the)?( music| song| spotify| playback)?", n) and n != "stop":
        return sp.pause()
    if re.fullmatch(r"(resume|unpause|continue|play)( the)?( music| song| spotify)?( please)?|(resume|continue) playing", n):
        return sp.resume()
    if re.fullmatch(r"(next|skip)( song| track| this| this song| it)?( please)?|play the next song", n):
        return sp.next()
    if re.fullmatch(r"(previous|last|go back)( song| track)?|play the (previous|last) song", n):
        return sp.previous()
    if re.fullmatch(r"what('s| is) (playing|this song|the song|this)|what song is (this|playing)|"
                    r"who (sings|is singing) this( song)?|what am i listening to|which song is this", n):
        return sp.now_playing()
    if re.fullmatch(r"(like|save|love|heart) (this|the|this) song|add this( song)? to (my )?(liked songs|likes|library)", n):
        return sp.like_current()
    m = re.fullmatch(r"(set )?(the )?(music|spotify|ipad) volume (to )?(\d{1,3})( percent)?", n)
    if m:
        return sp.volume(int(m.group(5)))
    m = re.fullmatch(r"(turn )?shuffle (on|off)", n)
    if m:
        return sp.shuffle(m.group(2) == "on")
    if re.fullmatch(r"(play )?(something else|another (one|song)|a different (one|song)|some other song)( please)?", n):
        return sp.choose("not that one") or sp.next()  # the next best match for the last request, or just skip
    m = re.fullmatch(r"(play|put on|queue|add) (.+?)( on spotify)?( to the queue| next)?( please)?", n)
    if m and not NOT_MUSIC.search(m.group(2)):
        what, device = m.group(2), None
        d = re.search(rf"\s+(?:on|in|to|onto) (?:my |the )?{DEVICE_WORDS}(?: not .*| instead)?$", what)
        if d:  # "play numb on my ipad": a device, not part of the song name
            what, device = what[:d.start()].strip(), d.group(1)
        what = re.sub(r"\s+(?:on|in) spotify\b.*$", "", what).strip()
        if not what or GENERIC.match(what):  # "play spotify on my ipad", "play the current song"
            return sp.transfer(device) if device else sp.resume()
        if m.group(1) in ("queue", "add") or m.group(4):
            if m.group(1) == "add" and not m.group(4):
                return None  # "add X" without "to the queue" is ambiguous; let Claude handle it
            return sp.queue(what)
        kind, query = parse_play(what)
        return sp.play(query, kind, device)
    return None
