"""The phone app's link to Batsy: one small HTTP API for the Android widgets (android/ in this repo).

GET  /api/status   everything the widgets show: the laptop and PC (CPU, memory, temperature, battery, disk,
                   network), your Claude budget, what Spotify is playing, and what Batsy is doing right now
POST /api/spotify  {"action": "toggle" | "next" | "previous"}
POST /api/batsy    {"action": "stop"} (like "~") or {"action": "ask", "text": "..."} (type to Batsy; returns the reply)
POST /api/pc       {"action": "wake"}
GET  /app/...      the app's pages (batsy/phone_ui/): the app is a full-screen shell that shows these, so pages
                   change on the laptop without reinstalling anything
GET  /api/files    files received over Tailscale (batsy/received.py), newest first
POST /api/files    {"action": "open", "path": ...}: open a received file on the laptop
POST /api/home     {"action": "on" | "off" | "night"}: the light
POST /api/pc       {"action": "wake" | "lock" | "sleep" | "restart" | "shutdown"}
GET  /app.apk      the app itself, to install from the phone's browser
POST /api/pair     {"code": "123456"} -> {"token": ...}: the code `batsy phone` shows

Two ways in, the app tries both:
- USB: the phone is plugged into this laptop (adb from android-tools), and Batsy runs `adb reverse tcp:8765
  tcp:8765`, so the app just talks to 127.0.0.1:8765. Plugged into the PC instead, Batsy also keeps an SSH tunnel
  PC:8765 -> laptop:8765 and runs adb there. It even installs and pairs the app over USB ("install the widget app
  on my phone").
- Tailscale: anywhere else, http://<laptop's Tailscale name>:8765.
Every request needs the token from pairing; nothing is served to anyone else.
"""
import datetime as dt
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil

from batsy.config import DATA_DIR, SOURCE_DIR

log = logging.getLogger("batsy.phone")
TOKEN_FILE = DATA_DIR / "phone_token"
APK = SOURCE_DIR.parent / "android" / "dist" / "batsy-widgets.apk"
UI = SOURCE_DIR / "phone_ui"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".svg": "image/svg+xml", ".png": "image/png", ".woff2": "font/woff2", ".json": "application/json", ".webmanifest": "application/manifest+json"}
PACKAGE = "dev.batsy.widgets"

# What the PC reports about itself, as JSON, from one SSH call (Ubuntu: /proc and /sys only, no extra packages).
def _pc_probe() -> str:
    return r"""python3 -c '
import json, os, time, shutil, glob
def cpu():
    a = [int(x) for x in open("/proc/stat").readline().split()[1:]]
    return sum(a), a[3] + a[4]
def net():
    rx = tx = 0
    for line in open("/proc/net/dev").readlines()[2:]:
        name, data = line.split(":", 1)
        if name.strip() == "lo": continue
        f = data.split(); rx += int(f[0]); tx += int(f[8])
    return rx, tx
t1, i1 = cpu(); r1, x1 = net(); time.sleep(0.5); t2, i2 = cpu(); r2, x2 = net()
mem = dict((l.split(":")[0], int(l.split()[1])) for l in open("/proc/meminfo"))
total, avail = mem["MemTotal"], mem.get("MemAvailable", mem["MemFree"])
temps = []
for p in glob.glob("/sys/class/hwmon/hwmon*/temp*_input") + glob.glob("/sys/class/thermal/thermal_zone*/temp"):
    try: temps.append(int(open(p).read()) / 1000)
    except Exception: pass
du = shutil.disk_usage("/")
gpu = None
if shutil.which("nvidia-smi"):
    try:
        out = os.popen("nvidia-smi --query-gpu=utilization.gpu,temperature.gpu --format=csv,noheader,nounits").read()
        u, t = out.split(",")[:2]; gpu = {"util": float(u), "temp": float(t)}
    except Exception: pass
print(json.dumps({"cpu": round(100 * (1 - (i2 - i1) / max(1, t2 - t1)), 1), "cores": os.cpu_count(),
    "ram": round(100 * (1 - avail / total), 1), "ram_used_gb": round((total - avail) / 1048576, 1),
    "ram_total_gb": round(total / 1048576, 1), "temp": round(max([t for t in temps if 0 < t < 120] or [0])) or None,
    "disk": round(100 * du.used / du.total), "disk_free_gb": round(du.free / 1e9),
    "net_down_kbps": round((r2 - r1) * 2 / 1024), "net_up_kbps": round((x2 - x1) * 2 / 1024),
    "uptime_s": int(float(open("/proc/uptime").read().split()[0])), "gpu": gpu}))
'"""


def _ai_speed() -> dict:
    """How quick the local AI has been today: median seconds to the first word and to the end, and how many."""
    import sqlite3
    import statistics

    try:
        db = sqlite3.connect(str(DATA_DIR / "requests.db"), timeout=2)
        rows = db.execute("SELECT secs, first FROM requests WHERE t > ? AND route IN ('quick', 'full')",
                          (time.time() - 86400,)).fetchall()
        db.close()
    except Exception:
        return {}
    first = [f for _, f in rows if f is not None and f > 0.2]  # (not "One moment.", which is instant)
    return {"answers_today": len(rows), "first_s": round(statistics.median(first), 1) if first else None,
            "done_s": round(statistics.median([s for s, _ in rows]), 1) if rows else None}


def token() -> str:
    if not TOKEN_FILE.exists():
        TOKEN_FILE.write_text(secrets.token_urlsafe(24))
        os.chmod(TOKEN_FILE, 0o600)
    return TOKEN_FILE.read_text().strip()


class Hub:
    """Owned by the app: the HTTP server, the PC/USB link, and fresh stats while the phone is watching."""

    def __init__(self, app):
        self.app, self.cfg = app, app.cfg
        self.port = int(self.cfg.get("phone.port"))
        self.pair_code: tuple[str, float] | None = None
        from batsy.cloud import Drive

        self.drive = Drive(self.cfg)   # the Batman drive on the PC, as cloud storage
        self.last_seen = 0.0          # last request from the phone: stats are only gathered while it's looking
        self.pc_stats: dict | None = None
        self.pc_checked = 0.0
        self._net = (psutil.net_io_counters(), time.time())
        psutil.cpu_percent(None)      # first call primes the counter
        self._tunnel: subprocess.Popen | None = None
        self.usb = {"phone": False, "reverse": False, "checked": 0.0}

    # --- start ------------------------------------------------------------------------------------------
    def start(self) -> None:
        handler = _handler(self)
        try:
            self.server = ThreadingHTTPServer((self.cfg.get("phone.bind"), self.port), handler)
        except OSError as e:
            log.warning("phone API unavailable on port %d: %s", self.port, e)
            return
        threading.Thread(target=self.server.serve_forever, daemon=True, name="phone-api").start()
        log.info("phone API on port %d", self.port)
        if self.cfg.get("phone.usb"):
            threading.Thread(target=self._usb_loop, daemon=True, name="phone-usb").start()

    # --- USB: the phone plugged into this laptop (or into the PC) ----------------------------------------
    def _pc(self):
        pc = getattr(self.app, "pc", None)
        return pc if pc and pc.configured else None

    def _sh(self, where: str, command: str, timeout: float = 15) -> tuple[int, str]:
        """Run a shell command here ("laptop") or on the PC ("pc"). (exit code, output)"""
        if where == "pc":
            return self._pc().run(command, timeout=timeout)
        try:
            p = subprocess.run(["bash", "-c", command], capture_output=True, text=True, timeout=timeout)
            return p.returncode, (p.stdout + p.stderr).strip()
        except subprocess.TimeoutExpired:
            return 124, "timed out"

    def _phone_on(self) -> str | None:
        """Where the phone is plugged in with USB debugging allowed: "laptop", "pc", or None."""
        if shutil.which("adb") and self._sh("laptop", "adb devices | grep -qw device", 8)[0] == 0:
            return "laptop"
        if self._pc() and self._sh("pc", "adb devices | grep -qw device", 8)[0] == 0:
            return "pc"
        return None

    def _usb_loop(self) -> None:
        """Keep the cable path open. Plugged into the laptop: `adb reverse` here, and the app reaches Batsy at
        127.0.0.1:8765 directly. Plugged into the PC: an SSH tunnel PC -> laptop plus `adb reverse` on the PC.
        `adb reverse` is lost on every reconnect, so it's re-applied every few seconds."""
        while True:
            try:
                if shutil.which("adb") and time.time() - getattr(self, "_adb_fixed", 0) > 60:
                    # a phone plugged in after adb's server started shows "no permissions" until it restarts
                    out = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=8).stdout
                    if "no permissions" in out:
                        self._adb_fixed = time.time()
                        subprocess.run(["adb", "kill-server"], capture_output=True, timeout=8)
                        subprocess.run(["adb", "start-server"], capture_output=True, timeout=15)
                        log.info("phone usb: restarted adb (it had no permission for the phone)")
                where = self._phone_on()
                if where == "pc" and (self._tunnel is None or self._tunnel.poll() is not None):
                    pc = self._pc()
                    self._tunnel = subprocess.Popen(
                        pc.ssh_args()[:-1] + ["-N", "-o", "ExitOnForwardFailure=yes", "-R",
                                              f"127.0.0.1:{self.port}:127.0.0.1:{self.port}", pc.host],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                ok = bool(where) and self._sh(where, f"adb reverse tcp:{self.port} tcp:{self.port}", 8)[0] == 0
                self.usb.update(phone=where, reverse=ok, checked=time.time())
            except Exception:
                log.exception("phone usb link")
            time.sleep(5)

    def install_over_usb(self) -> str:
        """`adb install` the app on the phone (plugged into the laptop, or the PC), then hand it its token."""
        if not APK.exists():
            return "The app hasn't been built yet. Run android/build.sh on the Mac and pull the update."
        where = self._phone_on()
        if where is None:
            if not shutil.which("adb"):
                return "This laptop needs adb first: run sudo dnf install android-tools, then ask me again."
            return "I can't see the phone. Plug it in, turn on USB debugging, and tap Allow on the phone."
        apk = str(APK)
        if where == "pc":
            apk = "/tmp/batsy-widgets.apk"
            p = subprocess.run(["scp", "-q", "-o", f"ControlPath={Path.home()}/.ssh/batsy-%r@%h", str(APK),
                                f"{self._pc().host}:{apk}"], capture_output=True, text=True, timeout=120)
            if p.returncode != 0:
                return f"Copying the app to the PC failed: {(p.stderr or p.stdout).strip()[:120]}"
        code, out = self._sh(where, f"adb install -r {_q(apk)}", 180)
        if "UPDATE_INCOMPATIBLE" in out:  # the copy on the phone was built on another computer: replace it
            self._sh(where, f"adb uninstall {PACKAGE}", 30)
            code, out = self._sh(where, f"adb install {_q(apk)}", 180)
        if code != 0 or "Success" not in out:
            return f"Installing failed: {out.strip().splitlines()[-1][:140] if out.strip() else 'no reason given'}"
        # Android holds back broadcasts from a newly installed app until it has been opened once: open it, then pair
        self._sh(where, f"adb shell am start -n {PACKAGE}/.SettingsActivity", 15)
        time.sleep(2)
        self.pair_over_usb()
        self._sh(where, f"adb shell am start -n {PACKAGE}/.MainActivity", 15)
        return "Installed and paired. The Batsy app is open on your phone."

    def pair_over_usb(self) -> bool:
        """Give the app its token and addresses directly (adb), so there's no code to type."""
        where = self._phone_on()
        if where is None:
            return False
        remote = self.remote_url()
        extra = f"--es token {token()}" + (f" --es remote {_q(remote)}" if remote else "")
        code, out = self._sh(where, f"adb reverse tcp:{self.port} tcp:{self.port}; adb shell am broadcast "
                                    f"-n {PACKAGE}/.PairReceiver -a {PACKAGE}.PAIR {extra}", 30)
        return code == 0 and "result=0" in out.replace(" ", "")

    def remote_url(self) -> str:
        """How the phone reaches the laptop away from the PC: its Tailscale name (or address)."""
        try:
            out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5).stdout
            me = json.loads(out).get("Self", {})
            name = (me.get("DNSName") or "").rstrip(".")
            ip = (me.get("TailscaleIPs") or [""])[0]
            serve = subprocess.run(["tailscale", "serve", "status", "--json"], capture_output=True, text=True,
                                   timeout=5).stdout
            if name and '"443"' in serve:  # `tailscale serve` gives Batsy a secure https address: the phone's mic
                return f"https://{name}"   # only works on https (or USB), so that's the one to use away from home
            host = ip or name  # the address: a name needs Tailscale's DNS on the phone ("unknown host" without it)
            return f"http://{host}:{self.port}" if host else ""
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return ""

    def new_pair_code(self) -> str:
        code = f"{secrets.randbelow(10 ** 6):06d}"
        self.pair_code = (code, time.time() + 600)
        return code

    # --- the data ---------------------------------------------------------------------------------------
    def status(self) -> dict:
        self.last_seen = time.time()
        out = {"t": int(time.time()), "machines": [self._laptop()], "claude": self._claude(),
               "spotify": self._spotify(), "batsy": self._batsy(),
               "events": [e for e in getattr(self.app, "events", []) if time.time() - e["t"] < 600][-5:]}
        mp = getattr(self.app, "model_preview", None)
        if mp and time.time() - mp["t"] < 900:  # a picked model's size + outline, before slicing
            out["model_preview"] = {"t": mp["t"], "name": mp["item"]["name"], "site": mp["item"]["site"],
                                    "url": mp["item"]["url"], "info": mp["info"], "state": mp["state"],
                                    "image": f"/api/models/preview?k={self.thumb_key()}&t={int(mp['t'])}",
                                    "mesh": f"/api/models/mesh?k={self.thumb_key()}&t={int(mp['t'])}"}
        ms = getattr(self.app, "model_results", None)
        if ms and ms.get("t") and time.time() - ms["t"] < 600:  # a model search: the phone shows the cards
            out["models"] = {"q": ms["q"], "t": ms["t"], "items": ms["items"]}
        pr = getattr(self.app, "printer3d", None)
        if pr is not None:
            out["printer"] = pr.snapshot()
        pc = self._pc_card()
        if pc is not None:
            out["machines"].append(pc)
        box = self._ai_card()
        if box is not None:
            out["machines"].append(box)
        return out

    def _laptop(self) -> dict:
        vm = psutil.virtual_memory()
        du = psutil.disk_usage("/")
        n, t = psutil.net_io_counters(), time.time()
        n0, t0 = self._net
        dt_ = max(0.5, t - t0)
        self._net = (n, t)
        temps = psutil.sensors_temperatures() if hasattr(psutil, "sensors_temperatures") else {}
        hot = max((e.current for es in (temps or {}).values() for e in es if 0 < e.current < 120), default=None)
        b = psutil.sensors_battery()
        return {"name": self.cfg.get("phone.laptop_name"), "kind": "laptop", "online": True,
                "cpu": psutil.cpu_percent(None), "cores": psutil.cpu_count(), "ram": vm.percent,
                "ram_used_gb": round(vm.used / 2 ** 30, 1), "ram_total_gb": round(vm.total / 2 ** 30, 1),
                "temp": round(hot) if hot else None, "disk": round(du.percent), "disk_free_gb": round(du.free / 1e9),
                "net_down_kbps": round((n.bytes_recv - n0.bytes_recv) / dt_ / 1024),
                "net_up_kbps": round((n.bytes_sent - n0.bytes_sent) / dt_ / 1024),
                "battery": round(b.percent) if b else None, "charging": bool(b.power_plugged) if b else None,
                "uptime_s": int(time.time() - psutil.boot_time())}

    def _pc_card(self) -> dict | None:
        """The PC's widget card (stats refreshed in the background). Not `_pc`: that's the SSH link the USB code uses."""
        pc = getattr(self.app, "pc", None)
        if not (pc and pc.configured):
            return None
        name = self.cfg.get("phone.pc_name")
        if time.time() - self.pc_checked > 8:  # one SSH call (~0.6 s) at most every 8 s, on its own thread
            self.pc_checked = time.time()
            threading.Thread(target=self._refresh_pc, daemon=True).start()
        if self.pc_stats is None:
            return {"name": name, "kind": "pc", "online": None}
        return {"name": name, "kind": "pc", **self.pc_stats}

    def _refresh_pc(self) -> None:
        code, out = self.app.pc.run(_pc_probe(), timeout=6)
        try:
            self.pc_stats = {"online": True, **json.loads(out.strip().splitlines()[-1])} if code == 0 else \
                {"online": False}
        except (ValueError, IndexError):
            self.pc_stats = {"online": code == 0}

    def _ai_card(self) -> dict | None:
        """The HP that runs Batsy's local AI: its stats, whether it's serving, the model, and how fast answers are.
        Refreshed in the background at most every 8 s (one SSH call + two small HTTP calls)."""
        target = self.cfg.get("local.box_ssh")
        if not target:
            return None
        if time.time() - getattr(self, "ai_checked", 0) > 8:
            self.ai_checked = time.time()
            threading.Thread(target=self._refresh_ai, daemon=True).start()
        st = getattr(self, "ai_stats", None) or {"online": None}
        return {"name": self.cfg.get("local.box_name"), "kind": "ai", **st}

    def _refresh_ai(self) -> None:
        import subprocess
        import urllib.request

        from batsy import llm_hosts

        target = self.cfg.get("local.box_ssh")
        try:
            r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", target, _pc_probe()],
                               capture_output=True, text=True, timeout=8)
            st = {"online": True, **json.loads(r.stdout.strip().splitlines()[-1])} if r.returncode == 0 else \
                {"online": False}
        except (subprocess.SubprocessError, ValueError, IndexError, OSError):
            st = {"online": False}
        host = (self.cfg.get("local.hosts") or [""])[0].rstrip("/")
        ai = {"serving": llm_hosts.best(self.cfg) == host, "model": None, "loaded": False}
        if st.get("online") and host:
            try:
                with urllib.request.urlopen(host + "/api/ps", timeout=2) as resp:
                    models = json.loads(resp.read()).get("models", [])
                ai["loaded"] = bool(models)
                ai["model"] = models[0].get("name") if models else self.cfg.get("local.model")
            except Exception:
                ai["model"] = None  # the machine is up but its AI isn't
        ai.update(_ai_speed())
        self.ai_stats = {**st, "ai": ai}

    def _claude(self) -> dict:
        from batsy import claude_code

        b = claude_code.budget(self.cfg)
        return {k: {"used": b[k]["used"], "limit": b[k]["limit"], "pct": b[k]["pct"], "resets_at": b[k]["resets_at"]}
                for k in ("session", "week")} | {"requests_week": b.get("requests_week", 0),
                                                 "running": [a["label"] for a in _agents() if a["kind"] == "claude"],
                                                 "sessions": _claude_sessions()}

    def _spotify(self) -> dict | None:
        from batsy.tools import music

        sp = music.get_spotify(self.cfg)
        if not sp.ready():
            return None
        cached = getattr(self, "_sp", (0, None))
        if time.time() - cached[0] < 4:
            return cached[1]
        try:
            st = sp._req("GET", "/me/player", params={"additional_types": "episode"}) or {}
        except Exception as e:
            return {"error": str(e)[:120]}
        item = st.get("item") or {}
        images = (item.get("album") or {}).get("images") or item.get("images") or []
        out = None if not item else {
            "title": item.get("name", ""), "artist": ", ".join(a["name"] for a in item.get("artists", [])) or
            (item.get("show") or {}).get("name", ""), "album": (item.get("album") or {}).get("name", ""),
            "art": min(images, key=lambda i: abs((i.get("width") or 300) - 300))["url"] if images else "",
            "playing": bool(st.get("is_playing")), "progress_ms": st.get("progress_ms") or 0,
            "duration_ms": item.get("duration_ms") or 0, "device": (st.get("device") or {}).get("name", ""),
            "id": item.get("id", "")}
        self._sp = (time.time(), out)
        return out

    def _batsy(self) -> dict:
        from batsy import activity

        live = dict(getattr(self.app.overlay, "live", {}) or {})
        act = activity.snapshot()
        from batsy.tools import monitor

        rem = [it for _, it in monitor.reminders()][:1] if hasattr(monitor, "reminders") else []
        return {"state": live.get("state", "idle"), "status": live.get("status", ""),
                "since": live.get("since"), "you": live.get("you", ""), "said": live.get("said", ""),
                "said_t": live.get("t"), "agents": [a["label"] for a in act["agents"]][:4],
                "asks": [a["question"] for a in act["asks"]][:3],
                "next_reminder": {"what": rem[0].get("what") or "timer", "at": int(rem[0]["next_run"])} if rem else None,
                "today": self._today(),  # the day's to-dos, shown under the clock
                "listening_paused": time.time() < getattr(self.app, "muted_mic_until", 0.0),
                "voice_muted": time.time() < getattr(self.app.speaker, "muted_until", 0.0)}

    @staticmethod
    def _today() -> list[str]:
        import datetime as dt

        from batsy.memory_store import get_store

        try:
            rows = get_store().agenda_for(dt.date.today().isoformat())
            rows = sorted(rows, key=lambda r: bool(r["done"]))[:4]  # still to do first, ticked ones after
            return [{"text": r["text"], "done": bool(r["done"])} for r in rows]
        except Exception:
            return []

    # --- buttons -----------------------------------------------------------------------------------------
    def spotify_action(self, action: str, body: dict | None = None) -> dict:
        from batsy.tools import music

        sp = music.get_spotify(self.cfg)
        if not sp.ready():
            return {"ok": False, "say": "Spotify isn't connected on the laptop."}
        try:
            if action == "toggle":
                st = sp._req("GET", "/me/player") or {}
                said = sp.pause() if st.get("is_playing") else sp.resume()
            elif action in ("next", "previous", "pause", "resume"):
                said = getattr(sp, action)()
            elif action == "play_uri":  # the song the app kept on screen after it stopped: from where it stopped
                uri = str((body or {}).get("uri", ""))
                if not uri.startswith("spotify:track:"):
                    return {"ok": False, "say": "Unknown song."}
                target = sp.device()
                sp._req("PUT", "/me/player/play", {"device_id": target["id"]},
                        {"uris": [uri], "position_ms": max(0, int((body or {}).get("position_ms") or 0))})
                said = ""
            else:
                return {"ok": False, "say": f"Unknown action {action}"}
        except Exception as e:
            return {"ok": False, "say": str(e)}
        self._sp = (0, None)
        return {"ok": True, "say": said}

    def conversation(self, limit: int = 80, after: int = 0) -> dict:
        """The conversation, oldest first: what you said (by voice at the laptop, from the phone, typed, on the
        website) and what Batsy answered, plus what it announced by itself (reminders...). From Batsy's memory, so
        every device shows the same thing. `after`: only newer than that id. While a request is still being
        answered it's included as `live`, so the phone shows it straight away."""
        from batsy.memory_store import get_store

        st = get_store()
        with st.lock:
            rows = st.db.execute("SELECT id, t, source, user, reply FROM exchanges WHERE id > ? ORDER BY id DESC "
                                 "LIMIT ?", (after, max(1, min(limit, 300)))).fetchall()
        msgs = []
        for r in rows[::-1]:
            via = {"voice": "voice", "typed": "typed", "phone": "phone", "web": "website", "skill": "voice",
                   "routine": "routine", "alert": "batsy"}.get(r["source"], r["source"] or "")
            if r["user"]:
                msgs.append({"id": r["id"], "t": r["t"], "who": "you", "text": r["user"], "via": via})
            if r["reply"]:
                msgs.append({"id": r["id"], "t": r["t"], "who": "bot", "text": r["reply"],
                             "via": "announced" if r["source"] == "alert" else via})
        live = getattr(self.app.overlay, "live", {}) or {}
        cur = None
        if live.get("state") in ("listening", "thinking", "speaking") and live.get("you"):
            last_you = next((m["text"] for m in reversed(msgs) if m["who"] == "you"), "")
            if live["you"] != last_you:  # asked, not answered (and saved) yet
                cur = {"you": live["you"], "said": live.get("said", ""), "status": live.get("status", ""),
                       "state": live["state"]}
        return {"messages": msgs, "live": cur, "last_id": rows[0]["id"] if rows else after}

    def batsy_action(self, body: dict) -> dict:
        action = body.get("action")
        if action == "stop":
            self.app.end_conversation()
            return {"ok": True, "say": "Stopped."}
        if action == "ask":  # typed in the app: same as typing to Batsy with `batsy chat`
            text = (body.get("text") or "").strip()
            if not text:
                return {"ok": False, "say": "Nothing to send."}
            if self.app.answer_pending(text):  # Batsy asked yes / no: this is the answer
                return {"ok": True, "say": "Got it."}
            said: list[str] = []
            if self.app.turn_lock.locked():  # Batsy is still working on the last thing: say so, don't go quiet
                self.app.speaker.say("Got it, right after this one.")
            self.app.overlay.heard(text)
            if not self._handled_locally(text, said.append, speak=not body.get("quiet")):
                aloud = body.get("speak")  # said into the phone's mic: answered out loud from the laptop
                self.app.run_turn(text, "phone", lambda s: (said.append(s), self.app.speaker.say(s) if aloud else None,
                                                            self.app.overlay.say(s)),
                                  threading.Event(), self.app.ask_from_background)
                # (a check like "Just to check, you want me to …?" is asked on the phone, full screen, and out loud:
                # answer yes / no from the phone or the laptop. It used to be "no" at once, so nothing happened.)
            return {"ok": True, "say": " ".join(said) or "Done."}
        return {"ok": False, "say": f"Unknown action {action}"}

    # --- the phone's microphone ------------------------------------------------------------------------------
    def voice(self, raw: bytes, mode: str) -> dict:
        """Audio recorded on the phone. mode "ptt" (held the mic button): it's a request. mode "ears" (the phone
        listens for "Batsy" instead of the laptop): only what starts with "Batsy" (or follows a bare "Batsy") counts.
        The phone is only the microphone: Batsy answers out loud from the laptop, as usual (and as text on the phone)."""
        import tempfile

        import numpy as np

        from batsy.voice.ears import strip_wake_word

        from batsy import activity

        if mode == "ears":
            self.app.phone_ears_until = time.time() + 60  # the phone is listening: the laptop's mic stands down
        elif raw and hasattr(self.app.brain, "warm_soon"):
            self.app.brain.warm_soon()  # held the mic: the model loads while this is transcribed
        with tempfile.NamedTemporaryFile(suffix=".audio") as f:
            f.write(raw)
            f.flush()
            pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", f.name, "-ac", "1", "-ar", "16000", "-f", "s16le",
                                  "-"], capture_output=True, timeout=30).stdout
        audio = np.frombuffer(pcm, dtype=np.int16)
        if audio.size < 16000 * 0.3 or self.app.stt is None:
            return {"ok": True, "ignored": True}
        with activity.work("Your phone's mic", "voice", "Transcribing what you said on the phone"):
            text = self.app.stt.transcribe(audio, quiet=True).strip()
        log.info("phone mic (%s): %r", mode, text)
        if not text:
            return {"ok": True, "ignored": True}
        if mode == "ears":
            found, rest = strip_wake_word(text, self.cfg.get("voice.wake_words"))
            if found and not rest.strip(" .,!?"):
                self.armed = time.time() + 8  # just "Batsy": the next thing said is the request
                return {"ok": True, "heard": text, "armed": True}
            if found:
                text = rest.strip(" ,")
            elif time.time() < getattr(self, "armed", 0):
                self.armed = 0
            else:
                return {"ok": True, "ignored": True, "heard": text}
        if mode != "ears":  # hold-to-talk: people still say "Batsy, …" first
            found, rest = strip_wake_word(re.sub(r"^(?:uh+|um+|hey|ok|okay)[ ,]+", "", text, flags=re.I),
                                          self.cfg.get("voice.wake_words"))
            if found and rest.strip(" ,.?!"):
                text = rest.strip(" ,")
        self.app.speaker.unhush()
        r = self.batsy_action({"action": "ask", "text": text, "speak": True})
        return {"ok": True, "heard": text, "say": r.get("say", "")}

    def _handled_locally(self, text: str, capture, speak: bool = True) -> bool:
        """Run it as a local command, catching what Batsy says (also spoken and shown as usual, unless not `speak`:
        a request from the phone's mic is answered on the phone)."""
        old = self.app._reply

        def both(t):
            capture(t)
            if speak:
                old(t)
            else:
                self.app.last_said = t
                self.app.overlay.say(t)

        self.app._reply = both
        self.app._source_hint = "phone"  # saved in the conversation as asked from the phone
        try:
            return self.app.local_command(text)
        finally:
            self.app._source_hint = None
            self.app._reply = old

    def pc_action(self, action: str) -> dict:
        """The app asks "are you sure?" itself before restart / shutdown."""
        from batsy import pc as pcmod

        pc = getattr(self.app, "pc", None) or pcmod.Remote(self.cfg)
        if not pc.configured:
            return {"ok": False, "say": "The PC isn't set up."}
        if action == "wake":
            ok = pc.wake()
            return {"ok": ok, "say": "Waking the PC." if ok else "I couldn't send the wake signal."}
        if action == "lock":
            code, out = pc.run("loginctl lock-sessions")
            return {"ok": code == 0, "say": "Locked the PC." if code == 0 else pc._why(code, out)}
        power = {"sleep": "suspend", "restart": "reboot", "shutdown": "poweroff"}.get(action)
        if power:
            said = pc.power(power)
            self.pc_checked = 0
            return {"ok": not said, "say": said or {"suspend": "The PC is going to sleep.", "reboot": "Restarting the PC.",
                                                    "poweroff": "Shutting down the PC."}[power]}
        return {"ok": False, "say": f"Unknown action {action}"}

    def printer_action(self, body: dict) -> dict:
        """The 3D printer from the app: the app's own buttons are the confirmation."""
        pr = getattr(self.app, "printer3d", None)
        if pr is None:
            return {"ok": False, "say": "No 3D printer."}
        a = str(body.get("action", ""))
        try:
            if a == "status":
                return {"ok": True, "say": pr.status(), "printer": pr.snapshot()}
            if a == "preheat":
                from batsy.printer3d import TEMPS_FOR

                noz, bed = TEMPS_FOR.get(str(body.get("material", "pla")).lower(), (200, 60))
                return {"ok": True, "say": pr.preheat(noz, bed)}
            if a == "cool":
                return {"ok": True, "say": pr.cool()}
            if a == "home":
                return {"ok": True, "say": pr.home()}
            if a == "move":
                return {"ok": True, "say": pr.move(str(body.get("axis", "Z")).upper()[:1], float(body.get("mm", 0)))}
            if a == "query":  # read-only questions to the firmware (nothing that moves or heats)
                cmd = str(body.get("gcode", "")).strip().upper()
                if not re.fullmatch(r"M(20|20 L|27|27 C|33 \S+|23 \S+|105|114|115|119|503)", cmd):
                    return {"ok": False, "say": "Only read-only queries."}
                return {"ok": True, "lines": pr.send(cmd, 10)}
            if a == "print_last":  # the app asked "Is the bed clear?" itself
                last = self.last_slice()
                if not last or not Path(last["gcode"]).exists():
                    return {"ok": False, "say": "Nothing is sliced yet."}
                if pr.state["printing"] or pr.state["uploading"] is not None:
                    return {"ok": False, "say": "The printer is busy."}
                return {"ok": True, "say": pr.print_now(Path(last["gcode"]), last["title"])}
            if a == "history":
                return {"ok": True, "prints": pr.history()[::-1][:20]}
            if a == "print_again":  # from the print library (the app asks "Is the bed clear?" itself)
                hist = pr.history()
                pick = next((h for h in reversed(hist) if h["gcode"] == body.get("gcode")), None)
                if not pick or not Path(pick["gcode"]).exists():
                    return {"ok": False, "say": "I don't have that print any more."}
                return {"ok": True, "say": pr.print_now(Path(pick["gcode"]), pick["job"])}
            if a == "copy_last":  # copy the newest slice to the card without printing (also a speed test)
                last = self.last_slice()
                if not last or pr.state["printing"] or pr.state["uploading"] is not None:
                    return {"ok": False, "say": "Nothing to copy, or the printer is busy."}
                threading.Thread(target=pr.print_file, args=(Path(last["gcode"]), last["title"], False), daemon=True).start()
                return {"ok": True, "say": "Copying it to the card, without printing."}
            if a == "copy_result":
                return {"ok": True, "copy": getattr(pr, "last_copy", None)}
            if a == "files":
                files = pr.sd_files()
                return {"ok": True, "files": files, "names": pr.long_names(files) if body.get("long") else {}}
            if a == "print_sd":
                return {"ok": True, "say": pr.print_sd(str(body.get("name", "")), str(body.get("job", "")))}
            if a in ("pause", "resume", "cancel", "motors_off"):
                return {"ok": True, "say": getattr(pr, a)()}
        except OSError as e:
            return {"ok": False, "say": str(e)}
        return {"ok": False, "say": f"Unknown action {a}"}

    def where(self, client_ip: str, headers) -> dict:
        """Is the phone asking this near the laptop? Tailscale knows: on the same Wi-Fi it talks to the laptop
        directly over the home network ("via 192.168.1.x"); away, over the internet or a relay. The Android phone on
        the USB cable (127.0.0.1, no proxy) is near by definition."""
        import ipaddress

        fwd = (headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if not fwd and client_ip in ("127.0.0.1", "::1"):
            return {"near": True, "how": "USB cable"}
        ip = fwd or client_ip
        cache = getattr(self, "_where", {})
        hit = cache.get(ip)
        if hit and time.time() - hit[0] < 60:
            return hit[1]
        try:
            out = subprocess.run(["tailscale", "ping", "-c", "3", "--timeout", "3s", ip], capture_output=True, text=True,
                                 timeout=12).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        via = ""
        for line in out.splitlines():
            m = re.search(r"via (\S+)", line)
            if m:
                via = m.group(1)
        near = False
        if via and not via.startswith("DERP"):
            try:  # a direct path to an address on one of the laptop's own networks: the same Wi-Fi
                addr = ipaddress.ip_address(via.rsplit(":", 1)[0].strip("[]"))
                near = any(addr in net for net in _home_nets())
            except ValueError:
                near = False
        res = {"near": near, "how": f"same Wi-Fi ({via.rsplit(':', 1)[0]})" if near else ("away (relay)" if via.startswith("DERP")
                                                                                         else f"away ({via or 'no path'})")}
        cache[ip] = (time.time(), res)
        self._where = cache
        log.info("phone at %s: %s", ip, res)
        return res

    # --- the brain (Obsidian vault) ----------------------------------------------------------------------------
    def brain(self) -> dict:
        """Today at a glance: the summary (or the latest of the timeline), the plan, the inbox count."""
        from batsy import vault

        d = vault.today()
        p = vault.daily_path(d)
        summary = (vault.get_block(p, "summary") or "").strip()
        if summary.startswith("_"):
            summary = ""
        y = vault.daily_path(d - dt.timedelta(days=1))
        ysum = (vault.get_block(y, "summary") or "").strip()
        timeline = [x for x in (vault.get_block(p, "timeline") or "").splitlines() if x.strip()]
        plan = [x for x in (vault.get_block(p, "plan") or "").splitlines() if x.strip()]
        return {"date": d.isoformat(), "day": f"{d:%A %-d %B}", "summary": _md(summary) if summary else "",
                "yesterday": _md(ysum) if ysum and not ysum.startswith("_") else "",
                "latest": [_inline(re.sub(r"^- ", "", x)) for x in timeline[-4:]][::-1], "count": len(timeline),
                "plan": [{"done": "[x]" in x[:6].lower(), "text": _inline(re.sub(r"^- \[.\] ", "", x))} for x in plan],
                "inbox": len(list(vault.path("inbox").glob("*.md"))), "today": vault.daily_name(d)}

    def brain_note(self, name: str) -> dict:
        from batsy import vault, vault_index

        hit = vault_index.note_text(name)
        if not hit:
            return {"error": "no such note"}
        p, text = hit
        body = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.S)
        return {"title": p.stem, "path": p.relative_to(vault.ROOT).as_posix(), "html": _md(body, self.thumb_key()),
                "props": vault.props(p)}

    def brain_search(self, q: str) -> dict:
        from batsy import vault_index

        vault_index.reindex()
        return {"hits": [{"title": h["title"], "path": h["path"], "snippet": h["snippet"]}
                         for h in vault_index.search(q, 12)]}

    def thumb_key(self) -> str:
        """Thumbnails go in <img> tags (no token header): they carry a key derived from the token instead."""
        import hmac

        return hmac.new(token().encode(), b"cloud-thumb", "sha256").hexdigest()[:24]

    def cloud_ticket(self, path: str) -> dict:
        size, t = self.drive.stat(path)
        tk = secrets.token_urlsafe(18)
        self._tickets = {k: v for k, v in getattr(self, "_tickets", {}).items() if v[1] > time.time()}
        self._tickets[tk] = ("cloud:" + self.drive.safe(path), time.time() + 600)
        return {"ok": True, "url": f"/api/cloud/raw?t={tk}", "name": Path(path).name, "size": size, "t": t}

    def ticket_path(self, t: str) -> str | None:
        hit = getattr(self, "_tickets", {}).get(t)
        return hit[0] if hit and hit[1] > time.time() else None

    def last_slice(self) -> dict | None:
        """The newest slice (yours, or a calibration print): its preview, 3D paths, G-code and estimates."""
        from batsy import calib, slicer

        found = sorted([*Path(self.cfg.get("files.inbox")).expanduser().glob("**/sliced/*.paths.json"),
                        *(calib.OUT / "sliced").glob("*.paths.json")], key=lambda p: p.stat().st_mtime)
        if not found:
            return None
        paths = found[-1]
        gcode = paths.with_name(paths.name.removesuffix(".paths.json") + ".gcode")
        try:
            title = json.loads(paths.read_text()).get("title", "")
        except (OSError, ValueError):
            title = ""
        est = slicer._estimates(gcode) if gcode.exists() else {}
        return {"title": title or gcode.stem, "t": int(paths.stat().st_mtime), "gcode": str(gcode), "paths": str(paths),
                "preview": str(gcode.with_suffix(".png")), **est}

    def home_action(self, action: str) -> dict:
        from batsy import home

        ls = home.lights(self.cfg)
        if not ls:
            return {"ok": False, "say": "No lights are set up."}
        if action == "state":
            st = ls[0].state()
            return {"ok": st is not None, "on": st, "say": "" if st is not None else "The light isn't answering."}
        if action not in ("on", "off", "night"):
            return {"ok": False, "say": f"Unknown action {action}"}
        want = action != "off"
        if want:
            self.app._lights_off_pending = None
        got = ls[0].set(want)
        if got is None:
            return {"ok": False, "say": "The light isn't answering."}
        return {"ok": got == want, "on": got, "say": ("Night mode. Light on." if action == "night" else
                                                      f"Light {'on' if got else 'off'}.")}

    def files(self) -> dict:
        from batsy import received

        rows = received.all_rows(limit=60)
        return {"files": [{"name": r["name"], "from": r["sender"], "t": int(r["received"]), "kind": r["kind"],
                           "size": r["size"], "via": r["via"], "path": r["path"], "on_pc": bool(r.get("pc_path")),
                           "missing": bool(r.get("missing"))} for r in rows]}

    def file_action(self, body: dict) -> dict:
        from batsy import received

        path = str(body.get("path", ""))
        if body.get("action") not in ("open", "here") or path not in {r["path"] for r in received.all_rows(limit=2000)}:
            return {"ok": False, "say": "I don't know that file."}  # only files Batsy received, nothing else
        if not Path(path).exists():
            return {"ok": False, "say": "It's been moved or deleted."}
        if body["action"] == "here":  # to open on the phone: a link that works for 5 minutes, for this file only
            t = secrets.token_urlsafe(18)
            self._tickets = {k: v for k, v in getattr(self, "_tickets", {}).items() if v[1] > time.time()}
            self._tickets[t] = (path, time.time() + 300)
            return {"ok": True, "url": f"/api/files/raw?t={t}", "name": Path(path).name}
        self.app.open_result(path)
        return {"ok": True, "say": f"Opened {Path(path).name} on the laptop."}


def _inline(md: str) -> str:
    """One line of Markdown as HTML (wikilinks become links the app follows)."""
    import html as h

    s = h.escape(md)
    s = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r'<a data-note="\1">\2</a>', s)
    s = re.sub(r"\[\[([^\]]+)\]\]", r'<a data-note="\1">\1</a>', s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    return s


def _md(text: str, key: str = "") -> str:
    """A note as HTML for the phone: Batsy's markers hidden, [[links]] followable, images from the vault served."""
    from urllib.parse import quote

    import markdown

    text = re.sub(r"%% /?batsy(?::[\w-]+)? %%\n?", "", text)
    text = re.sub(r"\s*\^f-\w+", "", text)
    text = re.sub(r"!\[\[([^\]|]+)(?:\|\d+)?\]\]", lambda m: f"![](/api/brain/file?p={quote(m.group(1))}&k={key})", text)
    text = re.sub(r"!\[\[([^\]]+)\.base\]\]", "", text)
    text = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r'<a data-note="\1">\2</a>', text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r'<a data-note="\1">\1</a>', text)
    text = re.sub(r"^(\s*)- \[( |x)\] ", lambda m: m.group(1) + "- " + ("☑ " if m.group(2) == "x" else "☐ "), text, flags=re.M)
    return markdown.markdown(text, extensions=["tables", "sane_lists"])


def _home_nets() -> list:
    """The networks the laptop itself is on (Wi-Fi, Ethernet), IPv4 and IPv6 (home IPv6 is public, but the phone on
    the same Wi-Fi shares its /64). Not Tailscale's or Docker's."""
    import ipaddress

    nets = []
    for name, addrs in psutil.net_if_addrs().items():
        if name.startswith(("tailscale", "docker", "br-", "veth", "lo")):
            continue
        for a in addrs:
            try:
                if a.family == socket.AF_INET and a.netmask:
                    nets.append(ipaddress.ip_network(f"{a.address}/{a.netmask}", strict=False))
                elif a.family == socket.AF_INET6 and not a.address.startswith("fe80"):
                    nets.append(ipaddress.ip_network(f"{a.address.split('%')[0]}/64", strict=False))
            except ValueError:
                pass
    return nets


def _claude_sessions() -> list[dict]:
    """Claude Code running on this laptop: which project each session is in, and for how long."""
    out = []
    for p in psutil.process_iter(["name", "cmdline", "create_time", "cwd", "terminal"]):
        try:
            cmd = p.info["cmdline"] or []
            if not cmd or not (p.info["name"] == "claude" or os.path.basename(cmd[0]) == "claude"):
                continue
            cwd = p.info["cwd"] or ""
            if cwd.startswith("/tmp/cc-daemon") or "spare" in cwd.split("/")[-1:]:
                continue  # Claude Code's own warm spare workers, not a session
            parent = p.parent()
            if parent and parent.name() == "claude":
                continue  # a child of a session already listed
            out.append({"project": os.path.basename(cwd) or "~", "path": cwd, "since": int(p.info["create_time"]),
                        "interactive": bool(p.info["terminal"]), "pid": p.pid})
        except (psutil.Error, OSError):
            continue
    return sorted(out, key=lambda s: s["since"])


def _agents() -> list[dict]:
    from batsy import activity

    return activity.snapshot()["agents"]


def _q(s: str) -> str:
    return "'" + s.replace("'", "") + "'"


def _handler(hub: Hub):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Batsy"

        def log_message(self, *a):
            pass

        def _send(self, code: int, obj=None, body: bytes | None = None, ctype: str = "application/json") -> None:
            data = body if body is not None else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            try:
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):  # the phone stopped waiting (a slow answer): nothing to do
                self.close_connection = True

        def _authed(self) -> bool:
            got = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if got and secrets.compare_digest(got, token()):
                return True
            self._send(401, {"error": "not paired"})
            return False

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(min(n, 65536)) or b"{}")
            except ValueError:
                return {}

        def do_GET(self):
            from batsy import web

            if web.serve(self, hub.app, "GET"):  # the website (/web): you and your friends, by Tailscale identity
                return
            if self.path == "/app.apk":  # the app itself: no token yet when you install it
                if not APK.exists():
                    return self._send(404, {"error": "not built"})
                return self._send(200, body=APK.read_bytes(), ctype="application/vnd.android.package-archive")
            if self.path == "/ping":
                return self._send(200, {"batsy": True})
            if self.path == "/app" or self.path.startswith("/app/"):  # the pages: no data in them, no token needed
                rel = self.path.split("?", 1)[0].removeprefix("/app").strip("/") or "index.html"
                f = (UI / rel).resolve()
                if not str(f).startswith(str(UI.resolve()) + os.sep) or not f.is_file():
                    return self._send(404, {"error": "no such page"})
                return self._send(200, body=f.read_bytes(), ctype=TYPES.get(f.suffix, "application/octet-stream"))
            if self.path.startswith("/api/cloud/"):  # the Batman drive: files by ticket, thumbnails by key
                from urllib.parse import parse_qs, urlparse
                import mimetypes

                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                if self.path.startswith("/api/cloud/raw"):
                    want = hub.ticket_path(q.get("t", ""))
                    if not want or not want.startswith("cloud:"):
                        return self._send(404, {"error": "that link has expired"})
                    want = want[6:]
                    try:
                        size, _ = hub.drive.stat(want)
                    except (OSError, ValueError):
                        return self._send(404, {"error": "it's gone"})
                    self.send_response(200)
                    self.send_header("Content-Type", mimetypes.guess_type(want)[0] or "application/octet-stream")
                    self.send_header("Content-Length", str(size))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    try:
                        hub.drive.stream(want, self.wfile)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                if self.path.startswith("/api/cloud/thumb"):
                    if not secrets.compare_digest(q.get("k", ""), hub.thumb_key()):
                        return self._send(403, {"error": "no"})
                    try:
                        jpg = hub.drive.thumb(q.get("p", ""), int(q.get("v", 0)), int(q.get("s", 0)))
                    except ValueError:
                        jpg = None
                    if not jpg:
                        return self._send(404, {"error": "no preview"})
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(jpg)))
                    self.send_header("Cache-Control", "private, max-age=31536000, immutable")  # the URL has the version
                    self.end_headers()
                    self.wfile.write(jpg)
                    return
            if self.path.startswith("/api/models/mesh"):  # the picked model, as a light STL for the phone's 3D view
                from urllib.parse import parse_qs, urlparse

                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                mp = getattr(hub.app, "model_preview", None)
                mesh = Path((mp or {}).get("info", {}).get("mesh") or "")
                if not mp or not secrets.compare_digest(q.get("k", ""), hub.thumb_key()) or not mesh.is_file():
                    return self._send(404, {"error": "no model"})
                return self._send(200, body=mesh.read_bytes(), ctype="model/stl")
            if self.path.startswith("/api/models/preview"):  # the picked model's outline picture: <img>, so by key
                from urllib.parse import parse_qs, urlparse

                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                mp = getattr(hub.app, "model_preview", None)
                if not mp or not secrets.compare_digest(q.get("k", ""), hub.thumb_key()):
                    return self._send(404, {"error": "no preview"})
                return self._send(200, body=Path(mp["image"]).read_bytes(), ctype="image/png")
            if self.path.startswith("/api/brain/file"):  # a picture in a note (print previews): <img>, so by key
                from urllib.parse import parse_qs, urlparse
                import mimetypes

                from batsy import vault

                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                f = vault.ROOT / "_attachments" / Path(q.get("p", "")).name
                if not secrets.compare_digest(q.get("k", ""), hub.thumb_key()) or not f.is_file():
                    return self._send(404, {"error": "no such file"})
                return self._send(200, body=f.read_bytes(), ctype=mimetypes.guess_type(f.name)[0] or "image/png")
            if self.path.startswith("/api/files/raw"):  # a received file, opened on the phone: by a 5-minute ticket
                from urllib.parse import parse_qs, urlparse
                import mimetypes

                t = (parse_qs(urlparse(self.path).query).get("t") or [""])[0]
                want = hub.ticket_path(t)
                if not want or not Path(want).is_file():
                    return self._send(404, {"error": "that link has expired"})
                kind = mimetypes.guess_type(want)[0] or "application/octet-stream"
                return self._send(200, body=Path(want).read_bytes(), ctype=kind)
            if not self._authed():
                return
            if self.path.startswith("/api/status"):
                try:
                    return self._send(200, hub.status())
                except Exception as e:
                    log.exception("phone status")
                    return self._send(500, {"error": str(e)})
            if self.path.startswith("/api/brain"):
                from urllib.parse import parse_qs, urlparse

                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                try:
                    if self.path.startswith("/api/brain/note"):
                        return self._send(200, hub.brain_note(q.get("n", "Home")))
                    if self.path.startswith("/api/brain/search"):
                        return self._send(200, hub.brain_search(q.get("q", "")))
                    return self._send(200, hub.brain())
                except Exception as e:
                    log.exception("phone brain")
                    return self._send(500, {"error": str(e)})
            if self.path.startswith("/api/cloud"):
                from urllib.parse import parse_qs, urlparse

                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                mode = q.get("mode", "list")
                arg = {"list": q.get("path", "/batman"), "recent": "12", "search": q.get("q", "")}.get(mode)
                if arg is None:
                    return self._send(400, {"error": "no such mode"})
                try:
                    out = hub.drive.query(mode, arg)
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                out["k"] = hub.thumb_key()
                return self._send(200, out)
            if self.path.startswith("/api/conversation"):  # every exchange, however it was said: voice, typed, phone
                from urllib.parse import parse_qs, urlparse

                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                return self._send(200, hub.conversation(int(q.get("limit", 80)), int(q.get("after", 0))))
            if self.path.startswith("/api/where"):
                return self._send(200, hub.where(self.client_address[0], self.headers))
            if self.path.startswith("/api/files"):
                return self._send(200, hub.files())
            if self.path.startswith("/api/push/key"):
                from batsy import push

                return self._send(200, {"key": push.public_key()})
            if self.path.startswith("/api/plate"):  # models waiting to be sliced, and "how should it sit?"
                from batsy import plate
                from batsy.config import DATA_DIR

                if self.path.startswith("/api/plate/img"):
                    k = int((self.path.split("k=")[-1].split("&")[0] or "0") if "k=" in self.path else 0)
                    f = DATA_DIR / f"plate_option_{max(0, min(k, 5))}.png"
                    return self._send(200, body=f.read_bytes(), ctype="image/png") if f.exists() else self._send(404, {})
                return self._send(200, plate.state())
            if self.path.startswith("/api/slice"):
                last = hub.last_slice()
                if self.path.startswith("/api/slice/paths"):
                    if not last:
                        return self._send(404, {})
                    raw = Path(last["paths"]).read_bytes()
                    if "gzip" not in (self.headers.get("Accept-Encoding") or ""):
                        return self._send(200, body=raw)
                    import gzip  # a big slice is 5-15 MB of toolpaths: a third of that over Tailscale

                    z = gzip.compress(raw, 5)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Encoding", "gzip")
                    self.send_header("Content-Length", str(len(z)))
                    self.send_header("X-Raw-Length", str(len(raw)))  # for the progress bar (the browser unzips)
                    self.send_header("Access-Control-Expose-Headers", "X-Raw-Length")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(z)
                    return
                if self.path.startswith("/api/slice/preview"):
                    ok = last and Path(last["preview"]).exists()
                    return self._send(200, body=Path(last["preview"]).read_bytes(), ctype="image/png") if ok \
                        else self._send(404, {})
                return self._send(200, {k: v for k, v in (last or {}).items() if k not in ("gcode", "paths", "preview")})
            if self.path.startswith("/api/home"):
                return self._send(200, hub.home_action("state"))
            self._send(404, {"error": "no such thing"})

        def do_POST(self):
            from batsy import web

            if web.serve(self, hub.app, "POST"):
                return
            if self.path == "/api/pair":
                code = str(self._body().get("code", ""))
                ok = hub.pair_code and time.time() < hub.pair_code[1] and secrets.compare_digest(code, hub.pair_code[0])
                if not ok:
                    return self._send(403, {"error": "wrong or expired code"})
                hub.pair_code = None
                return self._send(200, {"token": token(), "remote": hub.remote_url()})
            if not self._authed():
                return
            if self.path.startswith("/api/plate"):  # tapped on the slice card: start choosing, or pick an option
                from batsy import plate, slicer

                body = self._body()
                app = hub.app
                if "pick" in body:
                    said = plate.answer(app, str(int(body["pick"]) + 1))
                elif body.get("start") == "again":  # the slice card's Position button: the last plate, chosen again
                    ms = plate.again(app)
                    if not ms:
                        said = "There's no model to position yet. Send me one."
                    else:
                        plate.WAITING.update(models=ms, t=time.time())
                        plate._save()
                        said = slicer.handle(app, f"slice {'both' if len(ms) > 1 else 'it'}", keep_last=True)  # same settings
                elif body.get("start"):
                    how = {"choose": "", "keep": " as it is", "best": " lying down"}.get(body["start"], "")
                    n = len(plate.waiting())
                    said = slicer.handle(app, f"slice {'both' if n > 1 else 'it'}{how}")
                elif body.get("cancel"):  # ✕: an open question is cancelled; a "waiting" card is dismissed
                    said = plate.answer(app, "cancel")
                    if said is None and plate.waiting():
                        plate.WAITING["models"] = []
                        plate._save()
                        said = "Okay, I'll leave them."
                else:
                    said = None
                if said:
                    app._reply(said)
                return self._send(200, {"ok": said is not None, "say": said or "", **plate.state()})
            if self.path.startswith("/api/voice"):  # the phone's microphone: raw audio in, Batsy's answer out
                from urllib.parse import parse_qs, urlparse

                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                n = int(self.headers.get("Content-Length") or 0)
                if self.path.startswith("/api/voice/off"):
                    self.rfile.read(n)
                    hub.app.phone_ears_until = 0.0  # the laptop's own mic is back
                    return self._send(200, {"ok": True})
                raw = self.rfile.read(min(n, 8_000_000))
                try:
                    return self._send(200, hub.voice(raw, q.get("mode", "ptt")))
                except Exception as e:
                    log.exception("phone voice")
                    return self._send(500, {"ok": False, "say": str(e)})
            if self.path.startswith("/api/cloud/upload"):  # the raw file is the body; folder and name in the URL
                from urllib.parse import parse_qs, urlparse

                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                try:
                    where = hub.drive.upload(q.get("dir", "/batman"), q.get("name", ""), self.rfile,
                                             int(self.headers.get("Content-Length") or 0))
                    return self._send(200, {"ok": True, "path": where, "say": f"Saved {Path(where).name}."})
                except (OSError, ValueError) as e:
                    return self._send(500, {"ok": False, "say": f"Upload failed: {e}"})
            body = self._body()
            try:
                if self.path == "/api/spotify":
                    return self._send(200, hub.spotify_action(str(body.get("action", "")), body))
                if self.path == "/api/batsy":
                    return self._send(200, hub.batsy_action(body))
                if self.path == "/api/pc":
                    return self._send(200, hub.pc_action(str(body.get("action", ""))))
                if self.path == "/api/home":
                    return self._send(200, hub.home_action(str(body.get("action", ""))))
                if self.path == "/api/push/subscribe":
                    from batsy import push

                    n = push.subscribe(body.get("subscription") or {}, str(body.get("device", "")))
                    if not body.get("quiet"):  # the app re-checks itself each time it opens: no message for that
                        push.notify("Batsy", "Notifications are on. You'll hear about your prints and reminders here.",
                                    tag="welcome")
                    return self._send(200, {"ok": True, "say": f"Notifications on ({n} device{'s' if n != 1 else ''})."})
                if self.path == "/api/push/test":
                    from batsy import push

                    n = push.notify("Batsy", body.get("text") or "This is a test notification.", tag="test")
                    return self._send(200, {"ok": bool(n), "say": f"Sent to {n} device{'s' if n != 1 else ''}." if n
                                            else "No phone has turned notifications on yet."})
                if self.path == "/api/printer":
                    return self._send(200, hub.printer_action(body))
                if self.path == "/api/files":
                    return self._send(200, hub.file_action(body))
                if self.path == "/api/models":  # a model picked on the phone (from the last search)
                    from batsy import printsearch

                    if body.get("action") in ("slice", "skip"):  # the buttons under a model's preview
                        from batsy import printsearch

                        yes = body["action"] == "slice"
                        if hub.app.confirm_request is not None:  # Batsy is asking "want me to slice it?": answer it
                            hub.app.answer_pending("yes" if yes else "no")
                            return self._send(200, {"ok": True, "say": "Slicing it." if yes else "Okay."})
                        return self._send(200, {"ok": True, "say": printsearch.slice_it(hub.app) if yes else "Okay."})
                    ms = getattr(hub.app, "model_results", None) or {}
                    items = ms.get("items") or []
                    i = int(body.get("i", -1))
                    if not 0 <= i < len(items):
                        return self._send(400, {"ok": False, "say": "That search has expired."})
                    ms["t"] = 0
                    said = printsearch.pick(hub.app, items[i])
                    hub.app._reply(said)
                    return self._send(200, {"ok": True, "say": said})
                if self.path == "/api/brain":  # a note from the phone: the brain's inbox
                    text = str(body.get("text", "")).strip()
                    if not text:
                        return self._send(400, {"ok": False, "say": "Nothing to note."})
                    from batsy import vault_capture

                    vault_capture.note(text, "phone")
                    return self._send(200, {"ok": True, "say": "Noted in your brain."})
                if self.path == "/api/cloud":
                    act = body.get("action")
                    if act == "open":
                        try:
                            return self._send(200, hub.cloud_ticket(str(body.get("path", ""))))
                        except FileNotFoundError:  # moved or deleted since the list was shown: say so, not a crash
                            msg = "That file isn't on the drive any more. It was moved or deleted."
                            return self._send(404, {"error": msg, "say": msg})
                        except (ConnectionError, PermissionError) as e:  # the PC is off, or can't be signed in to
                            return self._send(503, {"error": str(e), "say": str(e)})
                    if act == "mkdir":
                        p = hub.drive.mkdir(str(body.get("dir", "/batman")), str(body.get("name", "")))
                        return self._send(200, {"ok": True, "path": p, "say": f"Made {Path(p).name}."})
                    if act == "laptop":  # open it on the laptop: fetch a copy, then open it
                        src = hub.drive.safe(str(body.get("path", "")))
                        dst = Path.home() / "Batsy" / "batman" / src.removeprefix("/batman/")
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        with open(dst, "wb") as fh:
                            hub.drive.stream(src, fh)
                        hub.app.open_result(str(dst))
                        return self._send(200, {"ok": True, "say": f"Opened {dst.name} on the laptop."})
            except Exception as e:
                log.exception("phone %s", self.path)
                return self._send(500, {"ok": False, "say": str(e)})
            self._send(404, {"error": "no such thing"})

    return Handler


PHONE_INSTALL = re.compile(r"^(?:please )?(?:install|put|set up|update|reinstall) (?:the )?(?:batsy )?(?:widgets?|widget app|"
                           r"phone app|app) (?:on|to|onto) (?:my )?(?:phone|mobile|android|samsung|galaxy)$")
PHONE_PAIR = re.compile(r"^(?:pair|connect|link) (?:my |the )?(?:phone|mobile|widgets?|app|iphone|i phone|ipad)"
                        r"(?: again| to batsy)?$")


def handle(app, n: str) -> str | None:
    """Voice: "install the widget app on my phone", "pair my phone"."""
    hub = getattr(app, "hub", None)
    if hub is None:
        return None
    if PHONE_INSTALL.match(n):
        app.overlay.send("status", text="Installing the app on your phone")
        return hub.install_over_usb()
    if PHONE_PAIR.match(n):
        if re.search(r"\bi ?phone\b|\bipad\b", n):  # the iPhone app pairs with a code (USB pairing is Android's)
            code = hub.new_pair_code()
            app.show_visual("card", f"Pair your iPhone | In the Batsy app, type {code}. Valid for 10 minutes.")
            return f"In the Batsy app on your iPhone, type {' '.join(code)}."
        if hub.pair_over_usb():
            return "Paired over USB. The widgets will fill in within a few seconds."
        code = hub.new_pair_code()
        app.show_visual("card", f"Pair your phone | Open the Batsy app and enter {code}. Laptop: {hub.remote_url()}")
        return f"Open the Batsy app on your phone and enter {' '.join(code)}."
    return None
