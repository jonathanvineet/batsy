"""Let Claude see: a screenshot of the desktop or a frame from the webcam."""
import base64
import io
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

MAX_EDGE = 1568  # larger images are downscaled by the API anyway

_SCREENSHOT_CMDS = [
    ("gnome-screenshot", ["gnome-screenshot", "-f", "{out}"]),
    ("spectacle", ["spectacle", "-b", "-n", "-f", "-o", "{out}"]),
    ("grim", ["grim", "{out}"]),
    ("import", ["import", "-window", "root", "{out}"]),
]


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


def _capture_webcam(out: Path, device: str) -> str | None:
    if not shutil.which("ffmpeg"):
        return "ffmpeg not installed (dnf install ffmpeg-free)"
    p = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "v4l2", "-i", device, "-frames:v", "1", str(out)],
        capture_output=True, text=True, timeout=20,
    )
    return None if p.returncode == 0 else p.stderr.strip()[:300]


def _encode(path: Path) -> str:
    img = Image.open(path).convert("RGB")
    img.thumbnail((MAX_EDGE, MAX_EDGE))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return base64.standard_b64encode(buf.getvalue()).decode()


def register(reg):
    @reg.tool(
        "look",
        "Take a screenshot of the user's screen (source=screen) or a webcam photo (source=webcam) and see it. "
        "Use when the user says 'look at this', asks about what's on screen, or to check a GUI action worked.",
        {
            "source": {"type": "string", "enum": ["screen", "webcam"]},
            "webcam_device": {"type": "string", "description": "default /dev/video0"},
        },
        ["source"],
    )
    def look(ctx, source, webcam_device="/dev/video0"):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / ("shot.png" if source == "screen" else "cam.jpg")
            err = _capture_screen(out) if source == "screen" else _capture_webcam(out, webcam_device)
            if err:
                return f"Could not capture {source}: {err}"
            data = _encode(out)
        return [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
            {"type": "text", "text": f"Captured {source}."},
        ]
