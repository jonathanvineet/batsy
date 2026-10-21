"""Batsy's full-screen overlay (a separate Qt process, driven by JSON lines on stdin).

Commands in:  {"cmd": "listen"} {"cmd": "level", "v": 0..1} {"cmd": "partial", "text"} {"cmd": "heard", "text"}
              {"cmd": "think"} {"cmd": "say", "text"} {"cmd": "image", "path", "caption"}
              {"cmd": "card", "title", "text"} {"cmd": "done", "linger": s} {"cmd": "hide"}
              {"cmd": "saver", "stats": {...}, "next": "..."}  (screensaver; any mouse/key input ends it)
              {"cmd": "camera", "on": bool, "path"}  (live camera feed from batsy.camera_view's frame file)
              {"cmd": "model3d"}  (host the 3D viewer in this window; it then takes commands on its own socket)
              {"cmd": "status", "text"}  (what Batsy is doing right now: "Asking Claude", "Running skill"...)
Events out:   {"event": "dismissed"}  (Esc / click / Space)
              {"event": "measured", "mm"}  (3D viewer click-to-measure)
"""
import json
import math
import os
import random
import sys
import threading
import time

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QGuiApplication, QLinearGradient, QPainter, QPainterPath,
                           QPen, QPixmap, QPolygonF, QRadialGradient, QTextOption)
from PySide6.QtWidgets import QApplication, QWidget

from batsy.camera_view import FLASH_PATH

# Gotham at night: near-black, steel greys, bat-signal yellow.
YELLOW = QColor(255, 194, 14)
STEEL = QColor(150, 158, 174)
TEXT = QColor(234, 236, 242)
INK = QColor(5, 6, 9)
ACCENT = {"listen": QColor(255, 210, 50), "think": QColor(255, 168, 0), "speak": YELLOW, "idle": QColor(110, 118, 134)}
HEAD_FONT = "Nimbus Sans Narrow"  # condensed caps for labels and titles
BODY_FONT = "Cantarell"
STATE_LABEL = {"listen": "Listening", "think": "Thinking", "speak": "Speaking", "idle": ""}


def _bat_path() -> QPainterPath:
    """A bat in flight, wingspan 2 units, centred on (0, 0); y grows downwards. Right half, mirrored."""
    def curve(a, ctrl, b, n=6):  # sampled quadratic: the scalloped trailing edge of the wing
        return [((1 - t) ** 2 * a[0] + 2 * (1 - t) * t * ctrl[0] + t * t * b[0],
                 (1 - t) ** 2 * a[1] + 2 * (1 - t) * t * ctrl[1] + t * t * b[1]) for t in (i / n for i in range(1, n + 1))]

    right = [(0.0, -0.17), (0.06, -0.31), (0.10, -0.15), (0.22, -0.19), (0.48, -0.30), (0.78, -0.31), (1.0, -0.17),
             (0.93, -0.02), (0.88, 0.12)]
    for ctrl, end in [((0.80, 0.0), (0.70, 0.04)), ((0.64, 0.10), (0.55, 0.17)), ((0.48, 0.05), (0.38, 0.12)),
                      ((0.30, 0.20), (0.22, 0.28)), ((0.14, 0.15), (0.0, 0.42))]:
        right += curve(right[-1], ctrl, end)
    outline = right + [(-x, y) for x, y in reversed(right[1:-1])]
    return _polygon_path(outline)


def _polygon_path(points) -> QPainterPath:
    path = QPainterPath()
    path.addPolygon(QPolygonF([QPointF(x, y) for x, y in points]))
    path.closeSubpath()
    return path


BAT = _bat_path()


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
        self.camera_path: str | None = None  # live camera feed while set
        self.camera_mtime = 0.0
        self.cam_timer = QTimer(self, interval=100, timeout=self._camera_frame)
        self.status = ""  # what Batsy is doing right now (shown with a seconds counter)
        self.status_t0 = time.time()
        rng = random.Random(7)  # a fixed Gotham skyline: (x, width, height) as fractions of the screen
        self.skyline, x = [], 0.0
        while x < 1.0:
            bw = rng.uniform(0.025, 0.07)
            self.skyline.append((x, bw, rng.uniform(0.04, 0.15) * (1.5 if rng.random() < 0.12 else 1)))
            x += bw + rng.uniform(0.0, 0.006)
        self.windows = [(rng.random(), rng.random(), rng.random()) for _ in range(160)]
        self.viewer = None  # batsy.viewer3d.Viewer drawing into this window, created on first use
        self.model_on = False
        # 3D unused for a while: quit, so Batsy restarts the overlay without VTK's few hundred MB.
        self.free_timer = QTimer(self, singleShot=True, interval=5 * 60_000, timeout=self._free_3d)
        self.hide_timer = QTimer(self, singleShot=True, timeout=self._dismiss)
        self.anim = QTimer(self, interval=33, timeout=self._tick)

    # --- commands ---------------------------------------------------------
    def handle(self, m: dict) -> None:
        cmd = m.get("cmd")
        if cmd == "saver":
            first = self.saver is None
            self.saver, self.saver_next = m, m.get("next", "")
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
        if cmd == "status":
            text = m.get("text", "")
            if text != self.status:
                self.status, self.status_t0 = text, time.time()
            self._appear()
        elif cmd in ("say", "done", "hide") or (cmd == "listen" and m.get("fresh")):
            self.status = ""
        if cmd == "listen":
            self.hide_timer.stop()
            if m.get("fresh"):
                self.partial = self.heard = self.answer = ""
                self.card = None
                if self.camera_path is None:
                    self.image, self.caption = None, ""
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
        elif cmd == "camera":
            if m.get("on"):
                self.camera_path, self.camera_mtime = m.get("path"), 0.0
                self.card = None
                self.cam_timer.start()
                self._camera_frame()
                self._appear()
            else:
                self.camera_path = None
                self.cam_timer.stop()
                self.image, self.caption = None, ""
            self._place_3d()
        elif cmd == "model3d" and self.viewer is None:
            import pyvista as pv

            from batsy.viewer3d import Viewer

            pv.global_theme.font.family = "arial"
            self.viewer = Viewer(host=self)
            self.viewer.plotter.interactor.hide()
        elif cmd == "done":
            self.state = "idle"
            self.level = 0.0
            busy = self.camera_path or self.model_on  # live views stay up until closed or idle a while
            linger = max(float(m.get("linger", 6)), 180) if busy else float(m.get("linger", 6))
            self.hide_timer.start(int(1000 * linger))
        elif cmd == "hide" and self.camera_path is None:  # the live camera stays up until it's closed
            self._dismiss(notify=False)
        self.update()

    # --- hosted 3D viewer ------------------------------------------------
    def show_3d(self, on: bool) -> None:
        self.model_on = on
        if on:
            self.free_timer.stop()
            self.image, self.card, self.caption = None, None, ""
            self._appear()
        else:
            self.free_timer.start(30_000)  # closed on purpose: free it soon
        self._place_3d()
        self.update()

    def _place_3d(self) -> None:
        if self.viewer is None:
            return
        widget = self.viewer.plotter.interactor
        if self.model_on and self.camera_path is None:  # the live camera takes the screen while it's on
            w, h = self.width(), self.height()
            widget.setGeometry(int(w * 0.17), int(h * 0.06), int(w * 0.71), int(h * 0.72))  # clear of the orb
            widget.show()
        else:
            widget.hide()

    def resizeEvent(self, e):
        self._place_3d()

    def _free_3d(self) -> None:
        if self.viewer is None:
            return
        if self.isVisible():  # never quit mid-conversation; try again later
            self.free_timer.start(5 * 60_000)
            return
        print(json.dumps({"event": "viewer_closed"}), flush=True)
        QApplication.quit()

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
        if self.viewer is not None:
            self.free_timer.start()
        if notify:
            print(json.dumps({"event": "dismissed"}), flush=True)

    def keyPressEvent(self, e):
        if self.saver is not None or e.key() in (Qt.Key_Escape, Qt.Key_Space, Qt.Key_Q):
            self._dismiss()

    def mousePressEvent(self, e):
        self._dismiss()

    def _camera_frame(self) -> None:
        try:
            mtime = os.stat(self.camera_path).st_mtime
        except (OSError, TypeError):
            return
        looking = FLASH_PATH.exists() and time.time() - FLASH_PATH.stat().st_mtime < 1.5
        self.caption = "Looking…" if looking else "Live camera · ask me about it"
        if mtime != self.camera_mtime:
            pix = QPixmap(self.camera_path)
            if not pix.isNull():
                self.image, self.camera_mtime = pix, mtime
        self.update()

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
        self._gotham(p, w, h)
        p.setOpacity(self.fade)

        model = self.model_on and not self.camera_path
        has_media = self.image is not None or self.card is not None or model
        sig_c = QPointF(w * 0.08 + 30, h * 0.1 + 24) if has_media else QPointF(w / 2, h * 0.36)
        sig_r = 42 if has_media else min(w, h) * 0.12
        self._signal(p, sig_c, sig_r)
        if has_media:  # status beside the small emblem
            self._status(p, QPointF(sig_c.x() - sig_r - 10, sig_c.y() + sig_r + 18), center=False)
        else:
            self._status(p, QPointF(w / 2, sig_c.y() + sig_r * 1.45 + 18))

        text_w = w * 0.70
        y = (sig_c.y() + sig_r * 1.45 + 72) if not has_media else h * 0.80
        if not has_media:
            line = self.heard or self.partial
            if line:
                col = STEEL if self.heard else QColor(STEEL.red(), STEEL.green(), STEEL.blue(), 150)
                y = self._text(p, f"“{line}”" if self.heard else line, QRectF((w - text_w) / 2, y, text_w, 200),
                               20, col, italic=not self.heard) + 22
            if self.answer:
                self._text(p, self.answer, QRectF((w - text_w) / 2, y, text_w, h - y - 40), 30, TEXT)
        else:
            top = h * 0.07
            if model:  # frame around the 3D view (the view itself is a child widget drawn on top)
                r = QRectF(w * 0.17, h * 0.06, w * 0.71, h * 0.72).adjusted(-3, -3, 3, 3)
                p.setPen(QPen(QColor(YELLOW.red(), YELLOW.green(), YELLOW.blue(), 110), 1.5))
                p.setBrush(Qt.NoBrush)
                p.drawRect(r)
                self._text(p, "SAY: ROTATE IT · TOP VIEW · ZOOM IN · DIMENSIONS · CLOSE THE MODEL",
                           QRectF(r.left(), r.bottom() + 6, r.width(), 20), 10, STEEL, family=HEAD_FONT,
                           spacing=2.0)
            elif self.image is not None and self.card is not None:  # side by side: picture left, card right
                self._image(p, QRectF(w * 0.06, top, w * 0.52, h * 0.68))
                self._card(p, QRectF(w * 0.61, top + h * 0.04, w * 0.33, h * 0.60))
            elif self.image is not None:
                self._image(p, QRectF(w * 0.18, top, w * 0.64, h * 0.68))
            elif self.card is not None:
                self._card(p, QRectF(w * 0.18, top, w * 0.64, h * 0.68))
            sub = self.answer or self.heard or self.partial
            if sub:
                self._text(p, sub, QRectF(w * 0.12, h * 0.83, w * 0.76, h * 0.15), 22, TEXT, center=True)
        p.setOpacity(0.5 * self.fade)
        self._text(p, "ESC TO CLOSE", QRectF(0, h - 30, w - 24, 20), 10, STEEL, align=Qt.AlignRight,
                   family=HEAD_FONT, spacing=2.0)

    def _gotham(self, p: QPainter, w: int, h: int) -> None:
        """Night sky over a dark skyline, with a searchlight beam while Batsy is busy."""
        a = self.fade
        sky = QLinearGradient(0, 0, 0, h)
        sky.setColorAt(0, QColor(10, 12, 18, int(240 * a)))
        sky.setColorAt(0.7, QColor(5, 6, 9, int(240 * a)))
        sky.setColorAt(1, QColor(2, 2, 4, int(245 * a)))
        p.fillRect(self.rect(), sky)
        t = time.time() - self.t0
        if self.state in ("think", "listen", "speak"):  # searchlight from the rooftops up to the signal
            sway = 0.04 * math.sin(t * (1.6 if self.state == "think" else 0.5))
            base = QPointF(w * (0.62 + sway), h)
            tip_l, tip_r = QPointF(w * (0.38 + sway * 2), 0), QPointF(w * (0.62 + sway * 2), 0)
            beam = QLinearGradient(base, QPointF(w / 2, 0))
            beam.setColorAt(0, QColor(255, 214, 90, int(40 * a)))
            beam.setColorAt(1, QColor(255, 214, 90, 0))
            p.setPen(Qt.NoPen)
            p.setBrush(beam)
            p.drawPolygon(QPolygonF([QPointF(base.x() - w * 0.01, h), tip_l, tip_r, QPointF(base.x() + w * 0.01, h)]))
        p.setPen(Qt.NoPen)
        for i, (x, bw, bh) in enumerate(self.skyline):  # buildings
            p.setBrush(QColor(12, 14, 20, int(255 * a)))
            p.drawRect(QRectF(x * w, h * (1 - bh), bw * w, h * bh + 1))
        for wx, wy, lit in self.windows:  # a few lit windows
            x = wx * w
            b = next(((bx, bw, bh) for bx, bw, bh in self.skyline if bx * w <= x < (bx + bw) * w), None)
            if b is None or lit < 0.55:
                continue
            y = h * (1 - b[2] * (0.1 + 0.85 * wy))
            p.setBrush(QColor(255, 200, 80, int((22 + 40 * (lit - 0.55)) * a)))
            p.drawRect(QRectF(x, y, 3, 4))

    def _status(self, p: QPainter, at: QPointF, center: bool = True) -> None:
        """'ASKING CLAUDE · 12s' with a spinner: always visible, so a long wait is never a mystery."""
        label = self.status or STATE_LABEL.get(self.state, "")
        if not label:
            return
        busy = bool(self.status) or self.state == "think"
        secs = int(time.time() - self.status_t0) if self.status else 0
        text = label.upper() + (f"  ·  {secs}s" if busy and secs >= 2 else "")
        f = QFont(HEAD_FONT, 13)
        f.setBold(True)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 2.2)
        tw = QFontMetrics(f).horizontalAdvance(text)
        pad, spin_w, hgt = 16, (26 if busy else 12), 34
        width = tw + pad * 2 + spin_w
        x = at.x() - width / 2 if center else at.x()
        r = QRectF(x, at.y(), width, hgt)
        col = ACCENT.get(self.state, YELLOW) if not self.status else YELLOW
        p.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 150), 1.2))
        p.setBrush(QColor(10, 11, 15, 230))
        p.drawRoundedRect(r, hgt / 2, hgt / 2)
        t = time.time() - self.t0
        cy = r.center().y()
        if busy:  # three bars rising in turn
            for k in range(3):
                hh = 6 + 8 * (0.5 + 0.5 * math.sin(t * 7 - k * 0.9))
                p.setPen(Qt.NoPen)
                p.setBrush(col)
                p.drawRoundedRect(QRectF(r.left() + pad + k * 6, cy - hh / 2, 3.5, hh), 1.5, 1.5)
        else:  # a steady dot
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawEllipse(QPointF(r.left() + pad + 3, cy), 3.5, 3.5)
        p.setFont(f)
        p.setPen(TEXT)
        p.drawText(QRectF(r.left() + pad + spin_w, r.top(), tw + 4, hgt), Qt.AlignVCenter | Qt.AlignLeft, text)

    def _panel(self, p: QPainter, r: QRectF, title: str) -> QRectF:
        p.setPen(QPen(QColor(255, 255, 255, 28), 1.2))
        p.setBrush(QColor(255, 255, 255, 11))
        p.drawRoundedRect(r, 18, 18)
        inner = r.adjusted(22, 16, -22, -14)
        self._text(p, title.upper(), QRectF(inner.left(), inner.top(), inner.width(), 20), 10,
                   QColor(170, 160, 205), center=False)
        return QRectF(inner.left(), inner.top() + 28, inner.width(), inner.height() - 28)

    # --- screensaver: animated background + widgets from Batsy's spec ------------------------------------
    def _background(self, p: QPainter, w: int, h: int, style: str, accent: QColor, t: float) -> None:
        p.fillRect(self.rect(), QColor(3, 2, 8, int(255 * self.fade)))
        if style == "aurora":
            for i, (fx, fy, size) in enumerate([(0.2, 0.3, 0.7), (0.75, 0.25, 0.6), (0.5, 0.8, 0.8), (0.9, 0.7, 0.5)]):
                cx = w * (fx + 0.12 * math.sin(t / (17 + i * 5) + i))
                cy = h * (fy + 0.10 * math.cos(t / (23 + i * 4) + i * 2))
                col = accent if i % 2 == 0 else QColor.fromHsv((accent.hue() + 60 + i * 25) % 360, 200, 255)
                g = QRadialGradient(QPointF(cx, cy), max(w, h) * size * 0.5)
                g.setColorAt(0, QColor(col.red(), col.green(), col.blue(), 55))
                g.setColorAt(1, QColor(col.red(), col.green(), col.blue(), 0))
                p.setPen(Qt.NoPen)
                p.setBrush(g)
                p.drawRect(self.rect())
        elif style == "stars":
            if not hasattr(self, "_stars"):
                import random

                rnd = random.Random(7)
                self._stars = [(rnd.random(), rnd.random(), rnd.uniform(0.4, 2.2), rnd.uniform(0, 6.28), rnd.uniform(0.002, 0.01))
                               for _ in range(220)]
            p.setPen(Qt.NoPen)
            for x, y, r, ph, sp in self._stars:
                a = int(120 + 120 * math.sin(t * 1.5 + ph))
                p.setBrush(QColor(255, 255, 255, max(30, a)))
                p.drawEllipse(QPointF(((x + t * sp) % 1) * w, y * h), r, r)
        elif style == "waves":
            for k in range(5):
                path = QPainterPath()
                base = h * (0.62 + k * 0.07)
                path.moveTo(0, base)
                for x in range(0, w + 20, 20):
                    path.lineTo(x, base + 22 * math.sin(x / 140 + t * (0.4 + k * 0.12) + k))
                p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 70 - k * 10), 2))
                p.setBrush(Qt.NoBrush)
                p.drawPath(path)
        elif style == "orb":
            self._signal(p, QPointF(w * 0.5 + 40 * math.sin(t / 30), h * 0.5 + 30 * math.cos(t / 40)), min(w, h) * 0.18)

    def _glow(self, p: QPainter, r: QRectF, accent: QColor, t: float) -> None:
        a = int(90 + 80 * math.sin(t * 3))
        p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), a), 3))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r.adjusted(-3, -3, 3, 3), 20, 20)

    def _widget(self, p: QPainter, r: QRectF, wd: dict, d: dict, accent: QColor, t: float) -> None:
        kind = wd["type"]
        if kind == "photo":
            path = d.get("path")
            if path:
                cache = getattr(self, "_pix", {})
                if path not in cache:
                    cache = {path: QPixmap(path)}
                    self._pix = cache
                pix = cache[path].scaled(int(r.width()), int(r.height()), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                clip = QPainterPath()
                clip.addRoundedRect(r, 18, 18)
                p.save()
                p.setClipPath(clip)
                p.drawPixmap(int(r.left() - (pix.width() - r.width()) / 2), int(r.top() - (pix.height() - r.height()) / 2), pix)
                p.restore()
            return
        inner = self._panel(p, r, wd.get("title") or "")
        if wd.get("highlight"):
            self._glow(p, r, accent, t)
        white, soft = QColor(255, 255, 255), QColor(200, 195, 225)
        if d.get("error"):
            self._text(p, d["error"], QRectF(inner.left(), inner.top(), inner.width(), 40), 12, QColor(255, 140, 140), center=False)
            return
        if kind == "claude_budget":
            half = (inner.width() - 20) / 2
            for i, (label, key) in enumerate((("Session", "session"), ("This week", "week"))):
                self._mini_bar(p, QRectF(inner.left() + i * (half + 20), inner.top(), half, inner.height()), label,
                               d.get(key, {}), accent)
        elif kind == "local_tokens":
            self._text(p, f"{_fmt(d.get('tokens', 0))} tokens", QRectF(inner.left(), inner.top(), inner.width(), 40), 24, white, center=False)
            self._text(p, f"free · {d.get('requests', 0)} requests", QRectF(inner.left(), inner.top() + 46, inner.width(), 22), 12, soft, center=False)
        elif kind in ("running", "waiting", "upcoming", "agenda", "project", "command"):
            rows = d.get("rows") if "rows" in d else d.get("lines", [])
            empty = {"running": "Nothing running", "waiting": "Nothing to answer", "upcoming": "Nothing scheduled",
                     "agenda": "Nothing planned", "project": "No updates yet", "command": "(no output)"}[kind]
            self._rows(p, inner, rows, empty, accent, mono=kind == "command")
        elif kind == "system":
            items = [("CPU", d.get("cpu")), ("RAM", d.get("ram")), ("Temp", d.get("temp")), ("Battery", d.get("battery"))]
            y = inner.top()
            for label, val in items:
                if val is None:
                    continue
                self._text(p, label, QRectF(inner.left(), y, 80, 20), 12, soft, center=False)
                track = QRectF(inner.left() + 80, y + 6, inner.width() - 140, 8)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, 25))
                p.drawRoundedRect(track, 4, 4)
                frac = min(1.0, val / 100) if label != "Temp" else min(1.0, val / 100)
                p.setBrush(accent if frac < 0.8 or label == "Battery" else QColor(255, 90, 90))
                p.drawRoundedRect(QRectF(track.left(), track.top(), max(4.0, track.width() * frac), 8), 4, 4)
                unit = "°C" if label == "Temp" else "%"
                self._text(p, f"{val:.0f}{unit}", QRectF(track.right() + 8, y, 56, 20), 12, white, center=False)
                y += 30
        elif kind == "now_playing":
            p.setPen(Qt.NoPen)
            for i in range(5):  # little equaliser
                hgt = 6 + 18 * abs(math.sin(t * (3 + i * 0.7) + i))
                p.setBrush(accent)
                p.drawRoundedRect(QRectF(inner.left() + i * 9, inner.top() + 26 - hgt, 6, hgt), 2, 2)
            self._text(p, d.get("text", ""), QRectF(inner.left() + 56, inner.top(), inner.width() - 56, inner.height()), 14, white, center=False)
        elif kind == "countdown":
            self._text(p, d.get("big", ""), QRectF(inner.left(), inner.top(), inner.width(), 48), 30, white, center=False)
            self._text(p, d.get("sub", ""), QRectF(inner.left(), inner.top() + 52, inner.width(), 40), 12, soft, center=False)
        elif kind == "weather":
            self._text(p, f"{d.get('temp', '–')}°", QRectF(inner.left(), inner.top(), inner.width(), 50), 34, white, center=False)
            hum = f" · {d['humidity']}% humidity" if d.get("humidity") is not None else ""
            self._text(p, f"{d.get('desc', '')} in {d.get('place', '')}{hum}", QRectF(inner.left(), inner.top() + 54, inner.width(), 40), 12, soft, center=False)
        elif kind in ("note", "quote"):
            self._text(p, d.get("text", ""), inner, 17 if kind == "quote" else 15, white, italic=kind == "quote", center=kind == "quote")

    def _mini_bar(self, p, r, label, b, accent):
        used, limit, pct = b.get("used", 0), b.get("limit") or 0, min(100.0, b.get("pct") or 0.0)
        self._text(p, label, QRectF(r.left(), r.top(), r.width(), 18), 11, QColor(170, 160, 205), center=False)
        self._text(p, f"{_fmt(used)} / {_fmt(limit)}" if limit else _fmt(used), QRectF(r.left(), r.top() + 18, r.width(), 32), 20,
                   QColor(255, 255, 255), center=False)
        col = accent if pct < 70 else QColor(255, 170, 70) if pct < 90 else QColor(255, 90, 90)
        track = QRectF(r.left(), r.top() + 56, r.width(), 8)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 25))
        p.drawRoundedRect(track, 4, 4)
        if limit:
            p.setBrush(col)
            p.drawRoundedRect(QRectF(track.left(), track.top(), max(5.0, track.width() * pct / 100), 8), 4, 4)
        reset = b.get("resets_at")
        fmt = "%-I:%M %p" if label == "Session" else "%a %-I %p"
        sub = f"{pct:.0f}% · resets {time.strftime(fmt, time.localtime(reset))}" if reset and limit else "not used yet"
        self._text(p, sub, QRectF(r.left(), r.top() + 70, r.width(), 20), 11, QColor(200, 195, 225), center=False)

    def _rows(self, p, inner, rows, empty, accent, mono=False):
        if not rows:
            self._text(p, empty, QRectF(inner.left(), inner.top(), inner.width(), 24), 13, QColor(150, 145, 175), center=False)
            return
        y = inner.top()
        for line in rows[:7]:
            if y > inner.bottom() - 20:
                break
            if not mono:
                p.setPen(Qt.NoPen)
                p.setBrush(accent)
                p.drawEllipse(QPointF(inner.left() + 4, y + 11), 3, 3)
            self._text(p, line, QRectF(inner.left() + (0 if mono else 16), y, inner.width() - 16, 24), 12 if mono else 13,
                       QColor(235, 232, 250), center=False)
            y += 26

    def _paint_saver(self, p: QPainter, w: int, h: int) -> None:
        t = time.time() - self.t0
        sv = self.saver or {}
        theme = sv.get("theme", {})
        accent = QColor(theme.get("accent", "#d646ff"))
        self._background(p, w, h, theme.get("style", "aurora"), accent, t)
        p.setOpacity(self.fade * (0.6 if theme.get("dim") else 1.0))
        dx, dy = 24 * math.sin(t / 47), 16 * math.sin(t / 61)  # slow drift: no burn-in
        widgets, data = sv.get("widgets", []), sv.get("data", {})
        y = h * 0.06 + dy
        margin, gap = w * 0.06, 22
        cols = 3
        col_w = (w - 2 * margin - (cols - 1) * gap) / cols
        rest = []
        for i, wd in enumerate(widgets):
            if wd["type"] == "clock":
                self._text(p, time.strftime("%H:%M"), QRectF(dx, y, w, 110), 70, QColor(240, 236, 255))
                date = time.strftime("%A, %B ") + str(int(time.strftime("%d")))
                self._text(p, date, QRectF(dx, y + 104, w, 30), 17, QColor(170, 160, 205))
                y += 160
            else:
                rest.append((wd, data.get(str(i), {})))
        # flow the other widgets into rows of 3 columns
        rows, row, used = [], [], 0
        for wd, d in rest:
            span = max(1, min(cols, int(wd.get("span", 1))))
            if used + span > cols:
                rows.append(row)
                row, used = [], 0
            row.append((wd, d, span))
            used += span
        if row:
            rows.append(row)
        avail = h * 0.93 - y
        row_h = min(240, max(110, (avail - gap * (len(rows) - 1)) / max(1, len(rows))))
        for row in rows:
            x = margin + dx
            for wd, d, span in row:
                width = col_w * span + gap * (span - 1)
                self._widget(p, QRectF(x, y, width, row_h), wd, d, accent, t)
                x += width + gap
            y += row_h + gap
        p.setOpacity(0.4 * self.fade)
        self._text(p, "Say “Batsy” anytime", QRectF(dx, h - 40, w, 22), 12, QColor(200, 200, 220))

    def _signal(self, p: QPainter, c: QPointF, r: float) -> None:
        """The bat-signal: a yellow disc with a bat, pulsing to your voice, sweeping while thinking."""
        t = time.time() - self.t0
        col = ACCENT.get(self.state, ACCENT["idle"])
        if self.state == "listen":
            pulse = 1 + 0.35 * self.smooth + 0.02 * math.sin(t * 3)
        elif self.state == "speak":
            pulse = 1 + 0.03 * math.sin(t * 6) + 0.02 * math.sin(t * 13)
        else:
            pulse = 1 + 0.015 * math.sin(t * 1.5)
        R = r * pulse
        p.setPen(Qt.NoPen)
        for i in range(3, 0, -1):  # halo
            g = QRadialGradient(c, R * (1 + i * 0.6))
            g.setColorAt(0, QColor(col.red(), col.green(), col.blue(), (70 if self.state != "idle" else 25) // i))
            g.setColorAt(1, QColor(col.red(), col.green(), col.blue(), 0))
            p.setBrush(g)
            p.drawEllipse(c, R * (1 + i * 0.6), R * (1 + i * 0.6))
        disc = QRadialGradient(QPointF(c.x(), c.y() - R * 0.15), R * 1.05)
        disc.setColorAt(0, col.lighter(135))
        disc.setColorAt(0.75, col)
        disc.setColorAt(1, col.darker(170))
        p.setBrush(disc)
        p.drawEllipse(c, R, R)
        p.save()  # the bat, slightly wider than tall like the real signal
        p.translate(c)
        p.scale(R * 0.82, R * 0.82)
        p.setBrush(QColor(8, 8, 10))
        p.drawPath(BAT)
        p.restore()
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(0, 0, 0, 160), max(1.5, R * 0.04)))
        p.drawEllipse(c, R, R)
        if self.state == "think":  # a searchlight sweeping around the signal
            pen = QPen(QColor(col.red(), col.green(), col.blue(), 220), max(2.0, r * 0.05))
            pen.setCapStyle(Qt.FlatCap)
            p.setPen(pen)
            for k in range(2):
                rr = R * (1.22 + 0.16 * k)
                start = int(((t * (160 - 50 * k)) + k * 180) % 360 * 16)
                p.drawArc(QRectF(c.x() - rr, c.y() - rr, 2 * rr, 2 * rr), start, 55 * 16)
        if self.state == "listen":  # level ring
            p.setPen(QPen(QColor(col.red(), col.green(), col.blue(), int(70 + 170 * self.smooth)), 2))
            rr = R * (1.12 + 0.3 * self.smooth)
            p.drawEllipse(c, rr, rr)

    def _text(self, p, text, rect, size, color, italic=False, center=True, align=None, family=BODY_FONT,
              bold=False, spacing=0.0) -> float:
        f = QFont(family)
        f.setPointSize(size)
        f.setItalic(italic)
        f.setBold(bold)
        if spacing:
            f.setLetterSpacing(QFont.AbsoluteSpacing, spacing)
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
        path.addRoundedRect(target, 6, 6)
        p.save()
        p.setClipPath(path)
        p.drawPixmap(target.toRect(), pix)
        p.restore()
        snapping = self.camera_path is not None and self.caption.startswith("Looking")
        p.setPen(QPen(YELLOW, 6) if snapping else QPen(QColor(YELLOW.red(), YELLOW.green(), YELLOW.blue(), 110), 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(target, 6, 6)
        if self.caption:
            self._text(p, self.caption.upper(), QRectF(x, y + pix.height() + 10, pix.width(), 24), 11, STEEL,
                       family=HEAD_FONT, spacing=2.0)

    def _card(self, p: QPainter, area: QRectF) -> None:
        title, body = self.card
        p.setPen(QPen(QColor(255, 255, 255, 22), 1))
        p.setBrush(QColor(12, 14, 19, 235))
        p.drawRoundedRect(area, 6, 6)
        p.setPen(Qt.NoPen)
        p.setBrush(YELLOW)
        p.drawRect(QRectF(area.left(), area.top() + 18, 5, area.height() - 36))
        inner = area.adjusted(40, 30, -40, -30)
        y = (self._text(p, title.upper(), inner, 26, YELLOW, center=False, family=HEAD_FONT, bold=True, spacing=1.5)
             + 16) if title else inner.top()
        self._text(p, body, QRectF(inner.left(), y, inner.width(), inner.bottom() - y), 19, TEXT, center=False)


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
