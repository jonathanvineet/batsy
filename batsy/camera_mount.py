"""The camera mount: a NodeMCU (firmware/camera_mount) or an Arduino Uno (firmware/camera_mount_uno) with two
servos, over its USB cable (the same one-line commands on both).

  pan  = MG995, 360° continuous: it only knows a speed, so "turn 90" is a timed spin (calibrated degrees per second)
         and the Uno keeps an estimate of where it points. "Center" goes back by that estimate.
  tilt = MG90S, 180°: a real angle (10..170, 90 = level).

Voice: "turn the camera left", "look right 45 degrees", "tilt the camera up", "camera up a bit", "turn the camera
around", "spin the camera" / "stop the camera", "center the camera", "this is straight ahead" (sets zero),
"the camera turned too far / not far enough" (tunes the degrees per second), "where is the camera pointing".

The Uno is found by its own USB name, never by ttyUSB number (the printer is a CH340 too: printer3d excludes this
port, and this never opens the printer's). The port stays open: opening it resets the Uno (and twitches the tilt).
Config: [camera_mount] port = "/dev/serial/by-id/...", up = 1 (-1 if "up" tilts down), right = 1 (-1 if mirrored).
"""
import glob
import json
import logging
import re
import termios
import threading
import time
from pathlib import Path

log = logging.getLogger("batsy.mount")
PORT_FILE = Path.home() / ".local/share/batsy/camera_mount_port"
_lock = threading.Lock()
_ser = None

WORDS = {"a bit": 10, "a little": 10, "slightly": 8, "a lot": 60, "all the way": 80, "halfway": 45}
CAM = r"(?:the |my )?(?:camera|cam|webcam|camera mount|mount)"
_k = iter(range(1, 99))


def _num() -> str:  # a number of degrees (each use its own group name: n1, n2, ...)
    return rf"(?P<n{next(_k)}>\d+(?:\.\d+)?)\s*(?:degrees?|deg|°)?"
PAN = re.compile(rf"\b(?:turn|pan|rotate|move|point|swing)\s+{CAM}\s+(?:to the\s+)?(?P<dir>left|right)(?:\s+(?:by\s+)?{_num()})?"
                 rf"|\b(?:turn|pan|rotate|move)\s+{CAM}\s+(?:by\s+)?{_num()}\s+(?:to the\s+)?(?P<dir2>left|right)"
                 rf"|\b{CAM}\s+(?P<dir3>left|right)(?:\s+{_num()})?\b"
                 rf"|\blook\s+(?P<dir4>left|right)(?:\s+{_num()})?")
TILT = re.compile(rf"\b(?:tilt|move|point|turn)\s+{CAM}\s+(?P<dir>up|down)(?:\s+(?:by\s+)?{_num()})?"
                  rf"|\b{CAM}\s+(?P<dir2>up|down)(?:\s+{_num()})?\b|\blook\s+(?P<dir3>up|down)(?:\s+{_num()})?"
                  rf"|\btilt\s+{CAM}\s+(?:to\s+)?{_num()}")
AROUND = re.compile(rf"\b(?:turn|spin|rotate)\s+{CAM}\s+(?:all the way\s+)?around\b|\blook behind\b")
SPIN = re.compile(rf"\b(?:spin|keep turning|rotate|pan)\s+{CAM}(?:\s+(?:slowly|around and around|continuously|"
                  rf"(?P<way>left|right)))?\s*$|\bsweep\s+{CAM}\b")
STOP = re.compile(rf"\b(?:stop|freeze|hold)\s+{CAM}\b|\b{CAM}\s+stop\b")
CENTER = re.compile(rf"\b(?:center|centre|reset|straighten|level)\s+{CAM}\b|\b{CAM}\s+(?:back\s+)?(?:to\s+)?(?:the\s+)?"
                    rf"(?:center|centre|front|straight)\b|\blook (?:straight|ahead|forward)\b")
ZERO = re.compile(rf"\b(?:this is|that'?s|it'?s|now it'?s)\s+(?:pointing\s+)?straight(?: ahead)?\b|\b{CAM}\s+is\s+"
                  rf"(?:pointing\s+)?straight\b|\bset\s+{CAM}\s+zero\b")
TUNE = re.compile(rf"\b{CAM}\s+(?:turned|went|moved|spun)\s+(?P<how>too far|too much|not far enough|too little)\b")
SPEED = re.compile(rf"\bset\s+{CAM}\s+(?:turn(?:ing)?\s+)?speed\s+(?:to\s+)?(?P<dps>\d+)")  # degrees a second, full speed
WHERE = re.compile(rf"\bwhere(?:'s| is)\s+{CAM}\s+(?:pointing|looking|facing)\b")


def _cfg(app, key, default):
    try:
        return (app.cfg.get("camera_mount") or {}).get(key, default)
    except Exception:
        return default


def ports() -> list[str]:
    """The mount's port(s) (the by-id link it answered on), for printer3d to keep away from."""
    try:
        p = PORT_FILE.read_text().strip()
        return [p] if p and Path(p).exists() else []
    except OSError:
        return []


def _find(app) -> str | None:
    """The configured port, else the one it answered on before, else a CH340 that isn't the printer's (probed with
    one "?": only ever a port the printer isn't on)."""
    want = _cfg(app, "port", "") or (ports() or [""])[0]
    if want and Path(want).exists():
        return want
    from batsy import printer3d

    try:
        printer = Path(printer3d.PORT_FILE.read_text().strip()).resolve()
    except OSError:
        return None  # not knowing which one is the printer: never guess
    for link in sorted(glob.glob("/dev/serial/by-id/*")):
        if Path(link).resolve() == printer or not re.search(r"1a86|ch34|arduino|2341|usb-serial", link, re.I):
            continue
        if _probe(link):
            PORT_FILE.write_text(link)
            return link
    return None


def _open(port: str, timeout: float):
    """DTR and RTS low before opening: on a NodeMCU those lines are its reset and boot-mode pins."""
    import serial

    s = serial.Serial()
    s.port, s.baudrate, s.timeout = port, 115200, timeout
    s.dtr = s.rts = False
    s.open()
    return s


def _probe(link: str) -> bool:
    import serial

    try:
        with _open(link, 1) as s:
            time.sleep(2.2)  # an Uno resets as the port opens
            s.reset_input_buffer()
            s.write(b"?\n")
            return "batsy-cam" in s.readline().decode(errors="replace")
    except (OSError, serial.SerialException):
        return False


def send(app, cmd: str) -> dict | None:
    """One command to the Uno; its status back (None if it isn't plugged in)."""
    import serial

    global _ser
    with _lock:
        for _ in range(2):
            try:
                if _ser is None:
                    port = _find(app)
                    if not port:
                        return None
                    _ser = _open(port, 2)
                    time.sleep(2.2)  # an Uno resets as the port opens (a NodeMCU doesn't: DTR/RTS stay low)
                    _ser.reset_input_buffer()
                _ser.write((cmd + "\n").encode())
                end = time.time() + 15  # a tilt sweep answers when it's done
                while time.time() < end:
                    line = _ser.readline().decode(errors="replace").strip()
                    if line.startswith("{"):
                        return json.loads(line)
                return None
            except (OSError, serial.SerialException, ValueError, termios.error) as e:  # (the board dropping off USB)
                log.info("camera mount: %s", e)
                try:
                    _ser and _ser.close()
                except Exception:
                    pass
                _ser = None
    return None


def _n(m, default: float) -> float:
    n = next((m.group(g) for g in m.groupdict() if g.startswith("n") and m.group(g)), None)
    return float(n) if n else default


def _amount(text: str, default: float) -> float:
    return next((v for w, v in WORDS.items() if w in text), default)


def handle(app, n: str) -> str | None:
    if not re.search(r"\b(?:camera|cam|webcam|mount|look (?:left|right|up|down|behind|straight|ahead|forward)|"
                     r"pointing straight|straight ahead)\b", n):
        return None
    right = int(_cfg(app, "right", 1))
    up = int(_cfg(app, "up", 1))
    out, said = None, ""
    if (m := SPEED.search(n)):
        st = send(app, "?")
        out = st and send(app, f"K {m.group('dps')} {st['stop']} {st['dir']}")
        said = f"Turn speed set to {out['dps']:g} degrees a second." if out else ""
    elif STOP.search(n):
        out, said = send(app, "X"), "Stopped."
    elif TUNE.search(n):
        st = send(app, "?")
        if st:  # too far: it turns faster than it thinks, so it should think faster (shorter spins)
            dps = round(st["dps"] * (1.1 if re.search(r"too far|too much", n) else 1 / 1.1), 1)
            out = send(app, f"K {dps} {st['stop']} {st['dir']}")
            said = f"Adjusted. Turns are {'shorter' if dps > st['dps'] else 'longer'} now."
    elif ZERO.search(n):
        out, said = send(app, "Z"), "Got it, that's straight ahead."
    elif WHERE.search(n):
        out = send(app, "?")
        if out:
            p = ((out["pan"] + 180) % 360) - 180
            side = "straight ahead" if abs(p) < 5 else f"{abs(round(p))} degrees to the {'right' if (p > 0) == (right > 0) else 'left'}"
            tilt = out["tilt"] - 90
            return (f"It's pointing {side}" + ("" if abs(tilt) < 5 else
                    f", tilted {abs(tilt)} degrees {'up' if (tilt > 0) == (up > 0) else 'down'}") + ".")
    elif CENTER.search(n):
        out, said = send(app, "C"), "Back to center."
    elif AROUND.search(n):
        out, said = send(app, "R 180"), "Turning around."
    elif (m := SPIN.search(n)):
        way = -1 if (m.group("way") == "left") else 1
        speed = 25 if "slow" in n or "sweep" in n else 50
        out, said = send(app, f"S {way * right * speed}"), "Spinning. Say stop the camera."
    elif (m := PAN.search(n)):
        d = next(m.group(g) for g in ("dir", "dir2", "dir3", "dir4") if m.group(g))
        deg = _n(m, _amount(n, 30)) * (1 if d == "right" else -1) * right
        out, said = send(app, f"R {deg:g}"), f"Turning {d}."
    elif (m := TILT.search(n)):
        st = send(app, "?")
        if not st:
            out = None
        else:
            d = m.group("dir") or m.group("dir2") or m.group("dir3")
            if d:
                deg = _n(m, _amount(n, 15)) * (1 if d == "up" else -1) * up
                target = st["tilt"] + deg
            else:
                target = 90 + (_n(m, 0) - 90) * up
            target = max(10, min(170, round(target)))
            out, said = send(app, f"T {target}"), ("Tilting " + (d or f"to {target}") + ".")
            if out and out["tilt"] != target and out["tilt"] == st["tilt"]:
                if target in (10, 170) or d:  # really at the end of its travel
                    said = f"It's already as far {d or 'that way'} as it goes."
                else:  # the reply is its start-up line: it restarted mid-move (a servo pulling more than USB gives)
                    said = ("The mount restarted while moving: the servo is drawing more power than the USB port "
                            "gives. It needs its own 5 to 6 volt supply.")
    else:
        return None
    if out is None:
        return "The camera mount isn't answering. Is the Uno plugged in?"
    return said
