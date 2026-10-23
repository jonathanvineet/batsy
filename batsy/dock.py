"""Batsy's standby badge: the bat in the top-right corner while the overlay is closed.

Its own small process on XWayland (GNOME on Wayland won't let a normal window pin itself to a corner or stay on top;
X11 windows can). Driven by JSON lines on stdin from the daemon (batsy/overlay.py):
  {"cmd": "standby"}                        just the bat
  {"cmd": "away"}                           the overlay is opening from here: swell and fade out
  {"cmd": "rest", "you", "text", "image", "title"} the overlay closed into here: bat back, and a bubble with
                                                    the latest exchange (what you asked, Batsy's reply)
Events out: {"event": "open"} (bat or bubble clicked), {"event": "origin", "x", "y"} (the bat's centre on screen,
so the overlay can grow out of exactly this spot).
"""
import json
import math
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QObject, QPointF, QRect, QRectF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetrics, QGuiApplication, QImage, QPainter,
                           QPainterPath, QPixmap, QRegion, QTextOption)
from PySide6.QtWidgets import QApplication, QWidget

from batsy.config import DATA_DIR

LOGO = DATA_DIR / "assets" / "bat.svg"
FONTS = Path(__file__).resolve().parent / "ui" / "fonts"
SIGNAL = QColor(255, 194, 14)
BONE = QColor(238, 232, 218)
STEEL = QColor(141, 149, 165)
W, H = 420, 300            # window: room for the bubble under the bat
MARGIN = 10                # from the screen's right edge
TOP = 54                   # below the top bar: clear of maximised windows' title-bar buttons
BAT_W = 46                 # bat width in px at rest
BUBBLE_W = 330
BUBBLE_SECONDS = 25
YOU_H = 20                 # the "what you asked" line at the top of the bubble


def emit(ev: dict) -> None:
    try:
        print(json.dumps(ev), flush=True)
    except (BrokenPipeError, OSError):
        pass


def _bat_image(width: int) -> QImage | None:
    """The logo, tinted bat-signal yellow, at a given width (cached by the caller)."""
    if not LOGO.exists():
        return None
    from PySide6.QtSvg import QSvgRenderer

    r = QSvgRenderer(str(LOGO))
    size = r.defaultSize()
    h = max(1, round(width * size.height() / size.width()))
    img = QImage(width, h, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    r.render(p, QRectF(0, 0, width, h))
    p.setCompositionMode(QPainter.CompositionMode_SourceIn)  # keep the shape, paint it yellow
    p.fillRect(img.rect(), SIGNAL)
    p.end()
    return img


class Dock(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.resize(W, H)
        geo = QGuiApplication.primaryScreen().availableGeometry()
        self.move(geo.right() - W - MARGIN + 1, geo.top() + TOP)
        self.bat = _bat_image(BAT_W * 2)  # drawn at half size: crisp on any scaling
        self.center = QPointF(W - 14 - BAT_W / 2, 14 + BAT_W * 0.25)  # the bat's centre in the window
        self.t0 = time.time()
        self.swell = 0.0       # 0 at rest; 1 = swollen and gone (the overlay took over)
        self.bubble = 0.0      # 0 hidden .. 1 fully open
        self.you, self.text, self.title, self.thumb = "", "", "", None
        self.anim = QVariantAnimation(self, duration=280, easingCurve=QEasingCurve.OutCubic)
        self.anim.valueChanged.connect(self._swell_to)
        self.banim = QVariantAnimation(self, duration=320, easingCurve=QEasingCurve.OutBack)
        self.banim.valueChanged.connect(self._bubble_to)
        self.bubble_timer = QTimer(self, singleShot=True, timeout=self.close_bubble)
        self.breath = QTimer(self, interval=50, timeout=self.update)  # the gentle glow at rest
        self.setMouseTracking(True)

    # --- commands ---------------------------------------------------------------------------------------
    def handle(self, m: dict) -> None:
        cmd = m.get("cmd")
        if cmd == "standby":
            self._arrive(bubble=False)
        elif cmd == "away":
            self.bubble_timer.stop()
            self._animate(self.anim, self.swell, 1.0, 260)
            self._animate(self.banim, self.bubble, 0.0, 160)
            QTimer.singleShot(280, self._gone)
        elif cmd == "rest":
            self.you = " ".join((m.get("you") or "").split())
            self.text = " ".join((m.get("text") or "").split())
            self.title = m.get("title") or ""
            self.thumb = None
            if m.get("image"):
                pix = QPixmap(m["image"])
                if not pix.isNull():
                    self.thumb = pix.scaled(132, 132, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            self._arrive(bubble=bool(self.text or self.thumb or self.title))

    def _arrive(self, bubble: bool) -> None:
        self.swell = 1.0 if not self.isVisible() else self.swell
        self.show()
        self.breath.start()
        self._animate(self.anim, self.swell, 0.0, 360)  # settle back in from the overlay's size
        if bubble:
            QTimer.singleShot(260, lambda: self._animate(self.banim, self.bubble, 1.0, 380))
            self.bubble_timer.start(BUBBLE_SECONDS * 1000)
        else:
            self._animate(self.banim, self.bubble, 0.0, 160)
        self._shape()
        g = self.mapToGlobal(self.center.toPoint())
        emit({"event": "origin", "x": g.x(), "y": g.y()})

    def _gone(self) -> None:
        if self.swell > 0.95:
            self.breath.stop()
            self.hide()

    def close_bubble(self) -> None:
        self._animate(self.banim, self.bubble, 0.0, 220)
        QTimer.singleShot(240, self._shape)

    @staticmethod
    def _animate(anim: QVariantAnimation, start: float, end: float, ms: int) -> None:
        anim.stop()
        anim.setDuration(ms)
        anim.setStartValue(float(start))
        anim.setEndValue(float(end))
        anim.start()

    def _swell_to(self, v) -> None:
        self.swell = float(v)
        self.update()

    def _bubble_to(self, v) -> None:
        self.bubble = max(0.0, float(v))
        self._shape()
        self.update()

    # --- geometry: clicks only land on the bat and the bubble; everything else passes through ----------
    def _bat_rect(self) -> QRectF:
        return QRectF(self.center.x() - BAT_W / 2 - 8, self.center.y() - BAT_W / 2 - 4, BAT_W + 16, BAT_W * 0.6 + 8)

    def _bubble_rect(self) -> QRectF:
        f = QFont("Cantarell", 11)
        text_w = BUBBLE_W - 32 - (78 if self.thumb else 0)
        lines = QFontMetrics(f).boundingRect(QRect(0, 0, int(text_w), 2000), Qt.TextWordWrap, self._clip_text())
        h = max(lines.height() + (22 if self.title else 0) + (YOU_H if self.you else 0) + 26, 92 if self.thumb else 0)
        top = self.center.y() + BAT_W * 0.3 + 12
        return QRectF(W - 12 - BUBBLE_W, top, BUBBLE_W, min(h, H - top - 8))

    def _clip_text(self) -> str:
        """The latest reply, from its start (it's one answer, read in order), cut at a word if it's long."""
        t = self.text
        return t if len(t) <= 260 else t[:257].rsplit(" ", 1)[0] + "…"

    def _shape(self) -> None:
        region = QRegion(self._bat_rect().toRect())
        if self.bubble > 0.05:
            region = region.united(QRegion(self._bubble_rect().toRect().adjusted(-4, -4, 4, 4)))
        self.setMask(region)

    # --- input ----------------------------------------------------------------------------------------
    def mousePressEvent(self, e) -> None:
        emit({"event": "open"})

    def enterEvent(self, e) -> None:
        self.bubble_timer.stop()  # reading it: keep it open

    def leaveEvent(self, e) -> None:
        if self.bubble > 0.5:
            self.bubble_timer.start(8000)

    # --- drawing --------------------------------------------------------------------------------------
    def paintEvent(self, _) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        if self.bubble > 0.01:
            self._paint_bubble(p)
        self._paint_bat(p)

    def _paint_bat(self, p: QPainter) -> None:
        s = self.swell
        scale = 1 + 0.9 * s
        alpha = 1 - s
        t = time.time() - self.t0
        breathe = 0.5 + 0.5 * math.sin(t * 1.3)  # slow, barely there
        c = self.center
        glow_r = BAT_W * (0.75 + 0.1 * breathe) * scale
        from PySide6.QtGui import QRadialGradient

        g = QRadialGradient(c, glow_r)
        g.setColorAt(0, QColor(255, 194, 14, int((34 + 22 * breathe) * alpha)))
        g.setColorAt(1, QColor(255, 194, 14, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(c, glow_r, glow_r * 0.62)
        p.setOpacity(alpha * (0.88 + 0.12 * breathe))
        if self.bat is not None:
            w = BAT_W * scale
            h = w * self.bat.height() / self.bat.width()
            p.drawImage(QRectF(c.x() - w / 2, c.y() - h / 2, w, h), self.bat)
        else:  # no logo file: a quiet yellow dot
            p.setBrush(SIGNAL)
            p.drawEllipse(c, 6 * scale, 6 * scale)
        p.setOpacity(1.0)

    def _paint_bubble(self, p: QPainter) -> None:
        r = self._bubble_rect()
        k = self.bubble
        p.save()
        # unfold from the bat: scale about the bubble's top-right corner, which sits under the bat
        p.translate(r.right(), r.top())
        p.scale(0.6 + 0.4 * min(k, 1.05), 0.6 + 0.4 * min(k, 1.05))
        p.translate(-r.right(), -r.top())
        p.setOpacity(max(0.0, min(1.0, k)))
        path = QPainterPath()
        path.addRoundedRect(r, 14, 14)
        tail = QPainterPath()  # a little tail pointing up at the bat
        tx = self.center.x()
        tail.moveTo(tx - 8, r.top() + 1)
        tail.lineTo(tx, r.top() - 8)
        tail.lineTo(tx + 8, r.top() + 1)
        path = path.united(tail)
        p.setPen(QColor(255, 194, 14, 70))
        p.setBrush(QColor(8, 9, 12, 236))
        p.drawPath(path)
        inner = r.adjusted(16, 13, -16, -13)
        if self.thumb is not None:
            box = QRectF(inner.left(), inner.top(), 66, 66)
            clip = QPainterPath()
            clip.addRoundedRect(box, 8, 8)
            p.save()
            p.setClipPath(clip)
            p.drawPixmap(box.toRect(), self.thumb)
            p.restore()
            inner.setLeft(inner.left() + 78)
        y = inner.top()
        if self.you:  # what you asked, one line, so the reply under it makes sense on its own
            f = QFont("Cantarell", 10)
            f.setItalic(True)
            p.setFont(f)
            p.setPen(STEEL)
            said = QFontMetrics(f).elidedText(f"“{self.you}”", Qt.ElideRight, int(inner.width()))
            p.drawText(QRectF(inner.left(), y, inner.width(), YOU_H - 4), Qt.AlignLeft | Qt.AlignVCenter, said)
            y += YOU_H
        if self.title:
            f = QFont("Barlow Condensed", 13)
            f.setWeight(QFont.DemiBold)
            p.setFont(f)
            p.setPen(SIGNAL)
            p.drawText(QRectF(inner.left(), y, inner.width(), 20), Qt.AlignLeft | Qt.AlignVCenter, self.title)
            y += 22
        p.setFont(QFont("Cantarell", 11))
        p.setPen(BONE)
        opt = QTextOption(Qt.AlignLeft)
        opt.setWrapMode(QTextOption.WordWrap)
        p.drawText(QRectF(inner.left(), y, inner.width(), inner.bottom() - y), self._clip_text(), opt)
        p.restore()


class Reader(QObject):
    message = Signal(dict)


def main() -> None:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    for f in FONTS.glob("*.woff2"):
        QFontDatabase.addApplicationFont(str(f))
    dock = Dock()
    reader = Reader()

    def handle(m: dict) -> None:
        if m.get("cmd") == "__eof__":
            app.quit()
            return
        try:
            dock.handle(m)
        except Exception as e:  # never take the badge down
            print(f"dock: {e}", file=sys.stderr, flush=True)

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
