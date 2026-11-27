"""The cockpit: everything at a glance on the laptop, full screen, when you leave the mouse and keyboard alone for a
few seconds (overlay.cockpit_seconds, default 5). Any key or mouse movement closes it at once, and it never takes the
keyboard, so nothing you type is lost.

What it shows (batsy/ui/cockpit.html):
  Batsy live      a waveform that moves with your voice (the mic) and Batsy's (each spoken sentence's loudness),
                  listening / thinking / speaking, what you said and what Batsy is saying
  Happening now   running jobs with their current step and progress, questions waiting for you, what just finished
  Connected       laptop, HP (AI), PC, printer, phones, Mac, iPad, lights: up or down, with what matters about each
  Widgets         printer, music, research, today's to-dos, recent files, Claude usage

Opening and closing follow GNOME's idle time (asked once a second, four times a second while it's open).
While it's open, Batsy's live conversation is drawn here instead of popping the overlay up over it; pictures, cards
and the 3D view close it and use the overlay as usual.
"""
import json
import logging
import re
import subprocess
import sys
import threading
import time

log = logging.getLogger("batsy.cockpit")

LIVE = {"listen", "think", "say", "heard", "partial", "level", "status", "done", "quiet", "speakwave"}
MEDIA = {"image", "card", "model3d", "camera", "showcase", "saver", "frame", "measure"}


class Cockpit:
    def __init__(self, app):
        self.app = app
        self.after_ms = int(float(app.cfg.get("overlay.cockpit_seconds") or 5) * 1000)
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.visible = False
        self.wake = threading.Event()
        self._inhibit = (0.0, False)
        self._devices = (0.0, [])
        threading.Thread(target=self._watch, daemon=True, name="cockpit-idle").start()
        threading.Thread(target=self._feed, daemon=True, name="cockpit-feed").start()

    # --- the window ----------------------------------------------------------------------------------------------------
    def send(self, cmd: str, **kw) -> None:
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                import os

                env = dict(os.environ)
                plat = self.app.cfg.get("overlay").get("qt_platform") if hasattr(self.app, "cfg") else ""
                if plat:  # the same as the overlay (XWayland): otherwise it opened underneath it
                    env["QT_QPA_PLATFORM"] = plat
                self.proc = subprocess.Popen([sys.executable, "-m", "batsy.cockpit_window"], stdin=subprocess.PIPE,
                                             stdout=subprocess.DEVNULL, text=True, env=env)
            try:
                self.proc.stdin.write(json.dumps({"cmd": cmd, **kw}) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self.proc = None

    def open(self) -> None:
        if self.visible or not self._allowed():
            return
        try:  # you've stepped away: whatever the overlay was showing makes way for the cockpit
            self.app.overlay.hide()
        except Exception:
            pass
        self.send("data", **payload(self.app))  # the picture first, then the window: no blank frame
        live = dict(getattr(self.app.overlay, "live", {}) or {})
        self.send("state", state=live.get("state", "idle"), you=live.get("you", ""), said=live.get("said", ""))
        self.send("show")
        self.visible = True
        self.wake.set()
        log.info("cockpit open")

    def close(self) -> None:
        if not self.visible:
            return
        self.visible = False
        self.send("hide")
        log.info("cockpit closed")

    def _allowed(self) -> bool:
        """Not over a video, a presentation or a call (something holding the screen awake), not while the overlay is
        showing you a picture / card / 3D model, and not when the screen is locked."""
        ov = self.app.overlay
        if getattr(ov, "has_media", False) and time.time() - getattr(ov, "last_active", 0) < 120:
            return False
        if getattr(self.app, "saver_on", False):
            return False
        t, inhibited = self._inhibit
        if time.time() - t > 10:
            inhibited = _gdbus_bool(["--dest", "org.gnome.SessionManager", "--object-path", "/org/gnome/SessionManager",
                                     "--method", "org.gnome.SessionManager.IsInhibited", "8"])  # 8: idle
            if _gdbus_bool(["--dest", "org.gnome.ScreenSaver", "--object-path", "/org/gnome/ScreenSaver",
                            "--method", "org.gnome.ScreenSaver.GetActive"]):
                inhibited = True  # locked
            self._inhibit = (time.time(), inhibited)
        return not inhibited

    # --- when: GNOME tells us (idle for N s / you're back) ---------------------------------------------------------
    def _watch(self) -> None:
        # GNOME's idle monitor ties a watch to the connection that made it, so a one-off `gdbus call` can't hold one
        # (it vanishes as the call exits). Asking for the idle time is a tiny call: once a second while closed, four
        # times a second while open, so it's gone within a quarter second of you touching anything.
        while True:
            try:
                self._watch_polling()
            except Exception:
                log.exception("cockpit idle check")
                time.sleep(5)

    def _watch_polling(self) -> None:
        from batsy import idle

        while True:
            ms = idle.idle_ms()
            if ms is not None:
                if ms >= self.after_ms:
                    self.open()
                elif ms < 1000:
                    self.close()
            time.sleep(0.25 if self.visible else 1.0)

    # --- what: the whole picture, refreshed while it's open ----------------------------------------------------------
    def _feed(self) -> None:
        while True:
            self.wake.wait(2)
            self.wake.clear()
            if self.visible:
                try:
                    self.send("data", **payload(self.app))
                except Exception:
                    log.exception("cockpit data")

    def devices(self) -> list[dict]:
        """Everything Batsy can reach, up or down (Tailscale peers, the lights), refreshed every 20 s."""
        t, cached = self._devices
        if time.time() - t < 20:
            return cached
        out = []
        try:
            d = json.loads(subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True,
                                          timeout=5).stdout)
            mine = d.get("Self", {}).get("UserID")
            import datetime as dt

            for p in (d.get("Peer") or {}).values():
                if p.get("UserID") != mine:
                    continue  # friends' devices shared with you: not yours to watch
                seen = p.get("LastSeen") or ""
                if not p.get("Online") and seen[:4].isdigit() and \
                        (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(seen.replace("Z", "+00:00"))).days > 14:
                    continue  # gone for weeks (an old phone): not worth a dot
                name = (p.get("DNSName") or "").split(".")[0] or p.get("HostName") or ""
                kind = (p.get("OS") or "").lower()
                out.append({"name": _nice(name, kind), "kind": kind, "online": bool(p.get("Online")),
                            "seen": p.get("LastSeen", "")[:16]})
        except Exception as e:
            log.debug("tailscale: %s", e)
        hub = getattr(self.app, "hub", None)
        usb = getattr(hub, "usb", None) or {}
        if usb.get("phone") and usb.get("reverse") and time.time() - usb.get("checked", 0) < 30:
            # the Android phone on the USB cable talks to Batsy without Tailscale: it's here, whatever Tailscale says
            phones = sorted((x for x in out if x["kind"] == "android"), key=lambda x: x.get("seen", ""), reverse=True)
            if phones:
                phones[0].update(online=True, via="USB")
            else:
                out.append({"name": "Android phone", "kind": "android", "online": True, "via": "USB"})
        light = (self.app.cfg.get("home.lights") or {}).get("light") if hasattr(self.app.cfg, "get") else None
        if light:
            import urllib.request

            try:
                urllib.request.urlopen(light, timeout=1.5)
                up = True
            except Exception as e:
                up = "HTTP Error" in str(e)
            out.append({"name": "Lights", "kind": "light", "online": up})
        out.sort(key=lambda x: (not x["online"], x["name"]))
        self._devices = (time.time(), out)
        return out


def payload(app) -> dict:
    """Everything the cockpit shows: the phone app's picture (machines, printer, music, Claude, Batsy, to-dos),
    plus jobs with their steps, research, devices and recent files."""
    from batsy import activity

    hub = getattr(app, "hub", None)
    base = {}
    if hub is not None:
        try:
            base = hub.status()
        except Exception as e:
            log.debug("hub status: %s", e)
    snap = activity.snapshot()
    out = {"t": time.time(), "machines": base.get("machines", []), "printer": base.get("printer"),
           "music": base.get("spotify"), "claude": base.get("claude"), "batsy": base.get("batsy", {}),
           "jobs": [a for a in snap["agents"] if a.get("kind") != "monitor"], "asks": snap.get("asks", []),
           "done": snap.get("done", [])[:4], "devices": app.cockpit.devices() if getattr(app, "cockpit", None) else []}
    try:
        from batsy import research

        r = research.active()
        out["research"] = {"active": r and {"name": r["name"], "notes": len(research.items(r["id"], "note")),
                                            "sources": len(research.items(r["id"], "source"))},
                           "all": [{"name": x["name"], "notes": x["notes"], "sources": x["sources"]}
                                   for x in research.all_research()[:4]]}
    except Exception as e:
        log.debug("research: %s", e)
    try:  # things you're keeping track of (tracker.py): the next date, the deadline, the repo
        from batsy import tracker

        out["tracking"] = [tracker.status(t) for t in tracker.active()]
    except Exception as e:
        log.debug("tracking: %s", e)
    try:  # the company (batsy/shop/): the queue, what's printing, the month's money
        from batsy.shop import reports as shop_reports

        out["shop"] = shop_reports.cockpit()
    except Exception as e:
        log.debug("shop: %s", e)
    try:
        from batsy import received

        out["files"] = [{"name": r["name"], "who": r["sender"] or "", "kind": r["kind"] or "", "t": r["received"]}
                        for r in received.all_rows(limit=4)]
    except Exception as e:
        log.debug("files: %s", e)
    return out


def _nice(name: str, kind: str) -> str:
    n = name.lower()
    return {"workstation": "This laptop", "c": "Mac", "jebas-a50": "Android phone", "iphone-11": "iPhone",
            "jonathans-ipad": "iPad"}.get(n) or name


def _gdbus_bool(args: list[str]) -> bool:
    try:
        out = subprocess.run(["gdbus", "call", "--session", *args], capture_output=True, text=True, timeout=3).stdout
        return "true" in out
    except (OSError, subprocess.SubprocessError):
        return False
