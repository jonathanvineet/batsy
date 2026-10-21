"""Every Claude Code (Pro plan) call goes through here: exact token accounting, Batsy's own budgets, and the
"running agents" list.

Pro's real limits aren't available outside the official app, so Batsy keeps its own: a session budget over a
rolling 5-hour window (like Pro's) and a weekly budget (calendar week). Near a limit it warns once; at the limit
it refuses and Batsy answers with the local brain until the window resets (unless forced).
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time

from batsy import activity
from batsy.config import DATA_DIR, WORKSPACE

log = logging.getLogger("batsy.claude")
USAGE_FILE = DATA_DIR / "usage.jsonl"
LIMITS_FILE = DATA_DIR / "claude_limits.json"
_warned: set[str] = set()
_lock = threading.Lock()
announce = None  # set by the app: speak a warning
CFG = None       # set by the app


def limits(cfg) -> dict:
    lim = dict(cfg.get("claude_budget"))
    if LIMITS_FILE.exists():
        try:
            lim.update(json.loads(LIMITS_FILE.read_text()))
        except ValueError:
            pass
    return lim


def set_limit(which: str, tokens: int) -> None:
    data = json.loads(LIMITS_FILE.read_text()) if LIMITS_FILE.exists() else {}
    data[f"{which}_tokens"] = int(tokens)
    LIMITS_FILE.write_text(json.dumps(data))


def _rows() -> list[dict]:
    if not USAGE_FILE.exists():
        return []
    out = []
    for line in USAGE_FILE.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("model", "").startswith("claude-code"):
            out.append(r)
    return out


def _tokens(r: dict) -> int:
    return r["in"] + r["out"] + r["cache_read"] + r["cache_write"]


def budget(cfg) -> dict:
    """{session: {used, limit, resets_at, pct}, week: {...}}"""
    lim = limits(cfg)
    rows = _rows()
    now = time.time()
    hours = lim["session_hours"] * 3600
    # Pro-style session: starts with the first request after the previous window ended
    start = None
    for r in rows:
        if start is None or r["t"] >= start + hours:
            start = r["t"]
    if start is None or now >= start + hours:
        s_used, s_reset = 0, None
    else:
        s_used, s_reset = sum(_tokens(r) for r in rows if r["t"] >= start), start + hours
    lt = time.localtime(now)
    week_start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - lt.tm_wday, 0, 0, 0, 0, 0, -1))
    w_used = sum(_tokens(r) for r in rows if r["t"] >= week_start)

    def pack(used, limit, reset):
        return {"used": used, "limit": limit, "resets_at": reset,
                "pct": round(100 * used / limit, 1) if limit else None}

    return {"session": pack(s_used, lim["session_tokens"], s_reset),
            "week": pack(w_used, lim["weekly_tokens"], week_start + 7 * 86400),
            "requests_week": sum(1 for r in rows if r["t"] >= week_start)}


def over_budget(cfg) -> str | None:
    """A sentence explaining why Claude is off limits right now, or None."""
    b = budget(cfg)
    for key, label in (("session", "this session's"), ("week", "this week's")):
        x = b[key]
        if x["limit"] and x["used"] >= x["limit"]:
            when = time.strftime("%-I:%M %p" if key == "session" else "%A", time.localtime(x["resets_at"]))
            return f"I've used {label} Claude budget, so I'll handle things myself until {when}."
    return None


def _warn(cfg) -> None:
    b = budget(cfg)
    warn_at = limits(cfg)["warn_at"]
    for key in ("session", "week"):
        x = b[key]
        tag = f"{key}-{x['resets_at']}"
        if x["limit"] and x["used"] >= warn_at * x["limit"] and tag not in _warned and announce:
            _warned.add(tag)
            announce(f"Heads up: {x['pct']:.0f} percent of your {key} Claude budget is used.")


def run(prompt: str, label: str, args: list[str] | None = None, cwd=WORKSPACE, timeout: int = 300,
        force: bool = False, cfg=None) -> str:
    """Run `claude -p` and return its text answer. Logs exact usage and respects budgets."""
    cfg = cfg or CFG
    exe = shutil.which("claude")
    if not exe:
        return "Claude Code isn't installed. Run: npm install -g @anthropic-ai/claude-code, then claude to log in."
    if not force and cfg is not None:
        why = over_budget(cfg)
        if why:
            return why
    env = dict(os.environ, PATH=f"{os.path.dirname(sys.executable)}:{os.path.expanduser('~/.local/bin')}:"
                                f"{os.environ.get('PATH', '')}")
    with activity.agent(label):
        try:
            p = subprocess.run([exe, "-p", prompt, "--output-format", "json", *(args or [])], cwd=cwd,
                               capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return "That took too long."
    try:
        data = json.loads(p.stdout)
    except ValueError:
        return (p.stdout.strip() or p.stderr.strip() or "Claude didn't answer.")[:3000]
    u = data.get("usage") or {}
    row = {"t": int(time.time()), "model": "claude-code", "label": label,
           "in": int(u.get("input_tokens", 0)), "out": int(u.get("output_tokens", 0)),
           "cache_read": int(u.get("cache_read_input_tokens", 0)),
           "cache_write": int(u.get("cache_creation_input_tokens", 0)),
           "secs": round(data.get("duration_ms", 0) / 1000, 1), "turns": data.get("num_turns")}
    with _lock, open(USAGE_FILE, "a") as f:
        f.write(json.dumps(row) + "\n")
    log.info("claude-code %s: %s", label, row)
    if cfg is not None:
        _warn(cfg)
    return str(data.get("result") or "").strip() or "Claude didn't answer."
