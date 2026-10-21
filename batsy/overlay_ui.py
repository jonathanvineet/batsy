"""Batsy's full-screen overlay (a separate Qt process, driven by JSON lines on stdin).

Commands in:  {"cmd": "listen"} {"cmd": "level", "v": 0..1} {"cmd": "partial", "text"} {"cmd": "heard", "text"}
              {"cmd": "think"} {"cmd": "say", "text"} {"cmd": "image", "path", "caption"}
              {"cmd": "card", "title", "text"} {"cmd": "done", "linger": s} {"cmd": "hide"}
              {"cmd": "saver", "stats": {...}, "next": "..."}  (screensaver; any mouse/key input ends it)
Events out:   {"event": "dismissed"}  (Esc / click / Space)
"""
import json
import math
import sys
import threading
import time

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPen, QPixmap,
                           QRadialGradient, QTextOption)
from PySide6.QtWidgets import QApplication, QWidget

ACCENT = {"listen": QColor(214, 70, 255), "think": QColor(70, 200, 255), "speak": QColor(150, 110, 255),
          "idle": QColor(120, 110, 160)}


class Bridge(QObject):
    message = Signal(dict)


class Overlay(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Batsy")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.state = "idle"
        self.level = 0.0
        self.smooth = 0.0
        self.partial = ""
        self.heard = ""
        self.answer = ""
        self.card: tuple[str, str] | None = None
        self.image: QPixmap | None = None
        self.caption = ""
        self.fade = 0.0
        self.t0 = time.time()
        self.saver: dict | None = None  # stats while in screensaver mode
        self.saver_next = ""
        self.mouse_origin = None
        self.hide_timer = QTimer(self, singleShot=True, timeout=self._dismiss)
        self.anim = QTimer(self, interval=33, timeout=self._tick)

    # --- commands ---------------------------------------------------------
    def handle(self, m: dict) -> None:
        cmd = m.get("cmd")
        if cmd == "saver":
            first = self.saver is None
            self.saver, self.saver_next = m.get("stats", {}), m.get("next", "")
            if first:
                self.hide_timer.stop()
                self.state = "idle"
                self.mouse_origin = None
                self.setMouseTracking(True)
                self.setCursor(Qt.BlankCursor)
                self._appear()
            self.update()
            return
        if cmd in ("listen", "think", "say", "image", "card", "hide"):
            self._leave_saver()
        if cmd == "listen":
            self.hide_timer.stop()
            if m.get("fresh"):
                self.partial = self.heard = self.answer = self.caption = ""
                self.image, self.card = None, None
            self.state = "listen"
            self._appear()
        elif cmd == "level":
            self.level = max(0.0, min(1.0, float(m.get("v", 0))))
        elif cmd == "partial":
            self.partial = m.get("text", "")
        elif cmd == "heard":
            self.heard, self.partial = m.get("text", ""), ""
        elif cmd == "think":
            self.state = "think"
            self._appear()
        elif cmd == "say":
            self.hide_timer.stop()
            self.state = "speak"
            self.answer = (self.answer + " " + m.get("text", "")).strip()[-900:]
            self._appear()
        elif cmd == "image":
            pix = QPixmap(m.get("path", ""))
            if not pix.isNull():
                self.image, self.caption = pix, m.get("caption", "")
                self._appear()
        elif cmd == "card":
            self.card = (m.get("title", ""), m.get("text", ""))
            self._appear()
        elif cmd == "done":
            self.state = "idle"
            self.level = 0.0
            self.hide_timer.start(int(1000 * float(m.get("linger", 6))))
        elif cmd == "hide":
            self._dismiss(notify=False)
        self.update()

    def _appear(self) -> None:
        if not self.isVisible():
            self.fade = 0.0
            self.showFullScreen()
            self.raise_()
            self.activateWindow()
            self.anim.start()

    def _leave_saver(self) -> None:
        if self.saver is not None:
            self.saver = None
            self.setMouseTracking(False)
            self.unsetCursor()

    def mouseMoveEvent(self, e):
        if self.saver is None:
            return
        pos = e.position()
        if self.mouse_origin is None:
            self.mouse_origin = pos
        elif abs(pos.x() - self.mouse_origin.x()) + abs(pos.y() - self.mouse_origin.y()) > 12:
            self._dismiss()

    def _dismiss(self, notify: bool = True) -> None:
        was_saver = self.saver is not None
        self._leave_saver()
        if was_saver and notify:
            print(json.dumps({"event": "saver_exit"}), flush=True)
            notify = False
        self.hide()
        self.anim.stop()
        self.state = "idle"
        if notify:
            print(json.dumps({"event": "dismissed"}), flush=True)

    def keyPressEvent(self, e):
        if self.saver is not None or e.key() in (Qt.Key_Escape, Qt.Key_Space, Qt.Key_Q):
            self._dismiss()

    def mousePressEvent(self, e):
        self._dismiss()

    def _tick(self) -> None:
        self.fade = min(1.0, self.fade + 0.12)
        self.smooth += (self.level - self.smooth) * 0.35
        self.update()

    # --- drawing ----------------------------------------------------------
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        w, h = self.width(), self.height()
        if self.saver is not None:
            self._paint_saver(p, w, h)
            return
        p.fillRect(self.rect(), QColor(6, 5, 14, int(225 * self.fade)))
        p.setOpacity(self.fade)

        has_media = self.image is not None or self.card is not None
        orb_c = QPointF(w * 0.08 + 40, h * 0.1 + 30) if has_media else QPointF(w / 2, h * 0.40)
        orb_r = 46 if has_media else min(w, h) * 0.11
        self._orb(p, orb_c, orb_r)

        text_w = w * 0.70
        y = (h * 0.40 + orb_r + 50) if not has_media else h * 0.80
        if not has_media:
            line = self.heard or self.partial
            if line:
                col = QColor(235, 235, 245) if self.heard else QColor(170, 165, 190)
                y = self._text(p, f"“{line}”" if self.heard else line, QRectF((w - text_w) / 2, y, text_w, 200),
                               22, col, italic=not self.heard) + 24
            if self.answer:
                self._text(p, self.answer, QRectF((w - text_w) / 2, y, text_w, h - y - 40), 30, QColor(255, 255, 255))
        else:
            top = h * 0.07
            if self.image is not None and self.card is not None:  # side by side: picture left, card right
                self._image(p, QRectF(w * 0.06, top, w * 0.52, h * 0.68))
                self._card(p, QRectF(w * 0.61, top + h * 0.04, w * 0.33, h * 0.60))
            elif self.image is not None:
                self._image(p, QRectF(w * 0.18, top, w * 0.64, h * 0.68))
            else:
                self._card(p, QRectF(w * 0.18, top, w * 0.64, h * 0.68))
            sub = self.answer or self.heard or self.partial
            if sub:
                self._text(p, sub, QRectF(w * 0.12, h * 0.80, w * 0.76, h * 0.18), 24, QColor(255, 255, 255),
                           center=True)
        p.setOpacity(0.35 * self.fade)
        self._text(p, "Esc to close", QRectF(0, h - 34, w - 24, 24), 11, QColor(200, 200, 220), align=Qt.AlignRight)

    def _paint_saver(self, p: QPainter, w: int, h: int) -> None:
        t = time.time() - self.t0
        p.fillRect(self.rect(), QColor(3, 2, 8, int(255 * self.fade)))
        p.setOpacity(self.fade)
        # everything drifts slowly so nothing burns into the panel
        dx, dy = 40 * math.sin(t / 47), 30 * math.sin(t / 61)
        self._orb(p, QPointF(w * 0.5 + dx * 3, h * 0.30 + dy * 2), min(w, h) * 0.05)
        clock = time.strftime("%H:%M")
        self._text(p, clock, QRectF(dx, h * 0.12 + dy, w, 160), 96, QColor(240, 236, 255))
        date = time.strftime("%A, %B ") + str(int(time.strftime("%d")))
        self._text(p, date, QRectF(dx, h * 0.12 + 150 + dy, w, 40), 20, QColor(170, 160, 205))

        tiles = [("This session", "session"), ("Today", "today"), ("Last 7 days", "week")]
        tw, th, gap = min(340, w * 0.26), 170, 28
        x0 = (w - (3 * tw + 2 * gap)) / 2 + dx
        y0 = h * 0.52 + dy
        for i, (label, key) in enumerate(tiles):
            st = self.saver.get(key, {})
            r = QRectF(x0 + i * (tw + gap), y0, tw, th)
            p.setPen(QPen(QColor(255, 255, 255, 30), 1.2))
            p.setBrush(QColor(255, 255, 255, 12))
            p.drawRoundedRect(r, 20, 20)
            inner = r.adjusted(24, 18, -24, -16)
            self._text(p, label.upper(), QRectF(inner.left(), inner.top(), inner.width(), 22), 11,
                       QColor(170, 160, 205), center=False)
            self._text(p, _fmt(st.get("tokens", 0)) + " tokens", QRectF(inner.left(), inner.top() + 28, inner.width(), 56),
                       30, QColor(255, 255, 255), center=False)
            sub = f"≈ ${st.get('cost', 0):.2f} · {st.get('requests', 0)} requests"
            self._text(p, sub, QRectF(inner.left(), inner.top() + 86, inner.width(), 24), 13,
                       QColor(200, 195, 225), center=False)
            total = max(1, st.get("tokens", 0))
            bar = QRectF(inner.left(), inner.bottom() - 10, inner.width(), 6)
            parts = [(st.get("cache_read", 0), QColor(90, 200, 255)), (st.get("in", 0) + st.get("cache_write", 0),
                     QColor(200, 90, 255)), (st.get("out", 0), QColor(255, 255, 255))]
            x = bar.left()
            p.setPen(Qt.NoPen)
            for val, col in parts:
                seg = bar.width() * val / total
                if seg > 0.5:
                    p.setBrush(col)
                    p.drawRoundedRect(QRectF(x, bar.top(), max(seg - 2, 1), bar.height()), 3, 3)
                    x += seg
        legend_y = y0 + th + 18
        for i, (name, col) in enumerate([("cached", QColor(90, 200, 255)), ("new input", QColor(200, 90, 255)),
                                         ("output", QColor(255, 255, 255))]):
            lx = w / 2 - 170 + i * 120 + dx
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawEllipse(QPointF(lx, legend_y + 9), 4, 4)
            self._text(p, name, QRectF(lx + 10, legend_y, 100, 20), 11, QColor(170, 160, 205), center=False)
        if self.saver_next:
            self._text(p, self.saver_next, QRectF(dx, h * 0.86 + dy, w, 30), 15, QColor(200, 195, 225))
        p.setOpacity(0.4 * self.fade)
        self._text(p, "Say “Batsy” anytime", QRectF(dx, h - 50, w, 24), 12, QColor(200, 200, 220))

    def _orb(self, p: QPainter, c: QPointF, r: float) -> None:
        t = time.time() - self.t0
        col = ACCENT.get(self.state, ACCENT["idle"])
        if self.state == "listen":
            pulse = 1 + 0.55 * self.smooth + 0.03 * math.sin(t * 3)
        elif self.state == "speak":
            pulse = 1 + 0.08 * math.sin(t * 5) + 0.04 * math.sin(t * 11)
        elif self.state == "think":
            pulse = 1 + 0.04 * math.sin(t * 2)
        else:
            pulse = 1 + 0.02 * math.sin(t * 1.5)
        R = r * pulse
        for i in range(3, 0, -1):  # glow
            g = QRadialGradient(c, R * (1 + i * 0.55))
            g.setColorAt(0, QColor(col.red(), col.green(), col.blue(), 50 // i))
            g.setColorAt(1, QColor(col.red(), col.green(), col.blue(), 0))
            p.setBrush(g)
            p.setPen(Qt.NoPen)
            p.drawEllipse(c, R * (1 + i * 0.55), R * (1 + i * 0.55))
        g = QRadialGradient(QPointF(c.x() - R * 0.3, c.y() - R * 0.35), R * 1.3)
        g.setColorAt(0, QColor(255, 255, 255, 235))
        g.setColorAt(0.35, col.lighter(130))
        g.setColorAt(1, col.darker(220))
        p.setBrush(g)
        p.drawEllipse(c, R, R)
        if self.state == "think":  # orbiting arcs
            pen = QPen(QColor(col.red(), col.green(), col.blue(), 200), max(2.0, r * 0.05))
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            for k in range(3):
                rr = R * (1.25 + 0.18 * k)
                start = int((t * (120 + 60 * k) + k * 120) % 360 * 16)
                p.drawArc(QRectF(c.x() - rr, c.y() - rr, 2 * rr, 2 * rr), start, 70 * 16)
        if self.state == "listen":  # level ring
            p.setPen(QPen(QColor(255, 255, 255, int(60 + 150 * self.smooth)), 2))
            p.setBrush(Qt.NoBrush)
            rr = R * (1.15 + 0.35 * self.smooth)
            p.drawEllipse(c, rr, rr)

    def _text(self, p, text, rect, size, color, italic=False, center=True, align=None) -> float:
        f = QFont()
        f.setPointSize(size)
        f.setItalic(italic)
        p.setFont(f)
        p.setPen(color)
        opt = QTextOption(align if align is not None else (Qt.AlignHCenter if center else Qt.AlignLeft))
        opt.setWrapMode(QTextOption.WordWrap)
        p.drawText(rect, text, opt)
        br = QFontMetrics(f).boundingRect(rect.toRect(), Qt.TextWordWrap | Qt.AlignHCenter, text)
        return rect.top() + br.height()

    def _image(self, p: QPainter, area: QRectF) -> None:
        pix = self.image.scaled(int(area.width()), int(area.height()), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        x = area.left() + (area.width() - pix.width()) / 2
        y = area.top() + (area.height() - pix.height()) / 2
        target = QRectF(x, y, pix.width(), pix.height())
        path = QPainterPath()
        path.addRoundedRect(target, 18, 18)
        p.save()
        p.setClipPath(path)
        p.drawPixmap(target.toRect(), pix)
        p.restore()
        p.setPen(QPen(QColor(255, 255, 255, 40), 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(target, 18, 18)
        if self.caption:
            self._text(p, self.caption, QRectF(x, y + pix.height() + 8, pix.width(), 30), 13, QColor(190, 185, 210))

    def _card(self, p: QPainter, area: QRectF) -> None:
        title, body = self.card
        p.setPen(QPen(QColor(255, 255, 255, 40), 1.5))
        p.setBrush(QColor(255, 255, 255, 18))
        p.drawRoundedRect(area, 22, 22)
        inner = area.adjusted(40, 34, -40, -34)
        y = self._text(p, title, inner, 30, QColor(255, 255, 255), center=False) + 18 if title else inner.top()
        self._text(p, body, QRectF(inner.left(), y, inner.width(), inner.bottom() - y), 20, QColor(225, 222, 240),
                   center=False)


def _fmt(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1e6:.2f}M"
    if n >= 1000:
        return f"{n / 1e3:.1f}K"
    return str(n)


def main() -> None:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    QGuiApplication.setApplicationDisplayName("Batsy")
    win = Overlay()
    bridge = Bridge()
    bridge.message.connect(win.handle)

    def reader():
        for line in sys.stdin:
            try:
                bridge.message.emit(json.loads(line))
            except ValueError:
                pass
        app.quit()  # parent went away

    threading.Thread(target=reader, daemon=True).start()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
