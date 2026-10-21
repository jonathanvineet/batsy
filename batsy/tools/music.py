"""Spotify tool for requests the local parser doesn't cover (moods, several songs, picking devices...)."""
from batsy.spotify import Spotify, SpotifyError

_sp: Spotify | None = None


def get_spotify(cfg) -> Spotify:
    global _sp
    if _sp is None:
        _sp = Spotify(cfg)
    return _sp


def register(reg):
    @reg.tool(
        "spotify",
        "Control Spotify playing on the user's iPad (via Spotify Connect; not the laptop, so never use playerctl "
        "for music). Actions: now_playing, play (query + kind), queue (query), pause, resume, next, previous, "
        "volume (value 0-100), shuffle (value 1/0), repeat (query: track|context|off), like, devices. "
        "kind: auto|track|artist|album|playlist (playlist checks the user's own playlists first).",
        {
            "action": {"type": "string", "enum": ["now_playing", "play", "queue", "pause", "resume", "next",
                                                   "previous", "volume", "shuffle", "repeat", "like", "devices"]},
            "query": {"type": "string"},
            "kind": {"type": "string", "enum": ["auto", "track", "artist", "album", "playlist"]},
            "value": {"type": "integer"},
        },
        ["action"],
    )
    def spotify(ctx, action, query=None, kind="auto", value=None):
        sp = get_spotify(ctx.cfg)
        if not sp.ready():
            return "Spotify isn't connected. The user must set [spotify] client_id and run `batsy spotify-login`."
        try:
            if action == "play":
                return sp.play(query or "", kind) if query else (sp.resume() or "Resumed.")
            if action == "queue":
                return sp.queue(query or "")
            if action == "volume":
                return sp.volume(value if value is not None else 50)
            if action == "shuffle":
                return sp.shuffle(bool(value))
            if action == "repeat":
                return sp.repeat(query or "context")
            if action == "like":
                return sp.like_current()
            if action == "devices":
                return "\n".join(f"{d['name']} ({d['type']}){' active' if d.get('is_active') else ''}"
                                 for d in sp.devices()) or "No devices online."
            return getattr(sp, action)() or "Done."
        except SpotifyError as e:
            return f"ERROR: {e}"
