"""Paths and user configuration.

Config lives in ~/.config/batsy/config.toml (every key optional), the API key in
~/.config/batsy/env (ANTHROPIC_API_KEY=...) or the environment.
"""
import os
import tomllib
from pathlib import Path

HOME = Path.home()
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "batsy"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local" / "share")) / "batsy"
WORKSPACE = HOME / "Batsy"  # where Batsy keeps sketches, scripts and scratch work
SKILLS_DIR = DATA_DIR / "skills"
MODELS_DIR = DATA_DIR / "models"
JOBS_DIR = DATA_DIR / "jobs"
SOCKET_PATH = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / f"batsy-{os.getuid()}.sock"
SOURCE_DIR = Path(__file__).resolve().parent

DEFAULTS = {
    "claude": {
        "model": "claude-opus-5-5",
        # low = fewest thinking tokens and fastest replies; "medium"/"high" for heavy coding sessions
        "effort": "low",
        "max_tokens": 32000,
        "web_tools": True,
        "max_steps": 25,
        # token savers
        # (memory is local, so these only matter inside one long multi-step request)
        "compact_at_tokens": 50000,    # summarise past this size (API minimum 50000)
        "clear_tool_results_at": 20000,  # drop old tool outputs/images past this size
        "keep_tool_results": 3,        # ...but keep the most recent few
        "cache_ttl": "1h",             # voice use is bursty; a 1h cache survives the gaps between questions
    },
    "memory": {
        "recent_exchanges": 6,     # last N back-and-forths sent with each request
        "recent_minutes": 180,     # ...if they happened within this window
        "related_exchanges": 3,    # older exchanges found by local search
        "all_facts_below": 15,     # send every remembered fact while there are this few...
        "max_facts": 8,            # ...otherwise only the most relevant ones
    },
    "spotify": {
        "client_id": "",        # from developer.spotify.com (see README)
        "device": "ipad",       # preferred Spotify Connect device (name contains this)
        "redirect_port": 8888,  # login callback http://127.0.0.1:8888/callback
    },
    "routines": {
        "day_starts_at": 5,   # before this hour it's still "tonight": good night at 3 AM asks about today's date
        "morning_extra": "",  # optional Claude prompt after the morning briefing, e.g. "weather in Chennai today, one line"
        "custom": {},         # presets, see README
    },
    "overlay": {
        "enabled": True,              # full-screen Batsy view while you talk to it
        "linger_seconds": 6,          # stay up this long after a spoken-only answer
        "linger_media_seconds": 30,   # ...or after showing a picture / card
        "live_words": True,           # show your words as you speak (local Vosk, no tokens)
        "qt_platform": "",            # e.g. "xcb" to force XWayland if the window won't come to the front
    },
    "camera": {
        "name": "lenovo",       # matched against the V4L2 device name; first camera if none match
        "device": None,         # force /dev/videoN by number
        "snapshot_edge": 768,   # px sent to Claude per webcam look (~450 tokens)
        "screen_edge": 1280,    # px per screenshot (readable text, ~1200 tokens)
    },
    "voice": {
        "wake_words": ["batsy", "bat see", "bats see", "betsy", "batsie", "bitsy", "patsy"],
        "vosk_model": "vosk-model-small-en-us-0.15",
        "stt_engine": "auto",  # auto | whisper | vosk
        "whisper_model": "small.en",  # tiny.en / base.en are faster, small.en more accurate
        "tts_engine": "auto",  # auto | piper | espeak
        "piper_voice": "en_GB-alan-medium",
        "follow_up_seconds": 6,
        "barge_in": True,
        "input_device": None,  # sounddevice device name/index; None = system default
        "chime": True,
    },
    "safety": {
        "confirm_dangerous": True,
    },
    "monitor": {
        "health_interval": 60,
        "battery_low_percent": 15,
        "cpu_temp_high_c": 90,
        "disk_free_low_gb": 5,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class Config:
    def __init__(self, data: dict):
        self.data = data

    def get(self, dotted: str):
        node = self.data
        for part in dotted.split("."):
            node = node[part]
        return node

    @classmethod
    def load(cls) -> "Config":
        for d in (CONFIG_DIR, DATA_DIR, WORKSPACE, SKILLS_DIR, MODELS_DIR, JOBS_DIR):
            d.mkdir(parents=True, exist_ok=True)
        _load_env_file(CONFIG_DIR / "env")
        data = DEFAULTS
        cfg_file = CONFIG_DIR / "config.toml"
        if cfg_file.exists():
            with open(cfg_file, "rb") as f:
                data = _merge(DEFAULTS, tomllib.load(f))
        return cls(data)
