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

from batsy.config import CONFIG_DIR

API = "https://api.spotify.com/v1"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
TOKEN_FILE = CONFIG_DIR / "spotify_token.json"
SCOPES = ("user-read-playback-state user-modify-playback-state user-read-currently-playing "
          "playlist-read-private user-library-read user-library-modify")
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

    def search(self, query: str, kinds: str, limit: int = 5) -> dict:
        return self._req("GET", "/search", {"q": query, "type": kinds, "limit": limit}) or {}

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

    def play(self, query: str, kind: str = "auto") -> str:
        body, what = self.resolve(query, kind)
        self._req("PUT", "/me/player/play", self._dev_params(), body)
        return f"Playing {what}."

    def queue(self, query: str) -> str:
        body, what = self.resolve(query, "track")
        self._req("POST", "/me/player/queue", {"uri": body["uris"][0], **self._dev_params()})
        return f"Queued {what}."


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
    m = re.fullmatch(r"(play|put on|queue|add) (.+?)( on spotify| on the ipad| on ipad)?( to the queue| next)?( please)?", n)
    if m and not NOT_MUSIC.search(m.group(2)):
        if m.group(1) in ("queue", "add") or m.group(4):
            if m.group(1) == "add" and not m.group(4):
                return None  # "add X" without "to the queue" is ambiguous; let Claude handle it
            return sp.queue(m.group(2))
        kind, query = parse_play(m.group(2))
        return sp.play(query, kind)
    return None
