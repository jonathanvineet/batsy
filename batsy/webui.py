"""Qt WebEngine shell shared by the overlay and the 3D viewer window.

The UI itself is HTML/CSS/three.js in batsy/ui, served offline from a private batsy:// scheme:
  batsy://ui/<file>      the bundled UI
  batsy://file/<path>    a local file (pictures, camera frames)
  batsy://mesh/<id>      3D geometry prepared by batsy.viewer3d
Python -> JS: Web.send({...}) calls window.batsy.cmd(...). JS -> Python: console.log("@@batsy {json}").
If the renderer process dies (GPU driver trouble), the page reloads itself and on_reload() replays state.
"""
import json
import logging
import mimetypes
import os
import threading
import time
import urllib.parse
from pathlib import Path

log = logging.getLogger("batsy.webui")

UI_DIR = Path(__file__).parent / "ui"
SCHEME = b"batsy"
PREFIX = "@@batsy "
DEBUG = bool(os.environ.get("BATSY_UI_DEBUG"))
MESHES: dict[str, bytes] = {}  # id -> packed geometry, served at batsy://mesh/<id>
_MESH_LOCK = threading.Lock()
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("font/woff2", ".woff2")


def file_url(path) -> str:
    return "batsy://file" + urllib.parse.quote(str(Path(path).resolve()))


def put_mesh(key: str, data: bytes) -> str:
    with _MESH_LOCK:
        MESHES[key] = data
    return f"batsy://mesh/{key}"


def drop_meshes(keep: set[str]) -> None:
    with _MESH_LOCK:
        for k in [k for k in MESHES if k not in keep]:
            del MESHES[k]


def prepare(safe_gpu: bool = False) -> None:
    """Call before QApplication exists: registers batsy:// and sets lean Chromium flags."""
    from PySide6.QtWebEngineCore import QWebEngineUrlScheme

    flags = ["--renderer-process-limit=1", "--disable-background-networking", "--disable-extensions",
             "--autoplay-policy=no-user-gesture-required"]
    if safe_gpu or os.environ.get("BATSY_SAFE_GPU"):  # software rendering: slower, but survives bad GPU drivers
        flags += ["--disable-gpu", "--disable-gpu-compositing", "--enable-unsafe-swiftshader"]
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(flags + [os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")])
    s = QWebEngineUrlScheme(SCHEME)
    s.setSyntax(QWebEngineUrlScheme.Syntax.Host)
    s.setFlags(QWebEngineUrlScheme.Flag.SecureScheme | QWebEngineUrlScheme.Flag.LocalAccessAllowed
               | QWebEngineUrlScheme.Flag.CorsEnabled | QWebEngineUrlScheme.Flag.FetchApiAllowed
               | QWebEngineUrlScheme.Flag.ContentSecurityPolicyIgnored)
    QWebEngineUrlScheme.registerScheme(s)


def _handler_class():
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtWebEngineCore import QWebEngineUrlRequestJob, QWebEngineUrlSchemeHandler

    class Handler(QWebEngineUrlSchemeHandler):
        def requestStarted(self, job):  # noqa: N802 (Qt name)
            url = job.requestUrl()
            host, path = url.host(), urllib.parse.unquote(url.path())
            data, mime = None, "application/octet-stream"
            try:
                if host == "ui":
                    f = (UI_DIR / path.lstrip("/")).resolve()
                    if UI_DIR.resolve() in f.parents and f.is_file():
                        data, mime = f.read_bytes(), mimetypes.guess_type(f.name)[0] or mime
                elif host == "file":
                    f = Path(path)
                    if f.is_file():
                        data, mime = f.read_bytes(), mimetypes.guess_type(f.name)[0] or mime
                elif host == "mesh":
                    with _MESH_LOCK:
                        data = MESHES.get(path.lstrip("/"))
            except OSError as e:
                log.warning("batsy:// %s: %s", url.toString(), e)
            if data is None:
                job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
                return
            buf = QBuffer(parent=job)  # lives as long as the request
            buf.setData(QByteArray(data))
            buf.open(QIODevice.OpenModeFlag.ReadOnly)
            job.reply(QByteArray(mime.encode()), buf)

    return Handler


class Web:
    """A QWebEngineView showing batsy://ui/<page>, with a JSON bridge both ways."""

    def __init__(self, page: str, on_event, parent=None):
        from PySide6.QtCore import QObject, Qt, QUrl, Signal
        from PySide6.QtGui import QColor
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
        from PySide6.QtWebEngineWidgets import QWebEngineView

        class Page(QWebEnginePage):
            def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
                if message.startswith(PREFIX):
                    try:
                        relay.event.emit(json.loads(message[len(PREFIX):]))
                    except ValueError:
                        pass
                elif level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel or DEBUG:
                    log.warning("ui js: %s (%s:%s)", message, source, line)

        class Relay(QObject):
            event = Signal(dict)
            js = Signal(str)  # any thread -> GUI thread
            call = Signal(str, object)  # (expression, callback(result)) from a worker thread

        relay = Relay()
        self.relay = relay
        self.on_event, self.on_reload = on_event, None
        self.ready = False
        self.backlog: list[str] = []
        self.crashes: list[float] = []
        profile = QWebEngineProfile.defaultProfile()
        if not getattr(QWebEngineProfile, "_batsy_handler", None):
            QWebEngineProfile._batsy_handler = _handler_class()(profile)  # keep a reference: Qt doesn't own it
            profile.installUrlSchemeHandler(SCHEME, QWebEngineProfile._batsy_handler)
        self.view = QWebEngineView(parent)
        self.page = Page(profile, self.view)
        self.page.setBackgroundColor(QColor(6, 7, 10))
        self.view.setPage(self.page)
        st = self.page.settings()
        for attr, on in ((QWebEngineSettings.WebAttribute.WebGLEnabled, True),
                         (QWebEngineSettings.WebAttribute.Accelerated2dCanvasEnabled, True),
                         (QWebEngineSettings.WebAttribute.ShowScrollBars, False),
                         (QWebEngineSettings.WebAttribute.ErrorPageEnabled, False),
                         (QWebEngineSettings.WebAttribute.PluginsEnabled, False)):
            st.setAttribute(attr, on)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        relay.event.connect(self._event)
        relay.js.connect(self._run_js)
        relay.call.connect(lambda expr, cb: self.page.runJavaScript(expr, 0, cb))
        self.page.loadFinished.connect(self._loaded)
        self.page.renderProcessTerminated.connect(self._renderer_died)
        self.url = QUrl(f"batsy://ui/{page}")
        self.view.load(self.url)

    # --- bridge -------------------------------------------------------------------------------------------
    def send(self, msg: dict) -> None:
        """Thread-safe: hand a command to the page (queued until it has loaded)."""
        self.relay.js.emit(json.dumps(msg, default=float))

    def _run_js(self, payload: str) -> None:
        if not self.ready:
            self.backlog.append(payload)
            del self.backlog[:-200]
            return
        self.page.runJavaScript(f"window.batsy && window.batsy.cmd({payload})")

    def call(self, expr: str, timeout: float = 10.0):
        """Evaluate JS from a worker thread and wait for the result (never call this on the GUI thread)."""
        box, done = {}, threading.Event()

        def finish(result):
            box["v"] = result
            done.set()

        self.relay.call.emit(expr, finish)
        done.wait(timeout)
        return box.get("v")

    def _event(self, ev: dict) -> None:
        if DEBUG:
            log.warning("ui event %s", ev)
        if ev.get("event") == "ready":
            return
        try:
            self.on_event(ev)
        except Exception:
            log.exception("ui event %s", ev)

    def _loaded(self, ok: bool) -> None:
        if not ok:
            log.warning("ui page failed to load")
            return
        self.ready = True
        backlog, self.backlog = self.backlog, []
        for payload in backlog:
            self._run_js(payload)
        if self.on_reload:
            self.on_reload()

    def _renderer_died(self, status, code) -> None:
        now = time.time()
        self.crashes = [t for t in self.crashes if now - t < 120] + [now]
        log.warning("ui renderer ended (%s, %s); crash %d in 2 min", status, code, len(self.crashes))
        self.ready = False
        if len(self.crashes) >= 3:  # the GPU path is broken: let the parent restart us in safe mode
            from PySide6.QtWidgets import QApplication

            QApplication.exit(3)
            return
        from PySide6.QtCore import QTimer

        QTimer.singleShot(500, lambda: self.view.load(self.url))
