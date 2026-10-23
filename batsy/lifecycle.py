"""When and why this Batsy started, so it can say it's back after a restart and answer "have you restarted?".

A self-restart replaces the process in place (same PID, same process start time), so the process can't tell on
its own. Before restarting, app.restart() puts the reason in BATSY_RESTART_REASON; the new run picks it up here
and records it in lifecycle.json.
"""
import json
import os
import time

from batsy.config import DATA_DIR

STATE = DATA_DIR / "lifecycle.json"


def _load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def started() -> str:
    """Record this start and return what to say: "Batsy online." or "I'm back. ..." after a restart."""
    reason = os.environ.pop("BATSY_RESTART_REASON", None)
    prev = _load()
    now = time.time()
    record = {"started": now, "reason": reason or "start", "previous": {k: prev.get(k) for k in ("started", "reason")}}
    try:
        STATE.write_text(json.dumps(record))
    except OSError:
        pass
    if not reason:
        return "Batsy online."
    if reason.startswith("update:") and "files changed" in reason:
        return f"I'm back, updated. {reason.split(':', 1)[1].strip().capitalize()}."
    if reason == "update: already up to date":
        return "I'm back. There was nothing new to update."
    return "I'm back."


def _ago(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return "just now" if seconds < 20 else f"{seconds} seconds ago"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} minutes ago"
    hours = minutes // 60
    return f"about {hours} hour{'s' if hours != 1 else ''} ago"


def describe() -> str:
    """For "have you restarted?" / "how long have you been running?"."""
    s = _load()
    if not s.get("started"):
        return "I don't have a record of when I started."
    when = time.strftime("%-I:%M %p", time.localtime(s["started"]))
    ago = _ago(time.time() - s["started"])
    reason = s.get("reason") or "start"
    if reason == "start":
        return f"I started fresh {ago}, at {when}. I haven't restarted since."
    if reason.startswith("update:"):
        detail = reason.split(":", 1)[1].strip()
        return f"Yes, I restarted {ago}, at {when}, after updating: {detail}."
    return f"Yes, I restarted {ago}, at {when}."
