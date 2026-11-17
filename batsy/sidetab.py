"""Batsy's side tab: always on the right edge of the screen, under the corner bat, showing what Batsy is doing.

Idle, it's a thin line at the edge in the colour of Batsy's state. When Batsy is working it slides open: every job with
its current step and how long it's been going ("Commit & push batsy - Writing the commit message (local AI) 0:12",
"Print expert - Slicing: dice.stl --copies 5 --size 41"), a bar when there's real progress, what it just finished
and what it's waiting for you to answer. It folds back a few seconds after the work is done; click it to keep it
open (or closed).

Its own small process on XWayland (like the corner bat: GNOME on Wayland won't let a normal window stay on top at an
edge). JSON lines on stdin from the daemon: {"state", "status", "agents": [...], "done": [...], "asks": [...]}.
"""
import json
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QObject, QRectF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QWidget

FONTS = Path(__file__).resolve().parent / "ui" / "fonts"
SIGNAL, BONE, STEEL = QColor(255, 194, 14), QColor(238, 232, 218), QColor(141, 149, 165)
GOOD, BAD = QColor(60, 207, 110), QColor(255, 92, 77)
STATE_COLOUR = {"idle": STEEL, "listening": QColor(122, 167, 255), "thinking": SIGNAL, "working": SIGNAL,
                "speaking": GOOD}
OPEN_W, EDGE = 312, 12   # panel width; the always-visible edge line
TOP = 54 + 300 + 10      # below the corner bat's window and its speech bubble (dock.py: TOP 54, H 300)
PAD, ROW = 14, 46


def fmt(secs: int) -> str:
    return f"{secs // 60}:{secs % 60:02d}" if secs < 3600 else f"{secs // 3600}h {secs % 3600 // 60:02d}m"


class Reader(QObject):
    message = Signal(dict)


class SideTab(QWidget):
    """The window is always the full panel's size, pinned to the edge; the panel slides in and out inside it (moving
    a window frame by frame looks jerky), and only the visible part takes clicks."""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.data: dict = {"state": "idle", "agents": [], "done": [], "asks": []}
        self.got_at = time.time()
        self.open = 0.0          # 0 = folded to the edge line, 1 = fully out
        self.pinned: bool | None = None   # clicked: stays open (True) / folded (False); clicked again: automatic
        self.idle_since = time.time()
        self.anim = QVariantAnimation(self, duration=380, easingCurve=QEasingCurve.OutQuint)
        self.anim.valueChanged.connect(self._set_open)
        self.tick = QTimer(self, interval=60, timeout=self._tick)  # the breathing dot; timers
        self.tick.start()
        self.f_head = QFont("Inter", 10, QFont.DemiBold)
        self.f_label = QFont("Inter", 10, QFont.Medium)
        self.f_step = QFont("Inter", 9)
        self.f_small = QFont("Inter", 8)
        self._place()

    # --- data ----------------------------------------------------------------------------------------------
    def handle(self, m: dict) -> None:
        self.data = {**self.data, **m}
        self.got_at = time.time()
        if self._busy():
            self.idle_since = 0.0
        elif not self.idle_since:
            self.idle_since = time.time()
        self._decide()
        self._place()
        self.update()

    def _busy(self) -> bool:
        return bool(self.data.get("agents")) or bool(self.data.get("asks"))

    def _decide(self) -> None:
        want = self.pinned if self.pinned is not None else (self._busy() or time.time() - self.idle_since < 5)
        target = 1.0 if want else 0.0
        current = self.anim.endValue() if self.anim.state() == QVariantAnimation.Running else self.open
        if abs(target - (current or 0)) > 1e-3:
            self.anim.stop()
            self.anim.setStartValue(self.open)
            self.anim.setEndValue(target)
            self.anim.start()

    def _tick(self) -> None:
        self._decide()
        if self._busy() or 0 < self.open < 1 or self.data.get("state") not in ("idle", None):
            self.update()

    def _set_open(self, v) -> None:
        self.open = float(v)
        self._mask()
        self.update()

    def mousePressEvent(self, e) -> None:
        self.pinned = None if self.pinned is not None else (self.open < 0.5)
        self._decide()

    # --- geometry ----------------------------------------------------------------------------------------------
    def _rows(self) -> list[tuple]:
        d = self.data
        return ([("job", a) for a in d.get("agents") or []] + [("ask", q) for q in d.get("asks") or []] +
                [("done", x) for x in (d.get("done") or [])[:3]])

    def _height(self) -> int:
        n = len(self._rows())
        room = QGuiApplication.primaryScreen().availableGeometry().height() - TOP - 12  # down to the screen's bottom
        return max(84, min(room, PAD * 2 + 26 + max(1, n) * ROW))

    def _place(self) -> None:
        geo = QGuiApplication.primaryScreen().availableGeometry()
        h = self._height()
        if (self.width(), self.height()) != (OPEN_W, h):
            self.setGeometry(geo.right() - OPEN_W + 1, geo.top() + TOP, OPEN_W, h)
        self._mask()

    def _x(self) -> float:
        """Where the panel's left edge is in the window: off to the right when folded, sliding in as it opens."""
        return (OPEN_W - EDGE) * (1 - self.open)

    def _mask(self) -> None:
        from PySide6.QtGui import QRegion

        x = int(self._x())
        self.setMask(QRegion(x - 6, 0, OPEN_W - x + 6, self.height()))  # only what you see takes clicks

    # --- drawing -----------------------------------------------------------------------------------------------
    def _colour(self) -> QColor:
        state, jobs = self.data.get("state", "idle"), self.data.get("agents") or []
        return STATE_COLOUR.get("working" if jobs and state == "idle" else state, STEEL)

    def _breath(self, speed: float = 1.4) -> float:
        import math

        return 0.5 + 0.5 * math.sin(time.time() * math.pi * 2 / speed)

    def paintEvent(self, _) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        x, h = self._x(), float(self.height())
        colour = self._colour()
        # the edge line: always there, the colour of what Batsy is doing; it glows gently while busy
        line_h = 64 if self.open < 0.5 else h - 24
        glow = QColor(colour)
        glow.setAlphaF(0.25 + (0.3 * self._breath() if self._busy() else 0))
        p.setPen(Qt.NoPen)
        p.setBrush(glow)
        p.drawRoundedRect(QRectF(x, 10, 10, line_h + 4), 5, 5)
        solid = QColor(colour)
        solid.setAlphaF(0.95)
        p.setBrush(solid)
        p.drawRoundedRect(QRectF(x + 2.5, 13, 5, line_h - 2), 2.5, 2.5)
        n = len(self.data.get("agents") or [])
        if self.open < 0.6 and n:
            p.setOpacity(1 - self.open / 0.6)
            p.setPen(QColor(18, 20, 26))
            p.setBrush(colour)
            p.drawEllipse(QRectF(x - 4, 14 + line_h + 6, 16, 16))
            p.setFont(self.f_small)
            p.drawText(QRectF(x - 4, 14 + line_h + 6, 16, 16), Qt.AlignCenter, str(n))
            p.setOpacity(1)
        if self.open <= 0.01:
            p.end()
            return
        # the panel, sliding in from the edge
        panel = QRectF(x + EDGE, 0, OPEN_W - x - EDGE + 16, h)
        path = QPainterPath()
        path.addRoundedRect(panel, 16, 16)
        p.setOpacity(min(1.0, self.open * 1.4))
        p.fillPath(path, QColor(20, 22, 28, 242))
        p.setBrush(Qt.NoBrush)  # (just the outline: the state colour is still the brush)
        p.setPen(QPen(QColor(255, 255, 255, 18), 1))
        p.drawPath(path)
        p.setOpacity(max(0.0, (self.open - 0.35) / 0.65))  # the words appear once it's mostly out
        self._paint_panel(p, panel.x() + PAD, panel.width() - 16 - 2 * PAD)
        p.end()

    def _paint_panel(self, p: QPainter, x0: float, w: float) -> None:
        state, jobs = self.data.get("state", "idle"), self.data.get("agents") or []
        y = PAD
        title = {"listening": "Listening", "thinking": "Thinking", "speaking": "Speaking"}.get(state, "")
        title = title or (f"Working on {len(jobs)}" if len(jobs) > 1 else "Working" if jobs else "Idle")
        p.setPen(BONE)
        p.setFont(self.f_head)
        p.drawText(QRectF(x0, y, w, 18), Qt.AlignVCenter | Qt.AlignLeft, title)
        status = self.data.get("status") or ""
        if status and state != "idle":
            p.setPen(STEEL)
            p.setFont(self.f_small)
            fm = QFontMetrics(self.f_small)
            p.drawText(QRectF(x0 + 90, y, w - 90, 18), Qt.AlignVCenter | Qt.AlignRight,
                       fm.elidedText(status, Qt.ElideRight, int(w - 90)))
        y += 26
        rows = self._rows()
        if not rows:
            p.setPen(STEEL)
            p.setFont(self.f_step)
            p.drawText(QRectF(x0, y, w, 18), Qt.AlignLeft, "Nothing running")
            return
        elapsed = time.time() - self.got_at
        for kind, it in rows:
            if y + ROW > self.height():
                break
            self._paint_row(p, kind, it, x0, y, w, elapsed)
            y += ROW

    def _paint_row(self, p: QPainter, kind: str, it: dict, x: float, y: float, w: float, elapsed: float) -> None:
        fm_l, fm_s = QFontMetrics(self.f_label), QFontMetrics(self.f_step)
        base = p.opacity()
        if kind == "job":
            dot = QColor(SIGNAL)
            dot.setAlphaF(0.45 + 0.55 * self._breath(1.6))  # breathing: alive, not busy-looking
            p.setPen(Qt.NoPen)
            p.setBrush(dot)
            p.drawEllipse(QRectF(x, y + 5, 7, 7))
            p.setPen(BONE)
            p.setFont(self.f_label)
            p.drawText(QRectF(x + 16, y, w - 64, 17), Qt.AlignVCenter | Qt.AlignLeft,
                       fm_l.elidedText(it.get("label", ""), Qt.ElideRight, int(w - 64)))
            p.setPen(STEEL)
            p.setFont(self.f_small)
            p.drawText(QRectF(x + w - 46, y, 46, 17), Qt.AlignVCenter | Qt.AlignRight, fmt(int(it.get("secs", 0) + elapsed)))
            p.setPen(QColor(200, 196, 186))
            p.setFont(self.f_step)
            p.drawText(QRectF(x + 16, y + 18, w - 16, 16), Qt.AlignVCenter | Qt.AlignLeft,
                       fm_s.elidedText(it.get("step") or "Working…", Qt.ElideRight, int(w - 16)))
            pct = it.get("pct")
            if pct is not None:  # a bar only when there's real progress
                bar = QRectF(x + 16, y + 37, w - 16, 2)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, 20))
                p.drawRoundedRect(bar, 1, 1)
                p.setBrush(SIGNAL)
                p.drawRoundedRect(QRectF(bar.x(), bar.y(), bar.width() * max(0, min(100, pct)) / 100, 2), 1, 1)
        elif kind == "ask":
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(122, 167, 255))
            p.drawEllipse(QRectF(x, y + 5, 7, 7))
            p.setPen(BONE)
            p.setFont(self.f_label)
            p.drawText(QRectF(x + 16, y, w - 16, 17), Qt.AlignVCenter | Qt.AlignLeft, "Waiting for your answer")
            p.setPen(QColor(200, 196, 186))
            p.setFont(self.f_step)
            p.drawText(QRectF(x + 16, y + 18, w - 16, 28), Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop,
                       it.get("question", ""))
        else:  # done
            p.setOpacity(base * 0.6)
            ok = it.get("ok", True)
            p.setPen(Qt.NoPen)
            p.setBrush(GOOD if ok else BAD)
            p.drawEllipse(QRectF(x, y + 5, 7, 7))
            p.setPen(BONE)
            p.setFont(self.f_label)
            p.drawText(QRectF(x + 16, y, w - 64, 17), Qt.AlignVCenter | Qt.AlignLeft,
                       fm_l.elidedText(it.get("label", ""), Qt.ElideRight, int(w - 64)))
            p.setPen(STEEL)
            p.setFont(self.f_small)
            p.drawText(QRectF(x + w - 46, y, 46, 17), Qt.AlignVCenter | Qt.AlignRight, fmt(it.get("secs", 0)))
            p.setFont(self.f_step)
            p.drawText(QRectF(x + 16, y + 18, w - 16, 16), Qt.AlignVCenter | Qt.AlignLeft,
                       fm_s.elidedText(it.get("result") or "Done", Qt.ElideRight, int(w - 16)))
            p.setOpacity(base)


def main() -> None:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    for f in FONTS.glob("*.woff2"):
        QFontDatabase.addApplicationFont(str(f))
    tab = SideTab()
    tab.show()
    reader = Reader()

    def handle(m: dict) -> None:
        if m.get("cmd") == "__eof__":
            app.quit()
            return
        try:
            tab.handle(m)
        except Exception as e:  # never take the tab down
            print(f"sidetab: {e}", file=sys.stderr, flush=True)

    reader.message.connect(handle)

    def read_stdin() -> None:
        for line in sys.stdin:
            try:
                reader.message.emit(json.loads(line))
            except ValueError:
                pass
        reader.message.emit({"cmd": "__eof__"})

    threading.Thread(target=read_stdin, daemon=True).start()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
