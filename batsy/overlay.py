"""Daemon-side handle on the overlay process. Every call is fire-and-forget, and everything degrades to a no-op
if the overlay is disabled or PySide6 isn't installed.

Renderers: "web" (batsy/overlay_web.py: GPU-drawn, hosts the 3D viewer) or "classic" (batsy/overlay_ui.py:
plain Qt painting). If the web overlay keeps dying it is restarted with software rendering, then replaced by the
classic one, so a bad graphics driver costs you visuals, never the assistant."""
import json
import logging
import os
import subprocess
import sys
import threading
import time

from batsy.config import DATA_DIR

log = logging.getLogger("batsy.overlay")


def _has_webengine() -> bool:
    try:
        import PySide6.QtWebEngineWidgets  # noqa: F401
        return True
    except ImportError:
        return False


class Overlay:
    def __init__(self, cfg, on_dismiss=None, enabled: bool = True):
        self.cfg = cfg
        self.enabled = enabled and bool(cfg.get("overlay.enabled"))
        self.on_dismiss = on_dismiss
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.has_media = False
        self.last_active = 0.0  # last time Batsy used the overlay for a conversation
        self.on_saver_exit = None
        self.on_viewer_event = None  # 3D viewer hosted in the overlay: click-to-measure etc.
        # The standby bat in the top-right corner (batsy/dock.py) while the overlay is closed.
        self.on_dock_open = None  # the bat or its bubble was clicked
        self.dock: subprocess.Popen | None = None
        self.dock_lock = threading.Lock()
        self.dock_deaths: list[float] = []
        self.origin: dict | None = None
        # what the bubble will show: the latest exchange only (your words + Batsy's reply to them)
        self.convo = {"you": "", "text": "", "image": None, "title": "", "fresh": False, "open": False}
        want = cfg.get("overlay.renderer") or "auto"
        self.renderer = "web" if want in ("auto", "web") and _has_webengine() else "classic"
        self.safe_gpu = False
        self.deaths: list[float] = []
        self.next_start = 0.0
        if self.enabled:
            self._start()
            if cfg.get("overlay.dock") and os.environ.get("DISPLAY"):
                self._start_dock()

    @property
    def hosts_3d(self) -> bool:
        """The 3D viewer can live inside this overlay (web renderer only)."""
        return self.enabled and self.renderer == "web"

    def _start(self) -> None:
        try:
            import PySide6  # noqa: F401
        except ImportError:
            log.warning("PySide6 not installed; screen overlay disabled (pip install PySide6)")
            self.enabled = False
            return
        env = dict(os.environ)
        if self.cfg.get("overlay.qt_platform"):
            env["QT_QPA_PLATFORM"] = self.cfg.get("overlay.qt_platform")
        if self.safe_gpu:
            env["BATSY_SAFE_GPU"] = "1"
        module = "batsy.overlay_web" if self.renderer == "web" else "batsy.overlay_ui"
        try:
            err = open(DATA_DIR / "overlay.log", "ab")
            self.proc = subprocess.Popen(
                [sys.executable, "-m", module], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                text=True, bufsize=1, env=env, start_new_session=True,
            )
            err.close()
        except OSError as e:
            log.warning("overlay unavailable: %s", e)
            self.enabled = False
            return
        log.info("overlay started (%s%s)", self.renderer, ", software rendering" if self.safe_gpu else "")
        if self.origin:  # a restarted overlay still needs to know where the bat is
            self.proc.stdin.write(json.dumps({"cmd": "origin", **self.origin}) + "\n")
            self.proc.stdin.flush()
        threading.Thread(target=self._events, args=(self.proc,), daemon=True, name="overlay-events").start()

    def _events(self, proc) -> None:
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            try:
                if ev.get("event") == "dismissed" and self.on_dismiss:
                    self.on_dismiss()
                elif ev.get("event") == "saver_exit" and self.on_saver_exit:
                    self.on_saver_exit()
                elif ev.get("event") == "measured" and self.on_viewer_event:
                    self.on_viewer_event(ev)
                elif ev.get("event") == "shown":
                    self._dock_send("away")  # the overlay is growing out of the bat
                elif ev.get("event") == "hidden":
                    self._rest()
            except Exception:
                log.exception("overlay event %s", ev)
        code = proc.wait()
        if code not in (0, None) and proc is self.proc:
            self._died(code)

    def _died(self, code: int) -> None:
        """Pick how to come back: same renderer, then software rendering, then the classic overlay."""
        now = time.time()
        self.deaths = [t for t in self.deaths if now - t < 300] + [now]
        log.warning("overlay exited with code %s (%d times in 5 min); see overlay.log", code, len(self.deaths))
        if self.renderer == "web" and (code == 3 or len(self.deaths) >= 2) and not self.safe_gpu:
            self.safe_gpu = True
        elif self.renderer == "web" and len(self.deaths) >= 4:
            self.renderer, self.safe_gpu = "classic", False
            log.warning("web overlay keeps failing; using the classic overlay")
        self.next_start = now + min(30, 2 ** len(self.deaths))

    # --- the standby bat ----------------------------------------------------------------------------------
    def _start_dock(self) -> None:
        env = dict(os.environ, QT_QPA_PLATFORM="xcb")  # XWayland: may sit in a corner and stay on top
        try:
            err = open(DATA_DIR / "dock.log", "ab")
            self.dock = subprocess.Popen([sys.executable, "-m", "batsy.dock"], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=err, text=True, bufsize=1, env=env,
                                         start_new_session=True)
            err.close()
        except OSError as e:
            log.warning("standby bat unavailable: %s", e)
            return
        threading.Thread(target=self._dock_events, args=(self.dock,), daemon=True, name="dock-events").start()
        self._dock_send("standby")

    def _dock_events(self, proc) -> None:
        from urllib.parse import quote

        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("event") == "origin":
                logo = DATA_DIR / "assets" / "bat.svg"
                self.origin = {"x": ev["x"], "y": ev["y"], "logo": "batsy://file" + quote(str(logo)) if logo.exists() else ""}
                self.send("origin", **self.origin)
            elif ev.get("event") == "open" and self.on_dock_open:
                try:
                    self.on_dock_open()
                except Exception:
                    log.exception("dock open")
        if proc is self.dock and proc.wait() not in (0, None):
            now = time.time()
            self.dock_deaths = [t for t in self.dock_deaths if now - t < 300] + [now]
            if len(self.dock_deaths) < 4:
                log.warning("standby bat exited; restarting (see dock.log)")
                self._start_dock()

    def _dock_send(self, cmd: str, **kw) -> None:
        with self.dock_lock:
            if self.dock is None or self.dock.poll() is not None:
                return
            try:
                self.dock.stdin.write(json.dumps({"cmd": cmd, **kw}) + "\n")
                self.dock.stdin.flush()
            except (BrokenPipeError, OSError):
                self.dock = None

    def _rest(self) -> None:
        """The overlay closed: the bat comes back, with this conversation's answer (or picture) in a bubble."""
        c = self.convo
        if c["fresh"] and (c["text"] or c["image"] or c["title"]):
            self._dock_send("rest", you=c["you"], text=c["text"], image=c["image"], title=c["title"])
            c["fresh"] = False  # shown once; a later screensaver exit just brings back the bat
        else:
            self._dock_send("standby")

    def _track(self, cmd: str, kw: dict) -> None:
        c = self.convo
        if cmd == "listen" and kw.get("fresh"):
            c.update(you="", text="", image=None, title="", fresh=False, open=False)
        elif cmd == "heard":  # a new exchange: the bubble is about this one, not everything said before
            c.update(you=kw.get("text", ""), text="", open=True)
        elif cmd == "say":
            if not c["open"]:  # Batsy speaking up on its own (a reminder, a check-in question)
                c.update(you="", text="", open=True)
            c["text"] = (c["text"] + " " + kw.get("text", "")).strip()[:1200]
            c["fresh"] = True
        elif cmd == "done":
            c["open"] = False
        elif cmd == "image":
            c["image"], c["fresh"] = kw.get("path"), True
        elif cmd == "card":
            c["title"], c["fresh"] = kw.get("title", ""), True

    def send(self, cmd: str, **kw) -> None:
        if not self.enabled:
            return
        self._track(cmd, kw)
        if cmd != "saver":
            self.last_active = time.time()
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                if time.time() < self.next_start:  # backing off after a crash: drop this update
                    return
                log.warning("overlay process not running; starting it")
                self._start()
                if not self.enabled:
                    return
            try:
                self.proc.stdin.write(json.dumps({"cmd": cmd, **kw}) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self.proc = None

    def close(self) -> None:
        """Stop the overlay (and the standby bat) on purpose (not counted as a crash)."""
        with self.lock:
            proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.terminate()
        with self.dock_lock:
            dock, self.dock = self.dock, None
        if dock and dock.poll() is None:
            dock.terminate()

    # --- conveniences -----------------------------------------------------
    def listen(self, fresh: bool = False) -> None:
        if fresh:
            self.has_media = False
        self.send("listen", fresh=fresh)

    def level(self, v: float) -> None:
        self.send("level", v=round(v, 3))

    def partial(self, text: str) -> None:
        self.send("partial", text=text)

    def heard(self, text: str) -> None:
        self.send("heard", text=text)

    def think(self) -> None:
        self.send("think")

    def say(self, text: str) -> None:
        self.send("say", text=text)

    def image(self, path: str, caption: str = "") -> None:
        self.has_media = True
        self.send("image", path=str(path), caption=caption)

    def card(self, title: str, text: str) -> None:
        self.has_media = True
        self.send("card", title=title, text=text)

    def done(self) -> None:
        key = "overlay.linger_media_seconds" if self.has_media else "overlay.linger_seconds"
        self.send("done", linger=self.cfg.get(key))

    def hide(self) -> None:
        self.send("hide")

    def saver_spec(self, payload: dict) -> None:
        """Screensaver: theme + widgets + live data (see batsy/saver.py)."""
        self.send("saver", **payload)
