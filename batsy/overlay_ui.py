"""Batsy's classic full-screen overlay: plain Qt painting, used when the GPU overlay (overlay_web.py) can't run.
A separate process driven by JSON lines on stdin.

Commands in:  {"cmd": "listen"} {"cmd": "level", "v": 0..1} {"cmd": "partial", "text"} {"cmd": "heard", "text"}
              {"cmd": "think"} {"cmd": "say", "text"} {"cmd": "image", "path", "caption"}
              {"cmd": "card", "title", "text"} {"cmd": "done", "linger": s} {"cmd": "hide"}
              {"cmd": "saver", "stats": {...}, "next": "..."}  (screensaver; any mouse/key input ends it)
              {"cmd": "camera", "on": bool, "path"}  (live camera feed from batsy.camera_view's frame file)
              {"cmd": "status", "text"}  (what Batsy is doing right now: "Asking Claude", "Running skill"...)
Events out:   {"event": "dismissed"}  (Esc / click / Space)
"""
import json
import math
import os
import random
import re
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


def _tail(text: str, n: int) -> str:
    """The end of a long reply, starting at a sentence (or word) boundary."""
    if len(text) <= n:
        return text
    cut = text[-n:]
    m = re.search(r"[.!?]\s+", cut)
    return "…" + (cut[m.end():] if m and m.end() < n * 0.6 else cut.split(" ", 1)[-1])


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
        self.answer = ""            # Batsy's reply in this exchange only
        self.prev = ("", "")        # the exchange before it (you, Batsy), drawn small above
        self.turn_open = False
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
        rng = random.Random(7)  # drifting particles: (x, y, size, phase, speed)
        self.dots = [(rng.random(), rng.random(), rng.uniform(0.8, 2.4), rng.uniform(0, 6.28), rng.uniform(0.002, 0.008))
                     for _ in range(180)]
        self._scaled: dict = {}  # (id, w, h) -> scaled pixmap: scaling every frame was the main CPU cost
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
                self.prev, self.turn_open = ("", ""), False
                self.card = None
                if self.camera_path is None:
                    self.image, self.caption = None, ""
            self.state = "listen"
            self._appear()
        elif cmd == "level":
            self.level = max(0.0, min(1.0, float(m.get("v", 0))))
        elif cmd == "partial":
            self.partial = m.get("text", "")
        elif cmd == "heard":  # a new exchange: the last one moves up, small
            if self.heard or self.answer:
                self.prev = (self.heard, self.answer)
            self.heard, self.partial, self.answer, self.turn_open = m.get("text", ""), "", "", True
        elif cmd == "think":
            self.state = "think"
            self._appear()
        elif cmd == "say":
            self.hide_timer.stop()
            self.state = "speak"
            if not self.turn_open:  # Batsy speaking up on its own: its own exchange
                if self.heard or self.answer:
                    self.prev = (self.heard, self.answer)
                self.heard, self.answer, self.turn_open = "", "", True
            self.answer = (self.answer + " " + m.get("text", "")).strip()
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
        elif cmd == "done":
            self.state = "idle"
            self.turn_open = False
            self.level = 0.0
            busy = self.camera_path  # the live camera stays up until closed or idle a while
            linger = max(float(m.get("linger", 6)), 180) if busy else float(m.get("linger", 6))
            self.hide_timer.start(int(1000 * linger))
        elif cmd == "hide" and self.camera_path is None:  # the live camera stays up until it's closed
            self._dismiss(notify=False)
        self.update()

    def _appear(self) -> None:
        if not self.isVisible():
            self.fade = 0.0
            self.showFullScreen()
            self.raise_()
            self.activateWindow()
            self.anim.start()
            print(json.dumps({"event": "shown"}), flush=True)

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
        if self.isVisible():
            print(json.dumps({"event": "hidden"}), flush=True)
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
        self._backdrop(p, w, h)
        p.setOpacity(self.fade)

        has_media = self.image is not None or self.card is not None
        half = 110 if has_media else w * 0.24
        sig_c = QPointF(w * 0.04 + half, h * 0.065 + 17) if has_media else QPointF(w / 2, h * 0.33)
        self._line(p, sig_c, half, h)
        if has_media:  # status beside the short line
            self._status(p, QPointF(sig_c.x() + half + 22, sig_c.y() - 17), center=False)
        else:
            self._status(p, QPointF(w / 2, sig_c.y() + h * 0.12))

        text_w = w * 0.70
        y = (sig_c.y() + h * 0.12 + 62) if not has_media else h * 0.80
        if not has_media:
            prev = " · ".join(x for x in (f"“{self.prev[0]}”" if self.prev[0] else "", self.prev[1]) if x)
            if prev:  # the exchange before, dim and short, so the new one reads as its own
                prev = prev if len(prev) <= 160 else prev[:157].rsplit(" ", 1)[0] + "…"
                y = self._text(p, prev, QRectF((w - text_w) / 2, y, text_w, 90), 14,
                               QColor(STEEL.red(), STEEL.green(), STEEL.blue(), 120)) + 26
            line = self.heard or self.partial
            if line:
                col = STEEL if self.heard else QColor(STEEL.red(), STEEL.green(), STEEL.blue(), 150)
                y = self._text(p, f"“{line}”" if self.heard else line, QRectF((w - text_w) / 2, y, text_w, 200),
                               20, col, italic=not self.heard) + 22
            if self.answer:  # a long reply shows its latest part, never runs off the bottom
                self._text(p, _tail(self.answer, 520), QRectF((w - text_w) / 2, y, text_w, h - y - 40), 30, TEXT)
        else:
            top = h * 0.07
            if self.image is not None and self.card is not None:  # side by side: picture left, card right
                self._image(p, QRectF(w * 0.06, top, w * 0.52, h * 0.68))
                self._card(p, QRectF(w * 0.61, top + h * 0.04, w * 0.33, h * 0.60))
            elif self.image is not None:
                self._image(p, QRectF(w * 0.18, top, w * 0.64, h * 0.68))
            elif self.card is not None:
                self._card(p, QRectF(w * 0.18, top, w * 0.64, h * 0.68))
            sub = _tail(self.answer, 200) or self.heard or self.partial
            if sub:
                self._text(p, sub, QRectF(w * 0.12, h * 0.83, w * 0.76, h * 0.15), 22, TEXT, center=True)
        p.setOpacity(0.5 * self.fade)
        self._text(p, "ESC TO CLOSE", QRectF(0, h - 30, w - 24, 20), 10, STEEL, align=Qt.AlignRight,
                   family=HEAD_FONT, spacing=2.0)

    def _backdrop(self, p: QPainter, w: int, h: int) -> None:
        """Dark gradient with slowly drifting particles."""
        a = self.fade
        sky = QLinearGradient(0, 0, 0, h)
        sky.setColorAt(0, QColor(10, 12, 18, int(240 * a)))
        sky.setColorAt(1, QColor(3, 4, 6, int(245 * a)))
        p.fillRect(self.rect(), sky)
        self._particles(p, w, h, a)

    def _particles(self, p: QPainter, w: int, h: int, a: float) -> None:
        t = time.time() - self.t0
        p.setPen(Qt.NoPen)
        for x, y, r, ph, sp in self.dots:
            alpha = int((60 + 90 * (0.5 + 0.5 * math.sin(t * 1.1 + ph))) * a)
            p.setBrush(QColor(184, 194, 214, alpha))
            px = ((x + t * sp) % 1.0) * w + 18 * math.sin(t * 0.05 + ph)
            p.drawEllipse(QPointF(px, y * h + 18 * math.cos(t * 0.04 + ph)), r, r)

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
        if style != "minimal":
            self._particles(p, w, h, self.fade)
        if style == "stars":
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
            self._line(p, QPointF(w * 0.5, h * 0.5), w * 0.3, h)

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
                pix = self._fit(cache[path], int(r.width()), int(r.height()), Qt.KeepAspectRatioByExpanding)
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

    def _line(self, p: QPainter, c: QPointF, half: float, h: int) -> None:
        """The voice line: ripples with your voice, sweeps while thinking, pulses while speaking."""
        t = time.time() - self.t0
        col = ACCENT.get(self.state, ACCENT["idle"])
        listen, think, speak = self.state == "listen", self.state == "think", self.state == "speak"
        amp = h * 0.125 * (0.035 + 0.5 * self.smooth * listen + 0.2 * speak * (0.55 + 0.45 * math.sin(t * 7.3))
                           + 0.12 * think)
        for strand, (alpha, width) in enumerate(((60, 7.0), (230, 1.8), (90, 1.2))):
            path = QPainterPath()
            for i in range(0, 121):
                u = i / 60 - 1
                env = max(0.0, 1 - u * u) ** 2
                wave = (math.sin(u * 9 + t * 3.1 + strand * 2.1) * 0.55 + math.sin(u * 17 - t * 4.7 + strand * 1.3) * 0.28
                        + math.sin(u * 4 + t * 1.3) * 0.3 + think * 1.4 * math.exp(-((u - math.sin(t * 1.7)) * 3) ** 2))
                pt = QPointF(c.x() + u * half, c.y() + amp * env * wave)
                path.moveTo(pt) if i == 0 else path.lineTo(pt)
            p.setPen(QPen(QColor(col.red(), col.green(), col.blue(), alpha), width))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)

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

    def _fit(self, pix: QPixmap, w: int, h: int, mode) -> QPixmap:
        key = (pix.cacheKey(), w, h, int(mode == Qt.KeepAspectRatio))
        if key not in self._scaled:
            if len(self._scaled) > 8:
                self._scaled.clear()
            self._scaled[key] = pix.scaled(w, h, mode, Qt.SmoothTransformation)
        return self._scaled[key]

    def _image(self, p: QPainter, area: QRectF) -> None:
        pix = self._fit(self.image, int(area.width()), int(area.height()), Qt.KeepAspectRatio)
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
