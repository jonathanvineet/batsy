"""Batsy's full-screen overlay, drawn by the GPU: a Qt window holding the web UI in batsy/ui (three.js + HTML).

Same JSON-lines protocol on stdin as the classic painter overlay (batsy/overlay_ui.py), so the daemon can use
either. This process owns the window (show/hide, linger timers, the live camera feed) and hosts the 3D viewer's
scene; everything visual happens in the page. Events go out on stdout:
  {"event": "dismissed"} {"event": "saver_exit"} {"event": "measured", "mm"}
  {"event": "shown"} {"event": "hidden"}  (every time the window really appears / goes, whatever the reason)
Exit code 3 means the GPU renderer kept crashing: the daemon restarts us with software rendering.
"""
import json
import logging
import os
import sys
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from batsy import webui
from batsy.camera_view import FLASH_PATH

log = logging.getLogger("batsy.overlay")
SHOWN_CMDS = {"listen", "think", "say", "image", "card", "status"}


def emit(ev: dict) -> None:
    try:
        print(json.dumps(ev), flush=True)
    except (BrokenPipeError, OSError):
        pass


class Shell(QWidget):
    present = Signal(bool)  # from the 3D socket threads

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Batsy")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)  # the page grows out of the corner bat over your desktop
        self.origin: dict | None = None  # where the standby bat sits on screen (from the dock)
        self.collapsing = False
        self.collapse_notify = False
        self.collapse_timer = QTimer(self, singleShot=True, interval=1200, timeout=self._finish_dismiss)
        self.web = webui.Web("index.html", self._ui_event, self)
        self.web.on_reload = self._replay
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.web.view)
        self.saver = False
        self.model_on = False
        self.scene = None
        self.camera_path: str | None = None
        self.camera_mtime = 0.0
        self.camera_looking = None
        self.cam_timer = QTimer(self, interval=100, timeout=self._camera_frame)
        self.hide_timer = QTimer(self, singleShot=True, timeout=lambda: self._dismiss(notify=True))
        self.present.connect(self.show_3d)

    # --- commands from the daemon ---------------------------------------------------------------------------
    def handle(self, m: dict) -> None:
        cmd = m.get("cmd")
        if cmd == "origin":
            self.origin = {k: m.get(k) for k in ("x", "y", "logo")}
            self.web.send({"cmd": "origin", **self.origin})
            return
        if cmd == "saver":
            first = not self.saver
            self.saver = True
            self.web.send(m)
            if first:
                self.hide_timer.stop()
                self._appear()
            return
        if cmd in ("listen", "think", "say", "image", "card", "hide"):
            self.saver = False
        if cmd == "image":
            m = {**m, "url": webui.file_url(m.get("path", ""))}
        if cmd == "camera":
            if m.get("on"):
                self.camera_path, self.camera_mtime, self.camera_looking = m.get("path"), 0.0, None
                self.cam_timer.start()
                self.web.send({"cmd": "camera", "on": True})
                self._appear()
                self._camera_frame()
            else:
                self.camera_path = None
                self.cam_timer.stop()
                self.web.send({"cmd": "camera", "on": False})
            return
        if cmd == "model3d":
            self._ensure_scene()
            return
        if cmd in ("listen", "say"):
            self.hide_timer.stop()
        if cmd in SHOWN_CMDS:
            self._appear()
        if cmd == "done":
            busy = self.camera_path or self.model_on  # live views stay up until closed or idle a while
            linger = float(m.get("linger", 6))
            self.hide_timer.start(int(1000 * (max(linger, 180) if busy else linger)))
        if cmd == "hide":
            if self.camera_path is None:  # the live camera stays up until it's closed
                self._dismiss(notify=False)
            return
        self.web.send(m)

    # --- 3D -----------------------------------------------------------------------------------------------
    def _ensure_scene(self) -> None:
        if self.scene is not None:
            return
        from batsy import viewer3d

        self.scene = viewer3d.Scene(self.web.send, self.present.emit, lambda: self.web.call("batsy.viewer.snapshot()"))
        viewer3d.serve(self.scene)

    def show_3d(self, on: bool) -> None:
        self.model_on = on
        self.web.send({"cmd": "model3d", "on": on})
        if on:
            self.hide_timer.stop()
            self._appear()

    def _replay(self) -> None:
        """The page (re)loaded, e.g. after a renderer crash: put back what should be on screen."""
        if self.scene is not None:
            threading.Thread(target=self._resync, daemon=True).start()
        if self.camera_path:
            self.web.send({"cmd": "camera", "on": True})
            self.camera_mtime = 0.0
        if self.origin:
            self.web.send({"cmd": "origin", **self.origin})
        if self.isVisible():
            self.web.send({"cmd": "appear"})

    def _resync(self) -> None:
        with self.scene.lock:
            self.scene.sync(reset_camera=True)
        if self.model_on:
            self.web.send({"cmd": "model3d", "on": True})

    # --- window -------------------------------------------------------------------------------------------
    def _appear(self) -> None:
        if self.collapsing:  # asked to show while closing: open straight back up
            self.collapse_timer.stop()
            self.collapsing = False
            self.web.send({"cmd": "appear"})
            return
        if not self.isVisible():
            self.web.send({"cmd": "appear"})
            self.showFullScreen()
            self.raise_()
            self.activateWindow()
            self.web.view.setFocus()
            emit({"event": "shown"})

    def _dismiss(self, notify: bool = True) -> None:
        """Fold back into the corner bat (the page animates; we hide when it says it's done, or after 1.2 s)."""
        self.hide_timer.stop()
        if not self.isVisible():
            return
        self.collapse_notify = self.collapse_notify or notify
        if self.collapsing:
            return
        self.collapsing = True
        self.web.send({"cmd": "collapse"})
        self.collapse_timer.start()

    def _finish_dismiss(self) -> None:
        if not self.collapsing:
            return
        self.collapsing = False
        self.collapse_timer.stop()
        was_saver, self.saver = self.saver, False
        notify, self.collapse_notify = self.collapse_notify, False
        self.web.send({"cmd": "hidden"})
        self.hide()
        emit({"event": "hidden"})
        if notify:
            emit({"event": "saver_exit" if was_saver else "dismissed"})

    def _ui_event(self, ev: dict) -> None:
        kind = ev.get("event")
        if kind in ("dismiss", "saver_exit"):
            self._dismiss(notify=True)
        elif kind == "collapsed":
            self._finish_dismiss()
        elif kind == "measured":
            if self.scene is not None:
                threading.Thread(target=self.scene.measured, args=(ev.get("mm", 0),), daemon=True).start()
            emit({"event": "measured", "mm": ev.get("mm")})
        elif kind == "model_closed":
            self.model_on = False

    def _camera_frame(self) -> None:
        try:
            mtime = os.stat(self.camera_path).st_mtime
        except (OSError, TypeError):
            return
        try:
            looking = FLASH_PATH.exists() and time.time() - FLASH_PATH.stat().st_mtime < 1.5
        except OSError:
            looking = False
        if looking != self.camera_looking:
            self.camera_looking = looking
            self.web.send({"cmd": "camera_caption", "looking": looking})
        if mtime != self.camera_mtime:
            self.camera_mtime = mtime
            self.web.send({"cmd": "frame", "url": f"{webui.file_url(self.camera_path)}?v={mtime}"})


class Reader(QObject):
    message = Signal(dict)


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s overlay %(levelname)s: %(message)s")
    webui.prepare()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationDisplayName("Batsy")
    shell = Shell()
    reader = Reader()

    def safe_handle(m: dict) -> None:
        if m.get("cmd") == "__eof__":
            app.quit()
            return
        try:
            shell.handle(m)
        except Exception:  # one bad message must never take the screen down
            log.exception("overlay command %s", m.get("cmd"))

    reader.message.connect(safe_handle)

    def read_stdin() -> None:
        for line in sys.stdin:
            try:
                reader.message.emit(json.loads(line))
            except ValueError:
                pass
        reader.message.emit({"cmd": "__eof__"})  # parent went away

    threading.Thread(target=read_stdin, daemon=True, name="stdin").start()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
