"""Daemon-side handle on the overlay process (batsy/overlay_ui.py). Every call is fire-and-forget,
and everything degrades to a no-op if the overlay is disabled or PySide6 isn't installed."""
import json
import logging
import os
import subprocess
import sys
import threading

log = logging.getLogger("batsy.overlay")


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
        if self.enabled:
            self._start()

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
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "batsy.overlay_ui"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                text=True, bufsize=1, env=env, start_new_session=True,
            )
        except OSError as e:
            log.warning("overlay unavailable: %s", e)
            self.enabled = False
            return
        threading.Thread(target=self._events, args=(self.proc,), daemon=True, name="overlay-events").start()

    def _events(self, proc) -> None:
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("event") == "dismissed" and self.on_dismiss:
                self.on_dismiss()
            elif ev.get("event") == "saver_exit" and self.on_saver_exit:
                self.on_saver_exit()

    def send(self, cmd: str, **kw) -> None:
        if not self.enabled:
            return
        if cmd != "saver":
            import time

            self.last_active = time.time()
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                log.warning("overlay process exited; restarting")
                self._start()
                if not self.enabled:
                    return
            try:
                self.proc.stdin.write(json.dumps({"cmd": cmd, **kw}) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self.proc = None

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

    def saver(self, stats: dict, next_text: str = "", extra: dict | None = None) -> None:
        self.send("saver", stats=stats, next=next_text, **(extra or {}))
