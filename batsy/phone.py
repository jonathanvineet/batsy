"""The phone app's link to Batsy: one small HTTP API for the Android widgets (android/ in this repo).

GET  /api/status   everything the widgets show: the laptop and PC (CPU, memory, temperature, battery, disk,
                   network), your Claude budget, what Spotify is playing, and what Batsy is doing right now
POST /api/spotify  {"action": "toggle" | "next" | "previous"}
POST /api/batsy    {"action": "stop"} (like "~") or {"action": "ask", "text": "..."} (type to Batsy; returns the reply)
POST /api/pc       {"action": "wake"}
GET  /app.apk      the app itself, to install from the phone's browser
POST /api/pair     {"code": "123456"} -> {"token": ...}: the code `batsy phone` shows

Two ways in, the app tries both:
- USB: the phone is plugged into the PC. Batsy keeps an SSH tunnel PC:8765 -> laptop:8765 open and runs
  `adb reverse tcp:8765 tcp:8765` on the PC, so the app just talks to 127.0.0.1:8765. Plug and go; it even
  installs and pairs the app over USB ("install the widget app on my phone").
- Tailscale: anywhere else, http://<laptop's Tailscale name>:8765.
Every request needs the token from pairing; nothing is served to anyone else.
"""
import json
import logging
import os
import re
import secrets
import shutil
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
        if self.cfg.get("phone.usb") and getattr(self.app, "pc", None) and self.app.pc.configured:
            threading.Thread(target=self._usb_loop, daemon=True, name="phone-usb").start()

    # --- USB through the PC -----------------------------------------------------------------------------
    def _usb_loop(self) -> None:
        """Keep the path phone -> USB -> PC -> laptop open: an SSH tunnel to the PC, and `adb reverse` on the PC
        whenever the phone is plugged in (it's lost on every reconnect, so it's re-applied every few seconds)."""
        while True:
            try:
                if self._tunnel is None or self._tunnel.poll() is not None:
                    if self.app.pc.up():
                        self._tunnel = subprocess.Popen(
                            self.app.pc.ssh_args()[:-1] + ["-N", "-o", "ExitOnForwardFailure=yes", "-R",
                                                           f"127.0.0.1:{self.port}:127.0.0.1:{self.port}",
                                                           self.app.pc.host],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if self._tunnel is not None and self._tunnel.poll() is None:
                    code, out = self.app.pc.run(f"adb devices | grep -w device >/dev/null && "
                                                f"adb reverse tcp:{self.port} tcp:{self.port} >/dev/null && echo ok",
                                                timeout=8)
                    self.usb.update(phone=code == 0, reverse="ok" in out, checked=time.time())
            except Exception:
                log.exception("phone usb link")
            time.sleep(10)

    def install_over_usb(self) -> str:
        """Copy the app to the PC and `adb install` it on the phone, then hand it its token. What to say back."""
        pc = getattr(self.app, "pc", None)
        if not (pc and pc.configured):
            return "The PC isn't set up, so I can't reach the phone over USB."
        if not APK.exists():
            return "The app hasn't been built yet. Run android/build.sh on the Mac and pull the update."
        code, out = pc.run("command -v adb && adb devices | grep -w device", timeout=8)
        if "adb" not in out:
            return "The PC needs adb: run sudo apt install adb on it."
        if code != 0:
            return ("I can't see the phone on the PC. Plug it in, turn on USB debugging, and tap Allow on the phone.")
        dest = "/tmp/batsy-widgets.apk"
        p = subprocess.run(["scp", "-q", "-o", f"ControlPath={Path.home()}/.ssh/batsy-%r@%h", str(APK),
                            f"{pc.host}:{dest}"], capture_output=True, text=True, timeout=120)
        if p.returncode != 0:
            return f"Copying the app to the PC failed: {(p.stderr or p.stdout).strip()[:120]}"
        code, out = pc.run(f"adb install -r {dest}", timeout=180)
        if code != 0 or "Success" not in out:
            return f"Installing failed: {out.strip().splitlines()[-1][:140] if out.strip() else 'no reason given'}"
        self.pair_over_usb()
        return "Installed and paired. Long-press your home screen, tap Widgets, and add the Batsy ones."

    def pair_over_usb(self) -> bool:
        """Give the app its token and addresses directly (adb), so there's no code to type."""
        pc = getattr(self.app, "pc", None)
        if not (pc and pc.configured):
            return False
        remote = self.remote_url()
        extra = f"--es token {token()}" + (f" --es remote {_q(remote)}" if remote else "")
        code, out = pc.run(f"adb reverse tcp:{self.port} tcp:{self.port}; am=$(adb shell am broadcast "
                           f"-n {PACKAGE}/.PairReceiver -a {PACKAGE}.PAIR {extra}); echo \"$am\"", timeout=15)
        return code == 0 and "result=0" in out.replace(" ", "")

    def remote_url(self) -> str:
        """How the phone reaches the laptop away from the PC: its Tailscale name (or address)."""
        try:
            out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5).stdout
            me = json.loads(out).get("Self", {})
            name = (me.get("DNSName") or "").rstrip(".")
            ip = (me.get("TailscaleIPs") or [""])[0]
            host = name or ip
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
               "spotify": self._spotify(), "batsy": self._batsy()}
        pc = self._pc()
        if pc is not None:
            out["machines"].append(pc)
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

    def _pc(self) -> dict | None:
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

    def _claude(self) -> dict:
        from batsy import claude_code

        b = claude_code.budget(self.cfg)
        return {k: {"used": b[k]["used"], "limit": b[k]["limit"], "pct": b[k]["pct"], "resets_at": b[k]["resets_at"]}
                for k in ("session", "week")} | {"requests_week": b.get("requests_week", 0),
                                                 "running": [a["label"] for a in _agents() if a["kind"] == "claude"]}

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
                "listening_paused": bool(getattr(self.app, "muted_mic", False))}

    # --- buttons -----------------------------------------------------------------------------------------
    def spotify_action(self, action: str) -> dict:
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
            else:
                return {"ok": False, "say": f"Unknown action {action}"}
        except Exception as e:
            return {"ok": False, "say": str(e)}
        self._sp = (0, None)
        return {"ok": True, "say": said}

    def batsy_action(self, body: dict) -> dict:
        action = body.get("action")
        if action == "stop":
            self.app.end_conversation()
            return {"ok": True, "say": "Stopped."}
        if action == "ask":  # typed in the app: same as typing to Batsy with `batsy chat`
            text = (body.get("text") or "").strip()
            if not text:
                return {"ok": False, "say": "Nothing to send."}
            said: list[str] = []
            self.app.overlay.heard(text)
            if not self._handled_locally(text, said.append):
                self.app.run_turn(text, "phone", lambda s: (said.append(s), self.app.overlay.say(s)),
                                  threading.Event(), lambda q: False)
            return {"ok": True, "say": " ".join(said) or "Done."}
        return {"ok": False, "say": f"Unknown action {action}"}

    def _handled_locally(self, text: str, capture) -> bool:
        """Run it as a local command, catching what Batsy says (it's also spoken and shown as usual)."""
        old = self.app._reply

        def both(t):
            capture(t)
            old(t)

        self.app._reply = both
        try:
            return self.app.local_command(text)
        finally:
            self.app._reply = old

    def pc_action(self, action: str) -> dict:
        pc = getattr(self.app, "pc", None)
        if action == "wake" and pc and pc.configured:
            ok = pc.wake()
            return {"ok": ok, "say": "Waking the PC." if ok else "I couldn't send the wake signal."}
        return {"ok": False, "say": "The PC isn't set up."}


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
            self.end_headers()
            self.wfile.write(data)

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
            if self.path == "/app.apk":  # the app itself: no token yet when you install it
                if not APK.exists():
                    return self._send(404, {"error": "not built"})
                return self._send(200, body=APK.read_bytes(), ctype="application/vnd.android.package-archive")
            if self.path == "/ping":
                return self._send(200, {"batsy": True})
            if not self._authed():
                return
            if self.path.startswith("/api/status"):
                try:
                    return self._send(200, hub.status())
                except Exception as e:
                    log.exception("phone status")
                    return self._send(500, {"error": str(e)})
            self._send(404, {"error": "no such thing"})

        def do_POST(self):
            if self.path == "/api/pair":
                code = str(self._body().get("code", ""))
                ok = hub.pair_code and time.time() < hub.pair_code[1] and secrets.compare_digest(code, hub.pair_code[0])
                if not ok:
                    return self._send(403, {"error": "wrong or expired code"})
                hub.pair_code = None
                return self._send(200, {"token": token(), "remote": hub.remote_url()})
            if not self._authed():
                return
            body = self._body()
            try:
                if self.path == "/api/spotify":
                    return self._send(200, hub.spotify_action(str(body.get("action", ""))))
                if self.path == "/api/batsy":
                    return self._send(200, hub.batsy_action(body))
                if self.path == "/api/pc":
                    return self._send(200, hub.pc_action(str(body.get("action", ""))))
            except Exception as e:
                log.exception("phone %s", self.path)
                return self._send(500, {"ok": False, "say": str(e)})
            self._send(404, {"error": "no such thing"})

    return Handler


PHONE_INSTALL = re.compile(r"^(?:please )?(?:install|put|set up|update|reinstall) (?:the )?(?:batsy )?(?:widgets?|widget app|"
                           r"phone app|app) (?:on|to|onto) (?:my )?(?:phone|mobile|android|samsung|galaxy)$")
PHONE_PAIR = re.compile(r"^(?:pair|connect|link) (?:my |the )?(?:phone|mobile|widgets?|app)(?: again| to batsy)?$")


def handle(app, n: str) -> str | None:
    """Voice: "install the widget app on my phone", "pair my phone"."""
    hub = getattr(app, "hub", None)
    if hub is None:
        return None
    if PHONE_INSTALL.match(n):
        app.overlay.send("status", text="Installing the app on your phone")
        return hub.install_over_usb()
    if PHONE_PAIR.match(n):
        if hub.pair_over_usb():
            return "Paired over USB. The widgets will fill in within a few seconds."
        code = hub.new_pair_code()
        app.show_visual("card", f"Pair your phone | Open the Batsy app and enter {code}. Laptop: {hub.remote_url()}")
        return f"Open the Batsy app on your phone and enter {' '.join(code)}."
    return None
