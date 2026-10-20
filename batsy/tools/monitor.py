"""Watchers that run in the background and speak up: timers, custom monitors, and a health check."""
import json
import subprocess
import threading
import time
import uuid

import psutil

from batsy.config import DATA_DIR

STATE_FILE = DATA_DIR / "monitors.json"
_lock = threading.RLock()
_items: dict[str, dict] = {}


def _save() -> None:
    with _lock:
        data = json.dumps(_items, indent=1)
    STATE_FILE.write_text(data)


def _load() -> None:
    if STATE_FILE.exists():
        try:
            _items.update(json.loads(STATE_FILE.read_text()))
        except ValueError:
            pass


def _check(item: dict) -> tuple[bool, str]:
    p = subprocess.run(["bash", "-lc", item["command"]], capture_output=True, text=True,
                       timeout=max(10, item["interval"]), errors="replace")
    out = p.stdout.strip()
    mode = item["mode"]
    if mode == "until_success":
        return p.returncode == 0, out
    if mode == "until_failure":
        return p.returncode != 0, out
    # on_change: fire whenever output differs from the last run
    changed = item.get("last_output") is not None and out != item["last_output"]
    item["last_output"] = out
    return changed, out


def _loop(ctx) -> None:
    while True:
        now = time.time()
        for mid, item in list(_items.items()):
            if now < item["next_run"]:
                continue
            if item["kind"] == "timer":
                ctx.announce(item["message"])
                with _lock:
                    _items.pop(mid, None)
                _save()
                continue
            item["next_run"] = now + item["interval"]
            try:
                fired, out = _check(item)
            except subprocess.TimeoutExpired:
                continue
            if fired:
                ctx.announce(f"{item['message']} {out[:200]}".strip() if item.get("speak_output") else item["message"])
                if not item.get("repeat"):
                    with _lock:
                        _items.pop(mid, None)
            _save()
        time.sleep(1)


class _Health:
    """Built-in alerts: low battery, overheating, disk filling up. Each alert fires once until it clears."""

    def __init__(self, ctx):
        self.ctx, self.active = ctx, set()

    def _flag(self, key: str, condition: bool, message: str) -> None:
        if condition and key not in self.active:
            self.active.add(key)
            self.ctx.announce(message)
        elif not condition:
            self.active.discard(key)

    def run(self) -> None:
        cfg = self.ctx.cfg
        while True:
            try:
                b = psutil.sensors_battery()
                if b:
                    low = cfg.get("monitor.battery_low_percent")
                    self._flag("battery", not b.power_plugged and b.percent <= low,
                               f"Heads up, battery is at {b.percent:.0f} percent. Plug in soon.")
                temps = psutil.sensors_temperatures() if hasattr(psutil, "sensors_temperatures") else {}
                hottest = max((e.current for es in (temps or {}).values() for e in es), default=0)
                limit = cfg.get("monitor.cpu_temp_high_c")
                self._flag("temp", hottest >= limit, f"Warning, the laptop is running hot at {hottest:.0f} degrees.")
                free_gb = psutil.disk_usage("/").free / 1e9
                self._flag("disk", free_gb < cfg.get("monitor.disk_free_low_gb"),
                           f"Disk space is low, only {free_gb:.1f} gigabytes free.")
            except Exception:
                pass
            time.sleep(cfg.get("monitor.health_interval"))


def start_background(ctx) -> None:
    _load()
    threading.Thread(target=_loop, args=(ctx,), daemon=True, name="monitors").start()
    threading.Thread(target=_Health(ctx).run, daemon=True, name="health").start()


def register(reg):
    @reg.tool(
        "set_timer",
        "Set a timer or reminder. Batsy speaks `message` when it fires. Survives restarts.",
        {
            "seconds": {"type": "number", "description": "delay from now"},
            "message": {"type": "string", "description": "what to say when it fires, e.g. 'Time to take the pizza out.'"},
        },
        ["seconds", "message"],
    )
    def set_timer(ctx, seconds, message):
        mid = uuid.uuid4().hex[:6]
        with _lock:
            _items[mid] = {"kind": "timer", "message": message, "next_run": time.time() + seconds,
                           "created": time.time()}
        _save()
        return f"Timer {mid} set for {seconds:.0f}s from now."

    @reg.tool(
        "add_monitor",
        "Watch something in the background by running a bash check every `interval` seconds, and speak "
        "`message` when it triggers. Modes: until_success (exit 0 = trigger, e.g. 'ping -c1 -W1 host'), "
        "until_failure (non-zero = trigger, e.g. 'pgrep -x firefox'), on_change (stdout changed, e.g. "
        "'ls ~/Downloads'). Monitors persist across restarts.",
        {
            "name": {"type": "string"},
            "command": {"type": "string"},
            "interval": {"type": "integer", "description": "seconds between checks (default 30)"},
            "mode": {"type": "string", "enum": ["until_success", "until_failure", "on_change"]},
            "message": {"type": "string"},
            "repeat": {"type": "boolean", "description": "keep watching after it fires (default false)"},
            "speak_output": {"type": "boolean", "description": "read the command's output after the message"},
        },
        ["name", "command", "mode", "message"],
    )
    def add_monitor(ctx, name, command, mode, message, interval=30, repeat=False, speak_output=False):
        mid = uuid.uuid4().hex[:6]
        with _lock:
            _items[mid] = {"kind": "monitor", "name": name, "command": command, "mode": mode, "message": message,
                           "interval": max(2, interval), "repeat": repeat, "speak_output": speak_output,
                           "next_run": time.time(), "created": time.time()}
        _save()
        return f"Monitor {mid} '{name}' is running every {interval}s."

    @reg.tool(
        "monitors",
        "List or remove timers and monitors.",
        {"action": {"type": "string", "enum": ["list", "remove"]}, "id": {"type": "string"}},
        ["action"],
    )
    def monitors(ctx, action, id=None):
        if action == "remove":
            with _lock:
                removed = _items.pop(id or "", None)
            _save()
            return f"Removed {id}." if removed else f"No monitor {id}."
        if not _items:
            return "No timers or monitors."
        rows = []
        with _lock:
            snapshot = list(_items.items())
        for mid, it in snapshot:
            if it["kind"] == "timer":
                rows.append(f"[{mid}] timer in {max(0, it['next_run'] - time.time()):.0f}s: {it['message']}")
            else:
                rows.append(f"[{mid}] {it['name']} ({it['mode']}, every {it['interval']}s): {it['command']}")
        return "\n".join(rows)
