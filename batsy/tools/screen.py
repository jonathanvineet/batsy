"""Let Claude see, on request only: the live camera window's latest frame, a webcam photo, or the screen.

Images are the most expensive thing Batsy sends, so they're captured only when the user asks and
downscaled hard (a 768px frame is ~450 tokens; full HD would be ~2700).
"""
import base64
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image

from batsy.config import DATA_DIR
from batsy.camera_view import FLASH_PATH, FRAME_PATH, PID_PATH

_SCREENSHOT_CMDS = [
    # GNOME Wayland only lets the portal take screenshots; it needs the system Python's PyGObject.
    ("/usr/bin/python3", ["/usr/bin/python3", str(Path(__file__).with_name("portal_shot.py")), "{out}"]),
    ("gnome-screenshot", ["gnome-screenshot", "-f", "{out}"]),
    ("spectacle", ["spectacle", "-b", "-n", "-f", "-o", "{out}"]),
    ("grim", ["grim", "{out}"]),
    ("import", ["import", "-window", "root", "{out}"]),
]


# Set by the voice app: on_camera(True/False) puts the live feed in the overlay instead of a separate window.
on_camera = None


def camera_pid() -> int | None:
    try:
        pid = int(PID_PATH.read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def open_camera(cfg) -> str:
    if camera_pid():
        return "Camera window is already open."
    argv = [sys.executable, "-m", "batsy.camera_view", "--name", cfg.get("camera.name"),
            "--snapshot-edge", str(cfg.get("camera.snapshot_edge"))] + (["--headless"] if on_camera else [])
    if cfg.get("camera.device") is not None:
        argv += ["--device", str(cfg.get("camera.device"))]
    log = open(DATA_DIR / "camera.log", "wb")
    proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(80):  # wait up to 8 s for the first frame
        if FRAME_PATH.exists():
            if on_camera:
                on_camera(True)
            return "Camera window is open."
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    if proc.poll() is None:
        proc.terminate()
    tail = (DATA_DIR / "camera.log").read_text(errors="replace")[-300:].strip()
    return f"The camera didn't start. {tail}".strip()


def close_camera() -> str:
    pid = camera_pid()
    if not pid:
        return "Camera window isn't open."
    os.kill(pid, signal.SIGTERM)
    if on_camera:
        on_camera(False)
    return "Camera closed."


def _capture_screen(out: Path) -> str | None:
    errors = []
    for binary, argv in _SCREENSHOT_CMDS:
        if not shutil.which(binary):
            continue
        p = subprocess.run([a.format(out=out) for a in argv], capture_output=True, text=True, timeout=20)
        if p.returncode == 0 and out.exists() and out.stat().st_size > 0:
            return None
        errors.append(f"{binary}: {p.stderr.strip()[:200]}")
    return "; ".join(errors) or "no screenshot tool installed (dnf install gnome-screenshot)"


def _capture_webcam(out: Path, cfg) -> str | None:
    # Prefer the open camera window's latest frame: no device contention, no warm-up delay.
    if not camera_pid() and on_camera and open_camera(cfg).endswith("open."):
        time.sleep(1.0)  # let exposure settle; the user sees the feed come up before Batsy looks
    if camera_pid() and FRAME_PATH.exists() and time.time() - FRAME_PATH.stat().st_mtime < 3:
        FLASH_PATH.touch()
        shutil.copy(FRAME_PATH, out)
        return None
    if not shutil.which("ffmpeg"):
        return "ffmpeg not installed (dnf install ffmpeg-free)"
    from batsy.camera_view import find_camera

    dev = cfg.get("camera.device")
    device = f"/dev/video{dev if dev is not None else find_camera(cfg.get('camera.name'))}"
    p = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "v4l2", "-i", device, "-frames:v", "1", str(out)],
        capture_output=True, text=True, timeout=20,
    )
    return None if p.returncode == 0 else p.stderr.strip()[:300]


def capture(source: str, out: Path, cfg) -> str | None:
    """Save one camera frame or screenshot to `out` (downscaled JPEG). Returns an error string or None."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / ("shot.png" if source == "screen" else "cam.jpg")
        err = _capture_screen(raw) if source == "screen" else _capture_webcam(raw, cfg)
        if err:
            return err
        img = Image.open(raw).convert("RGB")
        img.thumbnail((cfg.get("camera.screen_edge" if source == "screen" else "camera.snapshot_edge"),) * 2)
        img.save(out, "JPEG", quality=85)
    return None


def _encode(path: Path, edge: int) -> str:
    img = Image.open(path).convert("RGB")
    img.thumbnail((edge, edge))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=75)
    return base64.standard_b64encode(buf.getvalue()).decode()


def register(reg):
    @reg.tool(
        "camera",
        "Open or close the live camera window (shows the webcam feed on screen; nothing is sent to you "
        "until you call look with source=webcam).",
        {"action": {"type": "string", "enum": ["open", "close", "status"]}},
        ["action"],
    )
    def camera(ctx, action):
        if action == "open":
            return open_camera(ctx.cfg)
        if action == "close":
            return close_camera()
        return "open" if camera_pid() else "closed"

    @reg.tool(
        "look",
        "See one image: source=webcam (the camera's current frame) when the user asks about what's in front of "
        "the camera or what they're holding; source=screen when they ask about the screen. Call only when the "
        "question needs it, and once per question. detail=high only for reading small text.",
        {
            "source": {"type": "string", "enum": ["screen", "webcam"]},
            "detail": {"type": "string", "enum": ["normal", "high"]},
        },
        ["source"],
    )
    def look(ctx, source, detail="normal"):
        cfg = ctx.cfg
        edge = 1568 if detail == "high" else cfg.get("camera.snapshot_edge" if source == "webcam" else "camera.screen_edge")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / ("shot.png" if source == "screen" else "cam.jpg")
            err = _capture_screen(out) if source == "screen" else _capture_webcam(out, cfg)
            if err:
                return f"Could not capture {source}: {err}"
            data = _encode(out, edge)
        return [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}]
