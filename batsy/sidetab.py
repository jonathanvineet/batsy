"""Batsy's side tab: always on the right edge of the screen, under the corner bat, showing what Batsy is doing.

Idle, it's a slim tab (the bat's colour shows Batsy's state). When Batsy is working it slides open: every job with
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
KIND_ICON = {"claude": "✦", "turn": "🗣", "git": "⎇", "slice": "▤", "local": "◐", "search": "⌕", "download": "⇣",
             "voice": "🎙", "shell": "›_", "monitor": "◉", "batsy": "●", "learn": "✎"}
TAB_W, OPEN_W, TOP, MARGIN = 26, 320, 118, 0   # under the corner bat (dock.py sits at TOP 54)
PAD, ROW = 14, 52


def fmt(secs: int) -> str:
    return f"{secs // 60}:{secs % 60:02d}" if secs < 3600 else f"{secs // 3600}h {secs % 3600 // 60:02d}m"


class Reader(QObject):
    message = Signal(dict)


class SideTab(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.data: dict = {"state": "idle", "agents": [], "done": [], "asks": []}
        self.got_at = time.time()
        self.open = 0.0          # 0 = folded into the tab, 1 = fully open
        self.pinned: bool | None = None   # clicked: stays open (True) / closed (False) until the next change of mind
        self.idle_since = time.time()
        self.anim = QVariantAnimation(self, duration=260, easingCurve=QEasingCurve.OutCubic)
        self.anim.valueChanged.connect(self._set_open)
        self.tick = QTimer(self, interval=500, timeout=self._tick)  # timers count up; the spinner turns
        self.tick.start()
        self.f_title = QFont("Barlow Condensed", 13, QFont.Bold)
        self.f_label = QFont("Inter", 10, QFont.DemiBold)
        self.f_step = QFont("Inter", 9)
        self.f_small = QFont("Inter", 8)
        self._place()

    # --- data ----------------------------------------------------------------------------------------------
    def handle(self, m: dict) -> None:
        self.data = {**self.data, **m}
        self.got_at = time.time()
        busy = bool(self.data.get("agents")) or bool(self.data.get("asks"))
        if busy:
            self.idle_since = 0.0
        elif not self.idle_since:
            self.idle_since = time.time()
        self._decide()
        self._place()
        self.update()

    def _decide(self) -> None:
        busy = bool(self.data.get("agents")) or bool(self.data.get("asks"))
        want = self.pinned if self.pinned is not None else (busy or time.time() - self.idle_since < 6)
        target = 1.0 if want else 0.0
        if target != (self.anim.endValue() if self.anim.state() else self.open):
            self.anim.stop()
            self.anim.setStartValue(self.open)
            self.anim.setEndValue(target)
            self.anim.start()

    def _tick(self) -> None:
        self._decide()
        if self.data.get("agents") or self.open > 0:
            self.update()

    def _set_open(self, v) -> None:
        self.open = float(v)
        self._place()
        self.update()

    def mousePressEvent(self, e) -> None:
        # a click: keep it the other way round from now (open while idle, or folded while busy); again: automatic
        self.pinned = None if self.pinned is not None else (self.open < 0.5)
        self._decide()

    # --- layout ---------------------------------------------------------------------------------------------
    def _rows(self) -> list[tuple]:
        d, rows = self.data, []
        for a in d.get("agents") or []:
            rows.append(("job", a))
        for q in d.get("asks") or []:
            rows.append(("ask", q))
        for x in (d.get("done") or [])[:3]:
            rows.append(("done", x))
        return rows

    def _height(self) -> int:
        n = len(self._rows())
        return max(120, min(int(QGuiApplication.primaryScreen().availableGeometry().height() * 0.7),
                            PAD * 2 + 34 + max(1, n) * ROW + 6))

    def _place(self) -> None:
        geo = QGuiApplication.primaryScreen().availableGeometry()
        w = int(TAB_W + (OPEN_W - TAB_W) * self.open)
        h = self._height() if self.open > 0.02 else 96
        self.setGeometry(geo.right() - w - MARGIN + 1, geo.top() + TOP, w, h)

    # --- drawing ---------------------------------------------------------------------------------------------
    def paintEvent(self, _) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, 0, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r.adjusted(0, 0, 14, 0), 14, 14)  # rounded on the left only (the right is the screen edge)
        p.fillPath(path, QColor(18, 20, 26, 236))
        p.setPen(QPen(QColor(255, 255, 255, 26), 1))
        p.drawPath(path)
        state = self.data.get("state", "idle")
        jobs = self.data.get("agents") or []
        colour = STATE_COLOUR.get("working" if jobs and state == "idle" else state, STEEL)
        if self.open < 0.35:
            self._paint_tab(p, colour, len(jobs))
        if self.open > 0.3:
            p.setOpacity(min(1.0, (self.open - 0.3) / 0.5))
            self._paint_panel(p, colour, state, jobs)
        p.end()

    def _spinner(self, p: QPainter, cx: float, cy: float, rad: float, colour: QColor) -> None:
        a = (time.time() * 360) % 360
        p.setPen(QPen(colour, 2.2, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(QRectF(cx - rad, cy - rad, rad * 2, rad * 2), int(-a * 16), 100 * 16)

    def _paint_tab(self, p: QPainter, colour: QColor, n: int) -> None:
        cx = TAB_W / 2
        if n:
            self._spinner(p, cx, 22, 7, colour)
            p.setPen(BONE)
            p.setFont(self.f_label)
            p.drawText(QRectF(0, 34, TAB_W, 18), Qt.AlignCenter, str(n))
        else:
            p.setBrush(colour)
            p.setPen(Qt.NoPen)
            p.drawEllipse(QRectF(cx - 4, 18, 8, 8))
        p.setPen(QColor(141, 149, 165, 160))
        p.setFont(self.f_small)
        p.save()
        p.translate(cx + 4, 88)
        p.rotate(-90)
        p.drawText(0, 0, "BATSY")
        p.restore()

    def _paint_panel(self, p: QPainter, colour: QColor, state: str, jobs: list) -> None:
        w = self.width()
        x0, y = PAD, PAD
        # header: Batsy's state, and what it's saying right now
        p.setBrush(colour)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(x0, y + 6, 9, 9))
        p.setPen(BONE)
        p.setFont(self.f_title)
        label = {"idle": "Idle", "listening": "Listening", "thinking": "Thinking", "speaking": "Speaking"}.get(state, state.title())
        if jobs and state == "idle":
            label = "Working"
        p.drawText(QRectF(x0 + 16, y, w - x0 - 30, 22), Qt.AlignVCenter | Qt.AlignLeft, label)
        status = self.data.get("status") or ""
        if status:
            p.setPen(STEEL)
            p.setFont(self.f_small)
            p.drawText(QRectF(x0 + 100, y, w - x0 - 110, 22), Qt.AlignVCenter | Qt.AlignRight,
                       QFontMetrics(self.f_small).elidedText(status, Qt.ElideRight, int(w - x0 - 110)))
        y += 34
        rows = self._rows()
        if not rows:
            p.setPen(STEEL)
            p.setFont(self.f_step)
            p.drawText(QRectF(x0, y, w - 2 * x0, 20), Qt.AlignLeft, "Nothing running.")
            return
        elapsed = time.time() - self.got_at  # timers keep counting between updates
        for kind, it in rows:
            if y + ROW > self.height():
                break
            self._paint_row(p, kind, it, x0, y, w - 2 * x0, elapsed)
            y += ROW

    def _paint_row(self, p: QPainter, kind: str, it: dict, x: float, y: float, w: float, elapsed: float) -> None:
        fm_l, fm_s = QFontMetrics(self.f_label), QFontMetrics(self.f_step)
        if kind == "job":
            self._spinner(p, x + 7, y + 10, 6, SIGNAL)
            icon = KIND_ICON.get(it.get("kind", ""), "●")
            p.setPen(BONE)
            p.setFont(self.f_label)
            secs = int(it.get("secs", 0) + elapsed)
            right = fmt(secs)
            p.drawText(QRectF(x + 20, y, w - 70, 18), Qt.AlignVCenter | Qt.AlignLeft,
                       fm_l.elidedText(f"{icon}  {it.get('label', '')}", Qt.ElideRight, int(w - 70)))
            p.setPen(STEEL)
            p.drawText(QRectF(x + w - 48, y, 48, 18), Qt.AlignVCenter | Qt.AlignRight, right)
            step = it.get("step") or "Working…"
            p.setPen(QColor(215, 210, 198))
            p.setFont(self.f_step)
            p.drawText(QRectF(x + 20, y + 18, w - 20, 16), Qt.AlignVCenter | Qt.AlignLeft,
                       fm_s.elidedText(step, Qt.ElideRight, int(w - 20)))
            bar = QRectF(x + 20, y + 38, w - 20, 3)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 22))
            p.drawRoundedRect(bar, 1.5, 1.5)
            pct = it.get("pct")
            p.setBrush(SIGNAL)
            if pct is not None:
                p.drawRoundedRect(QRectF(bar.x(), bar.y(), bar.width() * max(0, min(100, pct)) / 100, 3), 1.5, 1.5)
            else:  # no real progress: a short sweep that keeps moving, so you can see it's alive
                t = (time.time() % 1.6) / 1.6
                p.drawRoundedRect(QRectF(bar.x() + (bar.width() - 50) * t, bar.y(), 50, 3), 1.5, 1.5)
        elif kind == "ask":
            p.setPen(SIGNAL)
            p.setFont(self.f_label)
            p.drawText(QRectF(x, y, w, 18), Qt.AlignVCenter | Qt.AlignLeft, "?  Waiting for your answer")
            p.setPen(BONE)
            p.setFont(self.f_step)
            p.drawText(QRectF(x + 20, y + 18, w - 20, 30), Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop,
                       it.get("question", ""))
        else:  # done
            ok = it.get("ok", True)
            p.setOpacity(p.opacity() * 0.7)
            p.setPen(GOOD if ok else BAD)
            p.setFont(self.f_label)
            p.drawText(QRectF(x, y, 16, 18), Qt.AlignVCenter | Qt.AlignLeft, "✓" if ok else "✕")
            p.setPen(BONE)
            p.drawText(QRectF(x + 20, y, w - 70, 18), Qt.AlignVCenter | Qt.AlignLeft,
                       fm_l.elidedText(it.get("label", ""), Qt.ElideRight, int(w - 70)))
            p.setPen(STEEL)
            p.drawText(QRectF(x + w - 48, y, 48, 18), Qt.AlignVCenter | Qt.AlignRight, f"took {fmt(it.get('secs', 0))}")
            p.setFont(self.f_step)
            res = it.get("result") or ""
            p.drawText(QRectF(x + 20, y + 18, w - 20, 16), Qt.AlignVCenter | Qt.AlignLeft,
                       fm_s.elidedText(res, Qt.ElideRight, int(w - 20)))
            p.setOpacity(p.opacity() / 0.7)


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
