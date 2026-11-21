"""The cockpit's window: full screen over everything, but it never takes the keyboard. It opens after a few seconds
without mouse or keyboard and closes the moment you touch either, so whatever you were typing into keeps the focus
and nothing you type is lost.

JSON lines on stdin: {"cmd": "show"} {"cmd": "hide"} and everything else goes to the page (batsy/ui/cockpit.html):
{"cmd": "data", ...} the whole picture, {"cmd": "listen"|"think"|"say"|"heard"|"level"|"speakwave"|...} Batsy live.
"""
import json
import logging
import sys
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from batsy import webui

log = logging.getLogger("batsy.cockpit")


class Reader(QObject):
    message = Signal(dict)


class Cockpit(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Batsy cockpit")
        # on top, no frame, and never the window that gets your keys
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus | Qt.Tool)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setStyleSheet("background: #06070a;")
        self.web = webui.Web("cockpit.html", lambda ev: None, self)
        self.web.view.setFocusPolicy(Qt.NoFocus)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.web.view)
        self.last_data: dict | None = None
        self.web.on_reload = lambda: self.last_data and self.web.send(self.last_data)

    def handle(self, m: dict) -> None:
        cmd = m.get("cmd")
        if cmd == "show":
            if not self.isVisible():
                self.web.send({"cmd": "appear"})
                self.showFullScreen()
            return
        if cmd == "hide":
            if self.isVisible():
                self.hide()
                self.web.send({"cmd": "away"})
            return
        if cmd == "data":
            self.last_data = m
        self.web.send(m)


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s cockpit %(levelname)s: %(message)s")
    webui.prepare()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationDisplayName("Batsy cockpit")
    win = Cockpit()
    reader = Reader()

    def handle(m: dict) -> None:
        if m.get("cmd") == "__eof__":
            app.quit()
            return
        try:
            win.handle(m)
        except Exception:
            log.exception("cockpit command %s", m.get("cmd"))

    reader.message.connect(handle)

    def read_stdin() -> None:
        for line in sys.stdin:
            try:
                reader.message.emit(json.loads(line))
            except ValueError:
                pass
        reader.message.emit({"cmd": "__eof__"})

    threading.Thread(target=read_stdin, daemon=True, name="stdin").start()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
