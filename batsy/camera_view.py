"""Live camera window. Runs as its own process (`python -m batsy.camera_view`).

It only displays the feed and keeps the latest frame on disk (small JPEG, a few times a second).
Nothing is sent to Claude from here: when the user asks a question, the `look` tool reads that
one frame. This avoids reopening the device, which V4L2 usually won't allow while it's in use.
"""
import argparse
import os
import signal
import sys
import time
from pathlib import Path

from batsy.config import SOCKET_PATH

RUN_DIR = SOCKET_PATH.parent
FRAME_PATH = RUN_DIR / "batsy-cam.jpg"
PID_PATH = RUN_DIR / "batsy-cam.pid"
FLASH_PATH = RUN_DIR / "batsy-cam.flash"  # touched by `look` so the window shows Batsy is looking
WINDOW = "Batsy camera"


def find_camera(name_hint: str) -> int:
    """Index of the capture node whose V4L2 name contains name_hint (e.g. 'lenovo'), else the first camera."""
    nodes = []
    for dev in sorted(Path("/sys/class/video4linux").glob("video*"), key=lambda p: int(p.name[5:])):
        try:
            name = (dev / "name").read_text().strip()
            index = int((dev / "index").read_text().strip()) if (dev / "index").exists() else 0
        except (OSError, ValueError):
            continue
        if index == 0:  # index 1+ are metadata nodes of the same camera
            nodes.append((int(dev.name[5:]), name))
    for num, name in nodes:
        if name_hint and name_hint.lower() in name.lower():
            return num
    return nodes[0][0] if nodes else 0


def list_cameras() -> list[str]:
    out = []
    for dev in sorted(Path("/sys/class/video4linux").glob("video*")):
        try:
            out.append(f"/dev/{dev.name}: {(dev / 'name').read_text().strip()}")
        except OSError:
            pass
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", type=int, default=None)
    ap.add_argument("--name", default="lenovo")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--snapshot-edge", type=int, default=768)
    ap.add_argument("--headless", action="store_true", help="no window; Batsy's overlay shows the frames")
    args = ap.parse_args()
    import cv2  # imported here so the rest of Batsy works without opencv installed

    device = args.device if args.device is not None else find_camera(args.name)
    cap = cv2.VideoCapture(device, cv2.CAP_V4L2) if sys.platform.startswith("linux") else cv2.VideoCapture(device)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    if not cap.isOpened():
        sys.exit(f"Could not open camera {device}")

    PID_PATH.write_text(str(os.getpid()))
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    if not args.headless:
        cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(WINDOW, 960, 540)
    save_every = 0.12 if args.headless else 0.4  # the overlay's live view needs a smoother feed
    last_save = 0.0
    tmp = FRAME_PATH.with_suffix(".tmp.jpg")
    try:
        while running:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            now = time.time()
            if now - last_save > save_every:
                h, w = frame.shape[:2]
                scale = args.snapshot_edge / max(h, w)
                small = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA) if scale < 1 else frame
                cv2.imwrite(str(tmp), small, [cv2.IMWRITE_JPEG_QUALITY, 80])
                os.replace(tmp, FRAME_PATH)
                last_save = now
            if args.headless:
                continue
            view = frame.copy()
            looking = FLASH_PATH.exists() and now - FLASH_PATH.stat().st_mtime < 1.5
            if looking:
                cv2.rectangle(view, (0, 0), (view.shape[1] - 1, view.shape[0] - 1), (255, 0, 200), 12)
            label = "Batsy is looking..." if looking else "Batsy camera - ask me about it"
            cv2.putText(view, label, (16, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 4, cv2.LINE_AA)
            cv2.putText(view, label, (16, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2, cv2.LINE_AA)
            cv2.imshow(WINDOW, view)
            key = cv2.waitKey(15) & 0xFF
            if key in (27, ord("q")) or cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()
        for p in (PID_PATH, FRAME_PATH):
            p.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
